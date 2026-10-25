"""PostgreSQL URL normalization without opening a database connection."""

import pytest
from sqlalchemy.engine import URL, make_url

from src.core.database_url import normalize_postgresql_url


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+psycopg"])
def test_normalize_preserves_connection_parts_and_tls(driver: str) -> None:
    source = (
        f"{driver}://app:p%40ss%3Aword@db.example:5433/dreamplanner"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Froot.pem"
        "&application_name=dreamplanner&target_session_attrs=read-write"
    )

    result = normalize_postgresql_url(source)
    parsed = make_url(source)

    assert isinstance(result, URL)
    assert result.drivername == "postgresql+psycopg"
    assert result.username == parsed.username == "app"
    assert result.password == parsed.password == "p@ss:word"
    assert result.host == parsed.host == "db.example"
    assert result.port == parsed.port == 5433
    assert result.database == parsed.database == "dreamplanner"
    assert result.query == parsed.query
    assert result.query["sslmode"] == "verify-full"
    assert result.query["sslrootcert"] == "/etc/ssl/root.pem"


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+psycopg2://app:secret-marker@db.example/dreamplanner",
        "sqlite:///tmp/secret-marker.db",
        "not-a-uri-secret-marker",
        "postgresql://app:secret-marker@db.example:invalid/dreamplanner",
    ],
)
def test_rejected_urls_never_expose_credentials(value: str) -> None:
    with pytest.raises(ValueError) as error:
        normalize_postgresql_url(value)

    assert "secret-marker" not in str(error.value)
