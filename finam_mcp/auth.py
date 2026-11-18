from __future__ import annotations

import asyncio
import logging
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .config import Settings

log = logging.getLogger("finam-mcp.auth")


class KeycloakJWTVerifier(TokenVerifier):
    """Fail-closed verifier for Keycloak access tokens."""

    def __init__(self, settings: Settings) -> None:
        self.issuer = str(settings.oauth_issuer_url).rstrip("/")
        self.audience = settings.oauth_audience or str(settings.mcp_resource_url).rstrip("/")
        self.required_scope = settings.mcp_required_scope
        self.jwks = PyJWKClient(
            str(settings.oauth_jwks_url),
            cache_keys=True,
            lifespan=300,
            timeout=settings.http_timeout_seconds,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        signing_key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "PS256", "ES256"],
            audience=self.audience,
            issuer=self.issuer,
            leeway=15,
            options={
                "require": ["exp", "iat", "iss", "sub", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self._decode, token)
            scopes = str(claims.get("scope", "")).split()
            if self.required_scope not in scopes:
                return None

            audience = claims.get("aud")
            resources = audience if isinstance(audience, list) else [audience]
            if self.audience not in resources:
                return None

            return AccessToken(
                token=token,
                client_id=str(claims.get("azp") or claims.get("client_id") or "unknown"),
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=self.audience,
                subject=str(claims["sub"]),
                claims=claims,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            log.warning("Rejected invalid OAuth access token")
            return None
        except Exception:
            log.exception("OAuth token verification unavailable")
            return None
