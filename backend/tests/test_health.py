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
    assert docs_response.status_code == 200
