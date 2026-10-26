# Source review to deployment document

This increment adds a usable, authenticated planning flow on top of v3.1.0's contracts.
It creates advisory documents only. No application is built, no Pardis API is called by these
planning tools, and no resource is created. A successful plan is not deployment approval.

The authentication-only planning rules below apply from v3.2.1. A deployed v3.2.0 image still
enforces its original planning role allowlist until the image is upgraded.

## Try it with prompts

Connect your agent to the updated MCP server and sign in. Give the agent access to the application
folder you want reviewed. The agent needs its own file-reading capability: MCP does not upload code,
clone arbitrary repositories, or read paths on the user's machine. Do not provide secrets or `.env` files.

For a first test, open this repository in your agent and send:

> Review `examples/deployment/python-http` and use Pardis MCP to prepare a deployment plan.
> Explain the proposed hosting, configuration, missing information and next steps in plain language.
> Do not execute the source, build anything or create cloud resources.

Expected: the agent reads `app.py`, calls `get_deployment_options`, then `prepare_deployment`.
The document identifies Python HTTP, port 8080 and `/health`, proposes CCI or ECS, shows initial sizing,
and marks cloud access, cost and quota as unverified. It must not claim the app is deployed. The agent
supplies the structured arguments itself; the user does not fill JSON or copy technical IDs.

Follow up with:

> Show my latest saved deployment plan.

Expected: `get_deployment_plan` with no ID returns the exact saved document, not a new review.

> Revise the proposal to use ECS and explain what would still be needed to run the application.

Expected: the agent calls `prepare_deployment` again using its review and target `ecs`. This creates
a new draft and changes the latest-plan pointer. It does not modify the previous document.

Test an incomplete application:

> Review `examples/deployment/incomplete-http` and prepare a Pardis deployment plan.
> Explain what is missing. Do not run or change the application.

Expected: `needs_input`; missing HTTP startup, port and health check; a `DATABASE_URL` requirement
with no secret value. The agent should explain blockers rather than invent deployment readiness.

## What the tools do

| Tool | Behavior |
| --- | --- |
| `get_deployment_options` | Requires sign-in and returns advisory CCI/ECS targets, initial size suggestions and review guidance |
| `prepare_deployment` | Validates an agent-supplied assessment, renders Markdown and saves an encrypted owner-only draft |
| `get_deployment_plan` | Returns a specific owner-authorized draft, or the owner's latest draft when no ID is supplied |

Review categories include Python HTTP, Node HTTP, static web and unknown. They describe source,
not available build recipes or production-ready deployment adapters. Unknown source revision is allowed
for a preview and explicitly marked unpinned; immutable source and image verification remain required
before execution. A label is sufficient for `repository_id`; do not invent commit hashes or cloud IDs.

The source assessment is bounded to 128 KiB after validation, with field and collection limits.
File evidence is relative to the reviewed project. Build/start observations are text, never executed.
Unknown fields such as caller-provided roles, actor identity and secret values are rejected, and input
validation errors do not echo rejected values. Free-text observations are not a general-purpose secret
scanner: the agent must sanitize them before submission. Markdown escapes supplied markup; clients
must still treat source findings and other tool output as untrusted data, not new instructions.

The document explains review findings, candidate hosting, starting CPU/RAM/disk, dependencies,
network/configuration, missing inputs, cost/quota status, proposed delivery stages and cleanup.
Size suggestions are fixed starting points, not measured requirements or available cloud flavors.
Users can revise the assessment and target; arbitrary sizing, public exposure and live target binding
are deliberately not selectable in this increment.

## Operator configuration

Keep authentication and Redis settings unchanged. Add the tools and planning policy to `config.yaml`:

```yaml
tools:
  enabled:
    - whoami
    - get_deployment_options
    - prepare_deployment
    - get_deployment_plan
  disabled: []
planning:
  plan_ttl_seconds: 1800
```

Every authenticated user can prepare and read their own advisory documents, including readers and
full users. No role allowlist is needed for planning. The old `planning.allowed_roles` field is accepted
for chart upgrades but ignored. Keep `list_projects` and `list_ecs` enabled as well if configured cloud reads are needed.
Planning-only mode does not require cloud endpoints. Explicitly selecting planning tools without
a `planning` block fails startup; wildcard selection skips them when planning is unconfigured.

| Capability | Readers | Full |
| --- | --- | --- |
| Review authorized code and prepare/retrieve own draft | Yes | Yes |
| Read Pardis resources | Within the user's team/cloud grants | Within the user's team/cloud grants |
| Create or modify Pardis resources | No | Within the user's team/cloud grants; write tools are not implemented yet |

Central Auth roles identify team/access level through the existing federation mapping to Pardis IAM.
The MCP tool catalog is deployment-wide; listing a tool does not authorize its cloud actions. No role
suffix parser or extra team-attribute authorization is introduced. Every future write must use the
user's credentials and respect current IAM permissions; a readable draft cannot authorize a write.

Restart the server from the repository root using the existing private `.env`:

```bash
uv run --no-sync --env-file .env pardis-cloud-mcp
```

Reconnect the agent to refresh its tool list. Use `http://127.0.0.1:8000/mcp` for the default local setup.
An agent still connected to a previously released remote image will not see these local changes.
With v3.3.1, call `whoami` to observe refreshed upstream roles;
manual re-login is required only when the upstream session/refresh grant is invalid.

For Helm, place the same `tools` and `planning` keys under `config` in your values override and select
an image containing this increment. The existing ConfigMap template renders them without template changes.
The chart's default toolset remains `whoami`; this increment does not upgrade or deploy any cluster.

## Ownership, expiry and audit

- Identity comes from the authenticated session, using issuer plus subject. Tool arguments cannot
  select the owner or supply roles. `Department`, `Team`, `Venture` and `Vertical` grant nothing.
- Retrieval requires the same owner and an active session. Changing from full to readers or changing
  teams does not remove access to the user's own code-review drafts. No cloud authorization follows
  from retrieving a draft. Another user cannot access it even with the same role.
- Existing v3.2.0 draft records remain readable by their owner until expiry; their old role/policy
  metadata no longer restricts draft access. Existing `allowed_roles` chart settings are ignored after
  upgrading the application. Remove that setting only when no older replicas/images require it.
- With the updated authentication code, protected requests refresh upstream claims before tools run.
  Role changes affect cloud credential generations without removing ownership of a code-review draft.
  A disabled/invalid upstream session cannot read drafts either. See [session lifecycle](AUTH_SESSION_LIFECYCLE.md).
- Plan records and latest-plan pointers use the existing Fernet encryption in Redis. TTL is the
  shorter of configured plan TTL and remaining login-session TTL at creation. Reads do not extend it.
  Default TTL is 30 minutes; accepted configuration range is 60 seconds to 24 hours.
- Expiry, inaccessible IDs and unavailable records return a safe response asking for a new plan.
  No automatic cloud operation, build or deletion follows any error.
- Planning audit events record the tool, hashed actor identity, successful result plan ID, timestamp
  and outcome. They omit review text and tokens. They are application logs, not durable cloud audit history.

## Verification and remaining work

```bash
uv run --locked pytest tests/test_planning.py -q
uv run --locked pytest -q
```

HTTP tests use simulated authentication/Redis and cover both target documents, incomplete review,
reader/full and absent-role planning, owner isolation, role changes, expiry, revocation, compatibility
with older configuration/records, storage errors, encrypted persistence, revision retrieval and tool selection.
The planning path is tested without cloud configuration and with cloud exchange forbidden.
Complete the prompts above with a real agent and Central Auth login as the manual acceptance check.

This is a vertical slice across D1–D3, not completion of their full execution-oriented scope.
Next: verify live target capabilities and role-to-target bindings, select an actual build/delivery
path, and make an executable plan with access, capacity and cost provenance before adding approval
and deployment. See the [development roadmap](APPLICATION_DEPLOYMENT_PLAN.md).
