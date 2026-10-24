"""Central tool catalog and startup selection for the MCP server."""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider
from pardis_cloud_mcp.cloud import ENTERPRISE_PROJECT_PATTERN, CloudError, CloudProject, CloudSettings, EcsPage, PardisCloud


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
        """Return the signed-in user's identity."""
        token = get_access_token()
        if token is None:
            raise ToolError("Authentication context is unavailable")
        claims = token.claims or {}
        return Identity(
            subject=token.subject or "", username=claims.get("preferred_username"),
            email=claims.get("email"), client_id=token.client_id, scopes=token.scopes,
        )

    async def list_projects() -> list[CloudProject]:
        """Discover regional IAM projects, not enterprise projects. An empty list does not prove lack of ECS access: enterprise-project-only users should call list_ecs with the deployment's configured default. If no default is configured, ask the operator to set PARDIS_PROJECT_ID; supplying an arbitrary project ID will not bypass discovery."""
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
        enterprise_project_id: Annotated[str, Field(pattern=ENTERPRISE_PROJECT_PATTERN)] = "all_granted_eps",
    ) -> EcsPage:
        """List one page of ECSs using the signed-in user's permissions. Omit project_id to use the configured regional IAM project even when list_projects is empty, or the sole discovered project. Other project IDs must be discovered. enterprise_project_id defaults to all_granted_eps (all enterprise projects granted to this user); use a specific enterprise project ID or 0 for default to narrow the query. It never grants access. Keep both project selections unchanged when passing next_marker. No resources are changed."""
        token = get_access_token()
        if token is None or cloud is None:
            raise ToolError("Cloud authentication context is unavailable")
        try:
            return await cloud.list_ecs(token.token, limit, marker, project_id, enterprise_project_id)
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
