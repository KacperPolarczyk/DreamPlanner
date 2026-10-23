"""Versioned API router composition."""

from fastapi import APIRouter

from src.auth.router import router as auth_router
from src.health.router import router as health_router
from src.users.router import router as users_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(health_router)
api_router.include_router(users_router)
