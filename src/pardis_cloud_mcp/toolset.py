"""Central tool catalog and startup selection for the MCP server."""
from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider
from pardis_cloud_mcp.cloud import ENTERPRISE_PROJECT_PATTERN, CloudError, CloudProject, CloudSettings, EcsPage, PardisCloud
from pardis_cloud_mcp.config import PlanningConfig
from pardis_cloud_mcp.deployment import Name, SourceAssessment, Target
from pardis_cloud_mcp.discovery import DiscoveryQuery, EcsDiscovery, EcsOptionsPage
from pardis_cloud_mcp.planning import PlanDocument, PlanningError, PlanningOptions, PlanningService


class Identity(TypedDict):
    username: str | None
    email: str | None
    department: str | None
    team: str | None
    venture: str | None
    vertical: str | None
    pardis_roles: list[str]
    subject: NotRequired[str]
    client_id: NotRequired[str]
    scopes: NotRequired[list[str]]


def register_tools(
    server: MCPServer,
    provider: RedisKeycloakOAuthProvider,
    cloud_settings: CloudSettings | None,
    enabled: tuple[str, ...] = ("*",),
    disabled: tuple[str, ...] = (),
    *,
    planning_config: PlanningConfig | None = None,
) -> None:
    cloud = None
    planning = PlanningService(provider, planning_config) if planning_config is not None else None

    def whoami(include_details: bool = False) -> Identity:
        """Return the signed-in user's username, email, organization attributes and pardis_roles. Set include_details=true only for debugging to also return subject, client_id and scopes. Roles are verified ID token claims captured at sign-in, not a live list of effective IAM permissions."""
        token = get_access_token()
        if token is None:
            raise ToolError("Authentication context is unavailable")
        claims = token.claims or {}
        identity = Identity(
            username=claims.get("preferred_username"), email=claims.get("email"),
            department=claims.get("Department"), team=claims.get("Team"),
            venture=claims.get("Venture"), vertical=claims.get("Vertical"),
            pardis_roles=claims.get("pardis_roles") if claims.get("pardis_roles") is not None else [],
        )
        if include_details:
            identity.update(subject=token.subject or "", client_id=token.client_id, scopes=token.scopes)
        return identity

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

    async def planning_call(action: str, **kwargs):
        token = get_access_token()
        if token is None or planning is None:
            raise ToolError("Deployment planning is unavailable in this session.")
        try:
            return await planning.call(action, token.token, **kwargs)
        except PlanningError as exc:
            raise ToolError(str(exc)) from None

    async def list_ecs_options(query: DiscoveryQuery) -> EcsOptionsPage:
        """Discover one page of real ECS deployment choices with the signed-in user's cloud credentials. resource_type: enterprise_projects, vpcs, subnets, security_groups, images, flavors, availability_zones. Start with enterprise_projects/vpcs/zones; subnets require a discovered vpc_id, and flavors can be narrowed by availability_zone. Keep IDs internal: users choose meaningful names, not JSON. IAM may deny a catalog independently of ECS access; explain the gap, never broaden permissions or infer grants from roles/team attributes. Missing endpoint errors are for the MCP operator. Images default to public active VM images; private/shared are explicit. EPS uses offset; other paginated catalogs use marker; zones return their full bounded catalog. A returned option is neither create permission nor reserved capacity, compatible placement, quota, price or approval. Do not create or modify resources. Results are advisory facts for the current draft, not an execution binding."""
        token = get_access_token()
        if token is None or cloud is None:
            raise ToolError("Cloud authentication context is unavailable")
        try:
            return await EcsDiscovery(cloud).list(token.token, query)
        except CloudError as exc:
            raise ToolError(str(exc)) from None

    async def get_deployment_options() -> PlanningOptions:
        """Get advisory CCI/ECS planning options for the signed-in user. These are configured suggestions, not live cloud grants, capacity or prices. Read the user's authorized source with your own file tools, then use prepare_deployment. Do not ask the user for JSON or cloud IDs."""
        return await planning_call("get_deployment_options")

    async def prepare_deployment(assessment: SourceAssessment, application_name: Name,
                                 target: Target = "cci") -> PlanDocument:
        """Create and save a readable advisory deployment document from your code review. Inspect the user's authorized source first; never run its scripts, send raw source/secrets, or invent missing facts. Fill the assessment yourself, use a short source label and omit unknown revision. Mark blockers or missing runtime information as needs_input. The user should not fill technical fields. Present document_markdown to the user; retain plan_id internally. Saves an encrypted draft only: no build, cloud writes, approval, pricing or quota verification. To revise, call again with the updated assessment/target."""
        return await planning_call("prepare_deployment", assessment=assessment,
                                   application_name=application_name, target=target)

    async def get_deployment_plan(
        plan_id: Annotated[str | None, Field(pattern=r"^plan-[a-f0-9]{32}$")] = None,
    ) -> PlanDocument:
        """Retrieve the signed-in user's own saved draft document, regardless of reader/full role changes. Omit plan_id for their latest plan; do not ask the user to copy an ID. Requires an active session and ownership. Expired or inaccessible plans must be prepared again. This does not deploy anything."""
        return await planning_call("get_deployment_plan", plan_id=plan_id)

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    stores_draft = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
    # Add new tools here: handler, cloud configuration requirement, MCP annotations.
    catalog = (
        (whoami, False, read_only),
        (list_projects, True, read_only),
        (list_ecs, True, read_only),
        (list_ecs_options, True, read_only),
        (get_deployment_options, False, read_only),
        (prepare_deployment, False, stores_draft),
        (get_deployment_plan, False, read_only),
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
    planning_names = {"get_deployment_options", "prepare_deployment", "get_deployment_plan"}
    if planning_config is None:
        if requested != {"*"} and selected & planning_names:
            raise ValueError("Selected planning tools require a planning block in YAML configuration")
        selected -= planning_names
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
