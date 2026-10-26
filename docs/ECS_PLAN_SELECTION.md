# Save real ECS choices in a deployment draft

Available from v3.4.0; authenticated live Pardis acceptance is pending.
This increment extends `prepare_deployment`, without adding another tool or any cloud write.
The generic CCI/ECS document flow remains available when no cloud selection is supplied.

## What changed

An agent can now discover catalogs, explain choices by name, and send an `ecs_selection` when
preparing an ECS plan. MCP independently queries the catalogs again with the current user's
temporary Pardis credentials. It does not accept a caller's cloud snapshot, sizing facts, roles,
owner, permission claims or verification booleans as evidence.

The selection contains a regional project route (optional when configured/discoverable), one specific
Enterprise Project, VPC, subnet, security group, VM image and category, flavor, AZ and requested root
disk size. `all_granted_eps` is a list filter, not a deployment destination, and is rejected here.
IDs stay internal to tool calls; a nontechnical user chooses meaningful names through the agent.

Backend checks:

- Each exact ID must appear in a fresh catalog under the user's credentials and regional route.
  Missing or inaccessible catalogs fail the selected draft; they do not trigger permission changes
  or fallbacks to administrator credentials. EPS visibility is not an enumeration of all grants:
  an EPS-denied reader can still prepare a generic document, but cannot verify this complete selection.
- The subnet must belong to the selected VPC. A security group with a reported VPC must match too;
  an absent VPC field is recorded as an unresolved compatibility check.
- Flavors are queried for the selected AZ. Disabled flavors and reported unusable EP/VPC/subnet/image
  statuses are rejected. Missing statuses are explicitly unverified.
- CPU and RAM come from flavor metadata, never from tool arguments. Requested disk and RAM must meet
  the image's reported `min_disk` and `min_ram`. Absent image minima are marked unverified; malformed
  numeric metadata is rejected. Current sizing bounds: 1–64 vCPU, 128–262144 MiB RAM and 10–1024 GiB
  root disk, with one instance. Flavor-local disk is not treated as the requested root volume.
- Shared networks may belong to another Enterprise Project. Such ownership differences alone are
  not rejected and never establish permission to deploy into the chosen Enterprise Project.
- Verification is bounded to 10 pages of 100 items per catalog and 45 seconds overall. Repeated
  cursors, scan exhaustion, timeout and upstream errors fail closed. An ID on a later page is not
  rejected merely because it was absent on page one. Detail lookups can replace scans if real catalog
  size or latency exceeds these bounds; no direct-detail API compatibility is assumed in this increment.
- A revoked session or changed authorization generation during validation prevents a successful save.

The stored `ecs_selection` contains normalized IDs, allowlisted names/metadata, effective resources,
catalog audit IDs, completion time, checks and explicit unknowns. Markdown is rendered from this same
backend-produced snapshot and the source assessment. The plan hash includes the snapshot and document.
Cloud names and source observations are untrusted data and are escaped in Markdown.

Every plan remains `status=draft`, `executable=false`, `write_access=not_checked`. CPU architecture,
firmware, virtualization, security rules, reachability, disk type, stock, capacity, prices, quota and
write grants are not fully verified. A recorded selection does not reserve anything or make a plan
execution-ready. Approval, builds and provisioning remain later stages.

## Storage and access

The existing encrypted Redis plan record stores the selection. There is no new database, key,
secret or role allowlist. TTL is the shorter of `planning.plan_ttl_seconds` (default 30 minutes) and
remaining MCP session lifetime, measured after verification. Reading does not extend TTL or query
cloud again. Retrieval returns the exact saved document and its point-in-time snapshot.

Reader/full users can prepare and retrieve their own plans. Another user cannot retrieve the plan,
even with identical roles. Role changes do not remove ownership of an existing draft, but its old
snapshot is never a current cloud grant. Future execution must revalidate current permissions and
choices. Disabled/revoked sessions cannot retrieve plans. Revising any choice creates a new plan;
previous plans remain unchanged until expiry. Failed selection checks preserve the previous latest plan.

## Operator setup

Keep the existing authentication settings. Enable these tools under `tools.enabled` (Helm: `config.tools.enabled`):

```yaml
tools:
  enabled:
    - whoami
    - list_ecs_options
    - get_deployment_options
    - prepare_deployment
    - get_deployment_plan
  disabled: []
planning:
  plan_ttl_seconds: 1800
```

Configure the existing IAM/ECS/IdP settings and verified `PARDIS_VPC_ENDPOINT`, `PARDIS_IMS_ENDPOINT`,
`PARDIS_EPS_ENDPOINT` origins. Use the **VPC** service, not **VPC Endpoint**: these are distinct services.
Regional routing follows `PARDIS_PROJECT_ID` or a sole discovered IAM project. Never guess endpoints
or regional project IDs. See [discovery setup](ECS_DISCOVERY.md).

Selection checking is available only when cloud settings and `list_ecs_options` are enabled.
Disabling that tool also disables selected-cloud checks inside `prepare_deployment`; generic drafts
still work without cloud configuration. Existing Helm ConfigMap/env templates support these settings;
no chart template, dependency, callback or Secret changes are required for this increment.

The IMS query now includes `enterprise_project_id=all_granted_eps` by default, and accepts a specific
EP filter during discovery. Huawei documents this parameter as necessary for EP-only authorization;
see [Querying Images](https://support.huaweicloud.com/intl/en-us/api-ims/ims_03_0602.html).
This filter grants nothing and still requires Pardis compatibility testing.

## Manual acceptance through prompts

Run the updated local server (or deploy an image containing this increment) and reconnect the agent
to refresh its tool schema. Point it to a sample application folder it is authorized to read.

1. Verify the catalog and identity:

   > Use Pardis MCP to show my identity and roles. Discover the Enterprise Projects, VPCs,
   > availability zones, VM images and flavors visible to me. List subnets for an appropriate VPC
   > and the security groups visible to me. Explain missing catalogs or permission errors.
   > Keep all cloud operations read-only. Do not change permissions or resources.

2. Prepare a real selection (for the repository fixture, use this prompt):

   > Review examples/deployment/python-http without running its code. Prepare an ECS deployment
   > plan using real Pardis options discovered with my account. Explain the options by name and
   > ask me to choose if needed. Use prepare_deployment with ecs_selection so the backend rechecks
   > and saves the exact choices and actual CPU/RAM/root disk sizing. Show the returned document
   > and unresolved checks. Do not build, deploy, modify resources or change permissions.

   Expected: the agent supplies assessment/IDs, backend reads catalogs, and the returned document
   shows names, actual flavor sizing and explicit unknowns. A catalog failure must remain visible;
   the agent must not silently replace chosen resources with an unverified generic plan.

3. Verify persistence:

   > Show my latest saved deployment plan. Retrieve it from Pardis MCP; do not regenerate the
   > document or query cloud catalogs again. Tell me when it expires and whether anything was deployed.

   Expected: exact saved document, original expiry and no provisioning. The structured response also
   includes the original normalized snapshot; it is not reauthorized cloud data.

4. Revise one choice:

   > Revise my ECS proposal to use a 60 GiB root disk, keeping the same other choices if they are
   > still valid. Save a new backend-checked plan and show its document. Do not deploy anything.

   Expected: fresh checks, a new plan ID/hash and unchanged earlier document. Choose another disk size
   if the selected image requires more than 60 GiB; never invent a successful compatibility check.

5. Negative and access tests:

   > Check whether a subnet from another visible VPC could be used with my selected VPC. Attempt
   > to prepare that combination only if both are visible. The backend must reject the mismatch
   > and preserve my previous plan. Do not modify any cloud resource or permission.

   If only one VPC exists, skip this live case; automated tests cover it. Repeat planning as reader
   and full. With a different user, retrieval of another user's plan must fail even with the same role.
   A successful generic draft is not a successful real-selection test. Reader/full **write behavior**
   cannot be tested yet because no write tool exists. Wait for configured expiry to test record expiry.

Record tested commit/image, catalog successes/failures and results privately, without publishing IDs,
endpoints, source or tokens in this repository. Authenticated cloud selection acceptance remains pending
until these prompts pass against the intended environment.

## Automated verification

```bash
uv run --locked pytest tests/test_ecs_plan.py tests/test_planning.py tests/test_discovery.py -q
uv run --locked pytest -q
```

Tests exercise the authenticated MCP HTTP route, real SDK request signing with mocked IAM/catalog
responses, encrypted Redis, exact document/snapshot/hash agreement, revisions, reader/full ownership,
forged IDs/snapshots, network mismatch, image minima, unusable metadata, later-page lookups, bounded
pagination, timeout, missing endpoints, per-catalog denial and access changes during verification.
They do not prove authenticated live Pardis behavior, cost, capacity or deployment success.

Local evidence (2026-10-26): 244 automated tests pass; local HTTP health reports Redis up,
anonymous MCP calls receive a 401 OAuth challenge, and OAuth discovery advertises PKCE S256.
The configured VPC, IMS and EPS origins respond with 401 to unauthenticated probes. This verifies
connectivity and the authentication boundary, not authenticated catalog permissions or response shape.

Next: close live catalog/selection acceptance, then D4 human approval and idempotent operation state
with fake execution. Select the build/delivery path before the first live ECS deployment. CCE and
team quota design are deferred; see the [active roadmap](APPLICATION_DEPLOYMENT_PLAN.md).
