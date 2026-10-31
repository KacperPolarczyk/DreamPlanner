"""Public liveness and database readiness routes."""

import os

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from src.core.config import Settings
from src.core.database_url import normalize_postgresql_url
from src.health.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Sprawdź stan API")
async def get_health(response: Response) -> HealthResponse:
    """Confirm process liveness without checking downstream dependencies."""
    response.headers["X-Release-Id"] = os.getenv("RELEASE_ID", "local")
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(status="ok")


def _check_database_ready() -> None:
    """Probe PostgreSQL through a disposable connection with short timeouts."""
    database_url = Settings().database_url
    if not database_url:
        raise ValueError("Database URL is absent")
    url = normalize_postgresql_url(database_url)
    existing_options = url.query.get("options", "")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)
    options = f"{existing_options} -c statement_timeout=2000".strip()
    url = url.update_query_dict(
        {"connect_timeout": "2", "options": options}, append=False
    )
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


@router.get(
    "/health/ready", response_model=HealthResponse, summary="Sprawdź gotowość API"
)
def get_readiness(response: Response) -> HealthResponse | JSONResponse:
    """Sprawdza gotowość API przez odczytowe połączenie z PostgreSQL."""
    response.headers["X-Release-Id"] = os.getenv("RELEASE_ID", "local")
    response.headers["Cache-Control"] = "no-store"
    try:
        _check_database_ready()
    except Exception:
        return JSONResponse(
            {
                "type": "about:blank",
                "title": "Usługa niedostępna",
                "status": 503,
                "detail": "Baza danych jest niedostępna.",
            },
            status_code=503,
            media_type="application/problem+json",
            headers={"Cache-Control": "no-store"},
        )
    return HealthResponse(status="ok")
