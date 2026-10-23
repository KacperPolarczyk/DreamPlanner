"""Approved per-IP and per-identity limits backed by SlowAPI memory storage."""

import hashlib
import hmac
import math
import secrets
import time
from dataclasses import dataclass

from fastapi import Request
from limits.limits import RateLimitItem
from limits.util import parse
from slowapi import Limiter

from src.core.config import Settings

_LIMITS: dict[str, tuple[RateLimitItem, RateLimitItem]] = {
    "login": (parse("20/5 minutes"), parse("10/15 minutes")),
    "refresh": (parse("60/minute"), parse("10/minute")),
    "activation": (parse("20/15 minutes"), parse("5/hour")),
}
_LIMITS["password_change"] = _LIMITS["login"]


class RateLimited(Exception):
    """A request exhausted one of its independently tracked limits."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        super().__init__("Rate limit exceeded.")


@dataclass(frozen=True)
class LimitStatus:
    """Safe HTTP metadata for the currently tightest limit."""

    amount: int
    remaining: int
    reset_at: float

    def headers(self, *, rejected: bool = False) -> dict[str, str]:
        """Return standard retry and rate-limit headers without key material."""
        result = {
            "X-RateLimit-Limit": str(self.amount),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(math.ceil(self.reset_at)),
        }
        if rejected:
            result["Retry-After"] = str(max(1, math.ceil(self.reset_at - time.time())))
        return result


class AuthRateLimits:
    """Apply an IP counter before parsing and an identity counter afterward."""

    def __init__(self, settings: Settings) -> None:
        self.limiter = Limiter(
            key_func=lambda request: request.client.host,
            strategy="moving-window",
            storage_uri=settings.rate_limit_storage_uri,
            headers_enabled=True,
            swallow_errors=False,
            in_memory_fallback_enabled=False,
        )
        if (
            not self.limiter.enabled
            or self.limiter._in_memory_fallback_enabled
            or self.limiter._swallow_errors
        ):
            raise ValueError("Rate limiting must be enabled without fallback")
        self._key = secrets.token_bytes(32)

    def _consume(self, item: RateLimitItem, scope: str, key: str) -> LimitStatus:
        """Charge one moving-window counter and raise with safe metadata."""
        allowed = self.limiter.limiter.hit(item, scope, key)
        reset_at, remaining = self.limiter.limiter.get_window_stats(item, scope, key)
        status = LimitStatus(item.amount, remaining, reset_at)
        if not allowed:
            raise RateLimited(status.headers(rejected=True))
        return status

    def check_ip(self, request: Request, operation: str) -> LimitStatus:
        """Count every request before JSON parsing or credential checks."""
        peer = request.client.host if request.client else "unknown-peer"
        ip_limit, _ = _LIMITS[operation]
        status = self._consume(ip_limit, f"{operation}:ip", peer)
        request.state.ip_limit_status = status
        return status

    def check_identity(
        self, request: Request, operation: str, identity: str
    ) -> LimitStatus:
        """Charge a keyed account, session, or token fingerprint."""
        keyed_identity = hmac.new(
            self._key, identity.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        _, identity_limit = _LIMITS[operation]
        status = self._consume(identity_limit, f"{operation}:identity", keyed_identity)
        return min(
            (request.state.ip_limit_status, status),
            key=lambda status: status.remaining / status.amount,
        )
