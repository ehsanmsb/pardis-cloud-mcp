from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient
from pydantic import AnyHttpUrl, BaseModel

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings


@dataclass(frozen=True)
class Settings:
    issuer_url: str
    audience: str
    resource_url: str
    required_scope: str = "mcp:tools"
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        required = ("OIDC_ISSUER_URL", "OIDC_AUDIENCE", "MCP_RESOURCE_URL")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            issuer_url=os.environ["OIDC_ISSUER_URL"].rstrip("/"),
            audience=os.environ["OIDC_AUDIENCE"],
            resource_url=os.environ["MCP_RESOURCE_URL"],
            required_scope=os.getenv("MCP_REQUIRED_SCOPE", "mcp:tools"),
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8000")),
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer_url}/protocol/openid-connect/certs"


class KeycloakJWTVerifier(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks_client = PyJWKClient(settings.jwks_url, cache_keys=True)

    def _decode(self, token: str) -> dict[str, Any]:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.settings.audience,
            issuer=self.settings.issuer_url,
            options={"require": ["exp", "iss", "sub", "aud"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except (jwt.PyJWTError, OSError, ValueError):
            return None

        scopes = str(claims.get("scope", "")).split()
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp", self.settings.audience)),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.settings.audience,
            subject=str(claims["sub"]),
            claims=claims,
        )


class Identity(BaseModel):
    subject: str
    username: str | None
    email: str | None
    client_id: str
    scopes: list[str]


def build_server(settings: Settings) -> MCPServer:
    server = MCPServer(
        "Pardis Cloud",
        instructions="Authenticated tools for Pardis Cloud.",
        token_verifier=KeycloakJWTVerifier(settings),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            required_scopes=[settings.required_scope],
            validate_token_resource=False,
        ),
    )

    @server.tool(description="Return the identity from the authenticated Keycloak access token.")
    def whoami() -> Identity:
        token = get_access_token()
        if token is None:
            raise RuntimeError("Authentication context is unavailable")

        claims = token.claims or {}
        return Identity(
            subject=token.subject or "",
            username=claims.get("preferred_username"),
            email=claims.get("email"),
            client_id=token.client_id,
            scopes=token.scopes,
        )

    return server


def main() -> None:
    settings = Settings.from_env()
    build_server(settings).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
