"""Strict access-token signature, claim, and rotation tests."""

import base64
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import jwt
import pytest

from src.auth.jwt_codec import InvalidAccessToken, JwtCodec, JwtConfigurationError
from src.core.config import Settings


def _key() -> str:
    """Return an independent test-only signing key."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _settings(**overrides: object) -> Settings:
    """Build a codec configuration without relying on local secrets."""
    values = {
        "jwt_signing_key_base64": _key(),
        "jwt_current_kid": "new",
        "jwt_issuer": "dreamplanner-test",
        "jwt_audience": "dreamplanner-api-test",
    }
    values.update(overrides)
    return Settings(**values)


def test_access_token_uses_only_approved_claims_and_hs256() -> None:
    """Issue a ten-minute JWT with an immutable user and session ID."""
    codec = JwtCodec(_settings())
    sid = uuid4()
    token = codec.issue(42, sid)
    assert codec.verify(token) == (42, sid)
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token,
        codec.keys["new"],
        algorithms=["HS256"],
        audience=codec.audience,
        issuer=codec.issuer,
    )
    assert header["alg"] == "HS256"
    assert header["kid"] == "new"
    assert set(claims) == {
        "sub",
        "iss",
        "aud",
        "iat",
        "nbf",
        "exp",
        "sid",
        "token_use",
        "jti",
    }
    assert claims["exp"] - claims["iat"] == 600
    assert claims["token_use"] == "access"


@pytest.mark.parametrize(
    ("change", "header"),
    [
        ({"iss": "other"}, None),
        ({"aud": "other"}, None),
        ({"token_use": "refresh"}, None),
        ({"sid": "bad"}, None),
        ({"sub": "0"}, None),
        ({"sub": "01"}, None),
        ({"sub": 42}, None),
        ({"exp": None}, None),
        ({"nbf": None}, None),
        ({"iat": None}, None),
        ({"exp": int(datetime.now(UTC).timestamp()) - 1}, None),
        ({"iat": int(datetime.now(UTC).timestamp()) + 60}, None),
        ({"exp": "overlong"}, None),
        ({}, {"kid": "unknown"}),
    ],
)
def test_invalid_claims_are_rejected(
    change: dict[str, object], header: dict[str, str] | None
) -> None:
    """Reject missing, wrong, expired, future, and overlong claims."""
    codec = JwtCodec(_settings())
    token = codec.issue(42, uuid4())
    claims = jwt.decode(
        token,
        codec.keys["new"],
        algorithms=["HS256"],
        audience=codec.audience,
        issuer=codec.issuer,
    )
    for name, value in change.items():
        if value is None:
            claims.pop(name)
        elif value == "overlong":
            claims[name] = claims["iat"] + 601
        else:
            claims[name] = value
    altered = jwt.encode(
        claims, codec.keys["new"], algorithm="HS256", headers=header or {"kid": "new"}
    )
    with pytest.raises(InvalidAccessToken):
        codec.verify(altered)


def test_wrong_signature_algorithm_and_tampering_are_rejected() -> None:
    """Never select the algorithm or key from attacker-controlled headers."""
    codec = JwtCodec(_settings())
    token = codec.issue(42, uuid4())
    wrong_key = jwt.encode(
        {"sub": "42"},
        secrets.token_bytes(32),
        algorithm="HS256",
        headers={"kid": "new"},
    )
    wrong_algorithm = jwt.encode(
        {"sub": "42"},
        secrets.token_bytes(48),
        algorithm="HS384",
        headers={"kid": "new"},
    )
    unsigned = jwt.encode(
        {"sub": "42"}, key=None, algorithm="none", headers={"kid": "new"}
    )
    for value in (
        wrong_key,
        wrong_algorithm,
        unsigned,
        token[:-1] + ("A" if token[-1] != "A" else "B"),
    ):
        with pytest.raises(InvalidAccessToken):
            codec.verify(value)


def test_previous_key_is_accepted_only_with_its_configured_id() -> None:
    """Allow a previous signing key during a bounded rotation period."""
    old_key = _key()
    old = JwtCodec(_settings(jwt_signing_key_base64=old_key, jwt_current_kid="old"))
    token = old.issue(7, uuid4())
    rotated = JwtCodec(
        _settings(jwt_previous_signing_key_base64=old_key, jwt_previous_kid="old")
    )
    assert rotated.verify(token)[0] == 7
    with pytest.raises(InvalidAccessToken):
        JwtCodec(_settings()).verify(token)


def test_missing_or_short_keys_fail_without_default_secrets() -> None:
    """Fail closed when signing configuration is absent or unsafe."""
    with pytest.raises(JwtConfigurationError):
        JwtCodec(_settings(jwt_signing_key_base64=None))
    with pytest.raises(JwtConfigurationError):
        JwtCodec(_settings(jwt_signing_key_base64=base64.b64encode(b"short").decode()))
