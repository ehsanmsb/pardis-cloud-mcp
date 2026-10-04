from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl, BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider


@dataclass(frozen=True)
class Settings:
    keycloak_issuer_url: str
    keycloak_client_id: str
    keycloak_client_secret: str
    oauth_issuer_url: str
    resource_url: str
    redis_url: str
    token_encryption_key: str
    required_scope: str = "mcp:tools"
    redis_prefix: str = "pardis-mcp"
    host: str = "127.0.0.1"
    port: int = 8000
    login_ttl_seconds: int = 300
    authorization_code_ttl_seconds: int = 60
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 28_800

    @classmethod
    def from_env(cls) -> Settings:
        required = (
            "OIDC_ISSUER_URL",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "MCP_OAUTH_ISSUER_URL",
            "MCP_RESOURCE_URL",
            "REDIS_URL",
            "TOKEN_ENCRYPTION_KEY",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        encryption_key = os.environ["TOKEN_ENCRYPTION_KEY"]
        try:
            Fernet(encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

        return cls(
            keycloak_issuer_url=os.environ["OIDC_ISSUER_URL"].rstrip("/"),
            keycloak_client_id=os.environ["OIDC_CLIENT_ID"],
            keycloak_client_secret=os.environ["OIDC_CLIENT_SECRET"],
            oauth_issuer_url=os.environ["MCP_OAUTH_ISSUER_URL"].rstrip("/"),
            resource_url=os.environ["MCP_RESOURCE_URL"],
            redis_url=os.environ["REDIS_URL"],
            token_encryption_key=encryption_key,
            required_scope=os.getenv("MCP_REQUIRED_SCOPE", "mcp:tools"),
            redis_prefix=os.getenv("REDIS_PREFIX", "pardis-mcp"),
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8000")),
        )

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.keycloak_issuer_url}/protocol/openid-connect/auth"

    @property
    def token_endpoint(self) -> str:
        return f"{self.keycloak_issuer_url}/protocol/openid-connect/token"

    @property
    def jwks_url(self) -> str:
        return f"{self.keycloak_issuer_url}/protocol/openid-connect/certs"

    @property
    def keycloak_redirect_uri(self) -> str:
        return f"{self.oauth_issuer_url}/oauth/keycloak/callback"


class Identity(BaseModel):
    subject: str
    username: str | None
    email: str | None
    client_id: str
    scopes: list[str]


def build_server(
    settings: Settings,
    provider: RedisKeycloakOAuthProvider | None = None,
) -> MCPServer:
    oauth_provider = provider or RedisKeycloakOAuthProvider(settings)
    server = MCPServer(
        "Pardis Cloud",
        instructions="Authenticated tools for Pardis Cloud.",
        auth_server_provider=oauth_provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.oauth_issuer_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            required_scopes=[settings.required_scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[settings.required_scope],
                default_scopes=[settings.required_scope],
            ),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=True,
        ),
    )

    @server.custom_route("/oauth/keycloak/callback", methods=["GET"])
    async def keycloak_callback(request: Request) -> Response:
        return await oauth_provider.handle_keycloak_callback(request)

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        try:
            await oauth_provider.redis.ping()
        except Exception:
            return JSONResponse({"status": "unhealthy", "redis": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "redis": "up"})

    @server.tool(description="Return the identity authenticated through Keycloak.")
    def whoami() -> Identity:
        token = get_access_token()
        if token is None:
            raise RuntimeError("Authentication context is unavailable")

        claims: dict[str, Any] = token.claims or {}
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
