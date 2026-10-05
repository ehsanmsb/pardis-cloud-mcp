"""Central tool catalog and startup selection for the MCP server."""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider
from pardis_cloud_mcp.cloud import CloudError, CloudProject, CloudSettings, EcsPage, PardisCloud


class Identity(BaseModel):
    subject: str
    username: str | None
    email: str | None
    client_id: str
    scopes: list[str]


def register_tools(
    server: MCPServer,
    provider: RedisKeycloakOAuthProvider,
    cloud_settings: CloudSettings | None,
    enabled: tuple[str, ...] = ("*",),
    disabled: tuple[str, ...] = (),
) -> None:
    cloud = None

    def whoami() -> Identity:
        """Return the identity authenticated through Keycloak."""
        token = get_access_token()
        if token is None:
            raise ToolError("Authentication context is unavailable")
        claims = token.claims or {}
        return Identity(
            subject=token.subject or "", username=claims.get("preferred_username"),
            email=claims.get("email"), client_id=token.client_id, scopes=token.scopes,
        )

    async def list_projects() -> list[CloudProject]:
        """List IAM projects accessible to the signed-in user. ECS operations use this deployment's configured regional endpoint; choose a project in that region."""
        token = get_access_token()
        if token is None or cloud is None:
            raise ToolError("Cloud authentication context is unavailable")
        try:
            return await cloud.list_projects(token.token)
        except CloudError as exc:
            raise ToolError(str(exc)) from None

    async def list_ecs(
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        marker: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9-]{1,128}$")] = None,
        project_id: Annotated[str | None, Field(pattern=r"^[a-fA-F0-9]{32}$")] = None,
    ) -> EcsPage:
        """List one page of ECS instances using the signed-in user's permissions and the configured regional endpoint. Choose project_id from list_projects; if omitted, use the configured default or the sole accessible project. Pass next_marker to fetch another page. No resources are changed."""
        token = get_access_token()
        if token is None or cloud is None:
            raise ToolError("Cloud authentication context is unavailable")
        try:
            return await cloud.list_ecs(token.token, limit, marker, project_id)
        except CloudError as exc:
            raise ToolError(str(exc)) from None

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    # Add new tools here: handler, cloud configuration requirement, MCP annotations.
    catalog = (
        (whoami, False, read_only),
        (list_projects, True, read_only),
        (list_ecs, True, read_only),
    )
    known = {handler.__name__ for handler, _, _ in catalog}
    requested = set(enabled)
    excluded = set(disabled)
    if "*" in requested and requested != {"*"}:
        raise ValueError('tools.enabled must contain "*" alone or a list of tool names')
    unknown = (requested - {"*"} | excluded) - known
    if unknown:
        raise ValueError(f"Unknown MCP tools: {', '.join(sorted(unknown))}. Available: {', '.join(sorted(known))}")
    selected = (known if requested == {"*"} else requested) - excluded
    cloud_names = {handler.__name__ for handler, needs_cloud, _ in catalog if needs_cloud}
    if cloud_settings is None:
        if requested != {"*"} and selected & cloud_names:
            raise ValueError("Selected cloud tools require PARDIS_IAM_ENDPOINT, PARDIS_ECS_ENDPOINT and PARDIS_IDP_ID")
        selected -= cloud_names
    elif selected & cloud_names:
        cloud = PardisCloud(cloud_settings, provider)

    for handler, _, annotations in catalog:
        if handler.__name__ in selected:
            server.add_tool(handler, annotations=annotations)
