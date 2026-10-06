from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider
from pardis_cloud_mcp.auth_page import home_page, not_found_page
from pardis_cloud_mcp.cloud import CloudSettings
from pardis_cloud_mcp.config import load_config
from pardis_cloud_mcp.toolset import register_tools


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
    cloud: CloudSettings | None = None
    enabled_tools: tuple[str, ...] = ("*",)
    disabled_tools: tuple[str, ...] = ()

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

        if "MCP_ENABLED_TOOLS" in os.environ or "MCP_DISABLED_TOOLS" in os.environ:
            raise ValueError("Move MCP_ENABLED_TOOLS/MCP_DISABLED_TOOLS from the environment to tools.enabled/tools.disabled in config.yaml")
        config = load_config(os.getenv("MCP_CONFIG_FILE", "config.yaml"))

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
            cloud=CloudSettings.from_env(),
            enabled_tools=tuple(config.tools.enabled),
            disabled_tools=tuple(config.tools.disabled),
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


async def _not_found(_: Request, __: Exception) -> Response:
    return not_found_page()


class PardisMCPServer(MCPServer):
    def streamable_http_app(self, **kwargs: Any) -> Starlette:
        app = super().streamable_http_app(**kwargs)
        app.add_exception_handler(404, _not_found)
        return app


def build_server(
    settings: Settings,
    provider: RedisKeycloakOAuthProvider | None = None,
) -> MCPServer:
    oauth_provider = provider or RedisKeycloakOAuthProvider(settings)
    server = PardisMCPServer(
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

    @server.custom_route("/", methods=["GET"])
    async def home(_: Request) -> Response:
        return home_page(settings.resource_url)

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

    register_tools(server, oauth_provider, settings.cloud, settings.enabled_tools, settings.disabled_tools)
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
