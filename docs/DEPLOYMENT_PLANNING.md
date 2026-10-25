# Source review to deployment document

This increment adds a usable, authenticated planning flow on top of v3.1.0's contracts.
It creates advisory documents only. No application is built, no Pardis API is called by these
planning tools, and no resource is created. A successful plan is not deployment approval.

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
| `get_deployment_options` | Checks planning access and returns advisory CCI/ECS targets, initial size suggestions and review guidance |
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
  allowed_roles:
    - team-hr-readonly
    - team-marketing-readonly
  plan_ttl_seconds: 1800
```

These two role names are the approved local test policy. For another environment, explicitly configure
the desired Central Auth `pardis_roles`. This permission allows document preparation only and never
adds IAM privileges. Keep `list_projects` and `list_ecs` enabled as well if configured cloud reads are needed.
Planning-only mode does not require cloud endpoints. Explicitly selecting planning tools without
`planning.allowed_roles` fails startup; wildcard selection skips them when planning is unconfigured.

Restart the server from the repository root using the existing private `.env`:

```bash
uv run --no-sync --env-file .env pardis-cloud-mcp
```

Reconnect the agent to refresh its tool list. Use `http://127.0.0.1:8000/mcp` for the default local setup.
An agent still connected to a previously released remote image will not see these local changes.
If roles changed upstream, sign out and sign in again, then check `whoami`.

For Helm, place the same `tools` and `planning` keys under `config` in your values override and select
an image containing this increment. The existing ConfigMap template renders them without template changes.
The chart's default toolset remains `whoami`; this increment does not upgrade or deploy any cluster.

## Ownership, expiry and audit

- Identity comes from the authenticated session, using issuer plus subject. Tool arguments cannot
  select the owner or supply roles. `Department`, `Team`, `Venture` and `Vertical` grant nothing.
- A user needs at least one allowed role. Multiple matching roles permit the same advisory tools;
  they do not enlarge sizes or grant access to additional cloud targets.
- Retrieval requires the same owner, an active session, current planning permission, an overlap
  with the plan's original allowed roles and an unchanged planning policy hash. HR-to-Marketing
  role changes therefore prevent retrieving an HR-only draft, even for the same person.
- Checks use the roles captured in the current MCP login session. Upstream role changes are not
  polled continuously; a fresh login is required. Another user cannot access a plan even with the same role.
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
role denial, organization attributes not granting access, owner isolation, role changes, expiry,
revocation, policy changes, storage errors, encrypted persistence, revision retrieval and tool selection.
The planning path is tested without cloud configuration and with cloud exchange forbidden.
Complete the prompts above with a real agent and Central Auth login as the manual acceptance check.

This is a vertical slice across D1–D3, not completion of their full execution-oriented scope.
Next: verify live target capabilities and role-to-target bindings, select an actual build/delivery
path, and make an executable plan with access, capacity and cost provenance before adding approval
and deployment. See the [development roadmap](APPLICATION_DEPLOYMENT_PLAN.md).
