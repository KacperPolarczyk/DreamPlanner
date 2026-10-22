"""Public health check route."""

from fastapi import APIRouter

from src.health.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Sprawdź stan API")
async def get_health() -> HealthResponse:
    """Confirm process liveness without checking downstream dependencies."""
    return HealthResponse(status="ok")
