# D0: Deployment contracts and offline examples

Implemented stage: D0 of [the development roadmap](APPLICATION_DEPLOYMENT_PLAN.md).
This stage adds data models and an offline validator. It does not register new MCP tools, run code
review automatically, generate a deployment document, execute builds, store plans, or contact Pardis.
Existing authentication, federation, tool configuration and cloud permissions are unchanged.

## Contracts

Models live in `src/pardis_cloud_mcp/deployment.py` and reject unknown fields and implicit type coercion.

| Model | Meaning |
| --- | --- |
| `SourceAssessment` | Agent-provided review data tied to a repository ID and full 40/64-character commit revision |
| `ApplicationBinding` | Internal application owner, repository, target binding and explicit role-sharing metadata |
| `RolePolicy` | Operator-owned role grants referencing declared targets, resource presets and actions |
| `DeploymentPlan` | Draft containing application/assessment/target/preset, rationale, steps and unresolved decisions |

All examples use synthetic identities and resource IDs. Their revision strings are placeholders,
not verified references to this repository. D2/D5 will establish source provenance and verify it before
execution. A structurally valid model is not proof of ownership, authenticity, authorization, available
capacity, approval, or deployability. In particular, actor and role policy records are trusted internal
inputs in future stages; accepting a JSON shape does not authorize caller-supplied identity.

- Authorization decisions and combining multiple roles are D1 work. Organization attributes cannot
  appear as role-policy rules. Do not copy the example policy into live `config.yaml`; it is unsupported.
- `shared_roles: []` documents the initial owner-only default; runtime enforcement arrives in D1.
- `python-http` is the initial fixture stack. CCI/ECS are accepted draft targets, not implemented adapters.
- Resource quantities are per instance/replica: CPU in millicores, memory in MiB, disk in GiB, and a
  separate replica count. Numeric schema bounds are input guardrails, not Pardis flavor support, quota
  checks, per-user policy limits or pricing. D1 restricts allowed presets and later stages check capacity.
- Presets: `small-cci` = 500m CPU, 512 MiB, 0 GiB requested persistent disk, one replica;
  `small-ecs` = 1000m CPU, 1024 MiB, 20 GiB disk, one instance. Both are examples only.
- Environment requirements contain names and optional secret references, never values. Unknown fields
  such as `value`, `access_token`, actor overrides and role overrides in assessments are rejected.
  This is not a general secret scanner: free-text observations must also be sanitized by their producer.
- Build/start observations are review text, never executable commands. Observed findings require file
  evidence; inferred and unknown findings are labeled. Evidence paths must be relative to the repo.
- Missing runtime port, health route, required secret reference, missing inputs or blocker findings
  prevent `ready_for_planning`. An incomplete application can still have a valid `needs_input` assessment.
- Every D0 plan is `draft`, `executable: false`, with cost/quota `unknown`, approval `not_requested`
  and at least one unresolved item. There is no approval API or persisted plan ID yet.
- Canonical plan hashes, expiry, document rendering and plan storage are D3 work. Current model
  instances are not an immutable or signed approval record.

## Run the offline demo

Run from the repository root. No `.env`, Redis, Central Auth login, cloud credentials or running MCP
server is required. Install the existing locked dependencies if necessary:

```bash
uv sync --locked --extra dev
```

Validate a source assessment and inspect its normalized JSON:

```bash
uv run --locked python examples/deployment/validate.py assessment examples/deployment/assessment.json
```

Expected: exit code 0, `readiness: ready_for_planning`, port 8080, health path `/health`.
This reads the prepared review fixture; it does not independently analyze or execute `app.py`.

Validate both target plan examples:

```bash
uv run --locked python examples/deployment/validate.py plan examples/deployment/plan.cci.json
uv run --locked python examples/deployment/validate.py plan examples/deployment/plan.ecs.json
uv run --locked python examples/deployment/validate.py policy examples/deployment/role-policy.json
uv run --locked python examples/deployment/validate.py application examples/deployment/application.json
```

Expected: exit code 0. Plans contain the target-specific binding and size, with `executable: false`.
The returned JSON is the D0 contract preview; the human-readable deployment document comes in D3.

Compare a valid review of an incomplete app with an invalid contract:

```bash
uv run --locked python examples/deployment/validate.py assessment examples/deployment/assessment.needs-input.json
uv run --locked python examples/deployment/validate.py assessment examples/deployment/assessment.invalid.json
```

The first succeeds with `needs_input` and explicit blockers. The second intentionally exits with code 1
because it contains a branch name instead of a commit, an invalid port and a synthetic secret `value`.
Errors contain field paths/types but not the rejected values. Files larger than 128 KiB are rejected.
Do not test with real secrets; successful output includes accepted review text.

If dependencies are already installed in `.venv`, use `.venv/bin/python` in place of
`uv run --locked python` to run these commands entirely offline.

## Run the fixture application, optionally

This separate command deliberately starts the synthetic HTTP application; validation never starts it:

```bash
uv run --locked python examples/deployment/python-http/app.py
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8080/health
```

Expected JSON: `{"status": "ok", "application": "sample-http"}`. Stop with Ctrl+C.
The fixture binds port 8080 on all interfaces for eventual container use; run it only in the intended
test environment and avoid a port already in use. It is a standard-library demo, not a production server.
`incomplete-http/app.py` intentionally lacks HTTP startup and health handling; inspect it as review input.

## Automated tests and review gate

```bash
uv run --locked pytest tests/test_deployment.py -q
uv run --locked pytest -q
```

Coverage includes valid example round-trips, explicit draft defaults, absent revision, identity/role
injection fields, unexpected secret values, invalid types and sizes, source paths, false readiness,
broken policy references, CCI/ECS namespace mismatch, app/plan reference mismatch, safe CLI errors,
oversized input and the sample health handler. Existing HTTP tests verify that the tool catalog is unchanged.

Review the example assessment, policy and both plans before D1. Confirm the names and fields are useful,
then implement actual role decisions and `get_deployment_options`. No CCI/ECS write test is part of D0.
