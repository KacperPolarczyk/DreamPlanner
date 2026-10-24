from fastapi.testclient import TestClient

from src.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_health() -> None:
    client = TestClient(app)
    response = client.get("/openapi.json")
    docs_response = client.get("/docs")

    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]
    readiness = response.json()["paths"]["/api/v1/health/ready"]["get"]
    assert readiness["responses"]["503"]["description"] == (
        "Baza danych jest niedostępna."
    )
    assert docs_response.status_code == 200


def test_readiness_checks_database_with_bounded_timeouts(monkeypatch) -> None:
    """Readiness runs SELECT 1 through a disposable bounded connection."""
    seen = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            seen["query"] = str(statement)

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            seen["disposed"] = True

    def create_engine(url, **kwargs):
        seen["url"] = url
        seen["options"] = kwargs
        return Engine()

    monkeypatch.setattr("src.health.router.create_engine", create_engine)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://reader:secret@127.0.0.1:5432/example"
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert seen["query"] == "SELECT 1"
    assert seen["disposed"] is True
    assert seen["url"].query["connect_timeout"] == "2"
    assert "statement_timeout=2000" in seen["url"].query["options"]
    assert seen["options"]["hide_parameters"] is True


def test_readiness_failure_is_redacted_and_liveness_survives(monkeypatch) -> None:
    """A database failure returns a safe 503 without changing liveness."""
    secret = "password=secret database=internal"

    def fail():
        raise RuntimeError(secret)

    monkeypatch.setattr("src.health.router._check_database_ready", fail)
    with TestClient(app) as client:
        readiness = client.get("/api/v1/health/ready")
        liveness = client.get("/api/v1/health")

    assert readiness.status_code == 503
    assert readiness.headers["content-type"] == "application/problem+json"
    assert readiness.headers["cache-control"] == "no-store"
    assert readiness.json() == {
        "type": "about:blank",
        "title": "Usługa niedostępna",
        "status": 503,
        "detail": "Baza danych jest niedostępna.",
    }
    assert secret not in readiness.text
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}


def test_readiness_without_database_url_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
