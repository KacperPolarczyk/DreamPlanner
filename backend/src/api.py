"""Versioned API router composition."""

from fastapi import APIRouter

from src.assignments.router import employee_router
from src.assignments.router import router as assignments_router
from src.auth.router import router as auth_router
from src.availability.router import router as availability_router
from src.health.router import router as health_router
from src.positions.router import router as positions_router
from src.schedules.router import router as schedules_router
from src.users.employee_router import router as employees_router
from src.users.router import router as users_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(health_router)
api_router.include_router(positions_router)
api_router.include_router(schedules_router)
api_router.include_router(availability_router)
api_router.include_router(assignments_router)
api_router.include_router(employee_router)
api_router.include_router(users_router)
api_router.include_router(employees_router)
