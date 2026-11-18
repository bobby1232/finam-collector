from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from finam_mcp.auth import KeycloakJWTVerifier
from finam_mcp.config import Settings


class SigningKey:
    def __init__(self, key):
        self.key = key


def settings() -> Settings:
    return Settings(
        collector_base_url="http://testserver",
        mcp_resource_url="https://testserver/mcp",
        oauth_issuer_url="https://testserver/oauth/realms/finam",
    )


def issue(private_key, **overrides):
    now = datetime.now(UTC)
    claims = {
        "iss": "https://testserver/oauth/realms/finam",
        "sub": "user-1",
        "azp": "chatgpt-finam-mcp",
        "aud": "https://testserver/mcp",
        "scope": "openid finam:read",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


@pytest.mark.asyncio
async def test_verifier_accepts_exact_issuer_audience_and_scope():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = KeycloakJWTVerifier(settings())
    verifier.jwks.get_signing_key_from_jwt = lambda _token: SigningKey(private_key.public_key())
    result = await verifier.verify_token(issue(private_key))
    assert result is not None
    assert result.subject == "user-1"
    assert result.resource == "https://testserver/mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://attacker.invalid/mcp"},
        {"iss": "https://attacker.invalid/realms/finam"},
        {"scope": "openid"},
        {"exp": 1},
    ],
)
async def test_verifier_rejects_wrong_or_expired_claims(overrides):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = KeycloakJWTVerifier(settings())
    verifier.jwks.get_signing_key_from_jwt = lambda _token: SigningKey(private_key.public_key())
    assert await verifier.verify_token(issue(private_key, **overrides)) is None
