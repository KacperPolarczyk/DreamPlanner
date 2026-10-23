"""Refresh cookie response handling shared by session-ending operations."""

from fastapi import Response

from src.core.config import Settings


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """Expire the refresh cookie using its configured name and path."""
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )
