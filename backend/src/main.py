"""FastAPI application assembly and ASGI entry point."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import api_router
from src.auth.dependencies import AuthHttpError
from src.auth.jwt_codec import JwtCodec
from src.auth.rate_limits import AuthRateLimits, RateLimited
from src.core.config import Settings


def _problem(
    status: int,
    kind: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render a safe RFC 9457 problem response without request secrets."""
    return JSONResponse(
        {"type": kind, "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def create_app() -> FastAPI:
    """Load settings and attach versioned routes with the browser CORS policy."""
    settings = Settings()
    application = FastAPI(title=settings.app_name)
    application.state.settings = settings
    application.state.jwt_codec = (
        JwtCodec(settings) if settings.jwt_signing_key_base64 is not None else None
    )
    application.state.auth_rate_limits = AuthRateLimits(settings)

    @application.middleware("http")
    async def limit_public_auth_by_ip(request: Request, call_next):
        """Count malformed and rejected attempts before request validation."""
        operation = {
            "/api/v1/auth/sessions": "login",
            "/api/v1/auth/sessions/current/refresh": "refresh",
            "/api/v1/auth/activations": "activation",
            "/api/v1/users/me/password-change": "password_change",
        }.get(request.url.path)
        if request.method == "POST" and operation:
            try:
                application.state.auth_rate_limits.check_ip(request, operation)
            except RateLimited as exc:
                return _problem(
                    429,
                    "urn:dreamplanner:problem:rate-limited",
                    "Zbyt wiele prób",
                    "Spróbuj ponownie później.",
                    exc.headers,
                )
        return await call_next(request)

    @application.exception_handler(AuthHttpError)
    def handle_auth_error(request: Request, exc: AuthHttpError) -> JSONResponse:
        """Translate authentication workflow errors to safe public problems."""
        headers = {"WWW-Authenticate": "Bearer"} if exc.bearer else None
        return _problem(exc.status, exc.kind, exc.title, exc.detail, headers)

    @application.exception_handler(RateLimited)
    def handle_rate_limit(request: Request, exc: RateLimited) -> JSONResponse:
        """Return one problem shape for both IP and identity exhaustion."""
        return _problem(
            429,
            "urn:dreamplanner:problem:rate-limited",
            "Zbyt wiele prób",
            "Spróbuj ponownie później.",
            exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Omit input, context, and secret values from validation problems."""
        syntax_error = any(error["type"] == "json_invalid" for error in exc.errors())
        if syntax_error:
            return _problem(
                400, "about:blank", "Nieprawidłowy JSON", "Nieprawidłowe żądanie."
            )
        errors = [
            {
                "location": [str(part) for part in error["loc"]],
                "code": error["type"],
                "message": "Nieprawidłowa wartość.",
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {
                "type": "urn:dreamplanner:problem:validation-error",
                "title": "Nieprawidłowe dane",
                "status": 422,
                "detail": "Nieprawidłowe dane żądania.",
                "errors": errors,
            },
            status_code=422,
            media_type="application/problem+json",
            headers={"Cache-Control": "no-store"},
        )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.include_router(api_router)
    return application


app = create_app()
