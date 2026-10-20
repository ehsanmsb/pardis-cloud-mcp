# ECS Implementation Plan

Proposed implementation, 2026-10-06. **None of the new tools or configuration below exists yet.**
For current evidence and acceptance IDs, see [MVP plan](MVP_PLAN.md).

## 1. Design constraints

Extend the existing HTTP MCP backend, OAuth/session provider, cloud adapter, YAML configuration
and encrypted Redis storage. Use the installed Huawei ECS/core SDK with explicit Pardis endpoints.
Do not introduce a second backend, shared cloud service account, job queue, or generic cloud framework.

```text
Existing login -> whoami/list_projects
                         |
                  get_ecs_options
                         |
                     plan_ecs -> expiring immutable plan in Redis
                         |
                 user reviews configuration
                         |
                 client approves create_ecs(plan_id)
                         |
             atomic operation record -> Pardis create API
                         |
                get_operation -> get_ecs -> verified ACTIVE
```

The agent requests tools; the backend enforces identity, project access, input limits and execution
state. Cloud IAM remains authoritative. Neither a prompt nor `mcp:tools` grants cloud permissions.

## 2. S0 — Prepare the bounded environment

Owner: developer and Pardis environment owner. No resource creation in this step.

- Privately record region/test project, approved image, small flavors, availability zone, system-disk
  type/size limit, VPC/subnet, security group and existing SSH key-pair name.
- Confirm resource compatibility, subnet capacity, quota and a cleanup owner. No public IP or
  arbitrary user data/password/private key is accepted in the first tool schema.
- Confirm the mapped user's least-privilege create policy, including required network/disk dependencies.
  Do not replace the read-only policy with administrator access as a shortcut.
- Prepare separate authorized and denied test users. Confirm denied behavior without altering
  existing production policies for the test.
- Finish the [read-only checklist](READ_ONLY_INTEGRATION.md): actor/console comparison, project
  boundaries, expiry and sanitized evidence. Verify target-runtime DNS/TLS separately from the laptop.
- Verify Pardis support for the selected create/job/detail APIs; read-only success alone is insufficient.
  Check optional dry-run and idempotency behavior before relying on either.

Exit: a private resource inventory and test policy exist; positive/negative reads are recorded.

## 3. S1 — Close auth gaps and make writes opt-in

Owner: developer. Files: `auth.py`, `config.py`, `toolset.py`, configuration examples and tests.

1. Complete AUTH-01/02 and JWT tests from the MVP plan. Preserve the explicit re-login policy;
   do not add upstream refresh solely to avoid documenting expiry.
2. Add one proposed `provisioning.enabled` switch, default `false`, to typed YAML configuration.
3. Register `create_ecs` only when the switch is true **and** the tool is explicitly named in
   `tools.enabled`. A wildcard alone must never opt in to a cloud-write tool.
4. Continue honoring `tools.disabled` with highest precedence. Explicitly requesting creation while
   provisioning is off should fail startup clearly, not silently expose a partially configured tool.
5. Validate provisioning configuration at startup; keep shipped local/Helm defaults read-only.
   Check the enablement policy again on execution as a defensive invariant.

Proposed shape, not a working configuration today:

```yaml
tools:
  enabled:
    - whoami
    - list_projects
    - list_ecs
    - get_ecs_options
    - plan_ecs
    - get_ecs
    - get_operation
  disabled: []
provisioning:
  enabled: false
  plan_ttl_seconds: 600
  # Private project/resource allowlists are supplied by the operator.
```

Define typed project/profile entries containing approved IDs, region/zone, disk bounds and fixed
network/SSH-key settings during S2. Keep actual entries private. Enabling writes additionally requires
adding `create_ecs` explicitly. Document this compatibility rule for existing wildcard users.

Tests: default/wildcard/explicit/disabled combinations; invalid config; unavailable tools cannot
be called; actual JWT verification; consent rejection and concurrent session revocation.

Exit: safe defaults and authentication gates pass before any cloud create test.

## 4. S2 — Options, preview and detail without cloud writes

Owner: developer. Reuse `cloud.py`; add a small `provisioning.py` for plan/operation models and
state if needed. Do not refactor the working federation layer just to match a new module layout.

Start with operator-approved options, not a general discovery service. This avoids needing new
IMS/VPC SDKs for the initial demo. Label configuration-derived options honestly; validate existence,
project/region and resource compatibility through available read APIs or recorded operator checks.
Do not present an allowlist as live quota/capacity validation. Add discovery APIs only where needed.

| Tool | Input | Output / contract |
| --- | --- | --- |
| `get_ecs_options` | Explicit accessible project | Approved profiles/resources, limits and validation provenance; no credentials |
| `plan_ecs` | Project, name, approved profile and bounded disk size | Random `plan_id`, normalized final configuration, warnings, expiry; no cloud mutation |
| `get_ecs` | Accessible project and server ID | Actual state, private IP and configuration needed for verification |

Keep the first input surface narrow: profiles resolve image/flavor/network/security group/key pair.
If a prompt is ambiguous, ask the user to select; do not guess project or image IDs.

Store an encrypted immutable plan with issuer+subject, originating MCP session, project, normalized
parameters, configuration digest, creation time and expiry (proposed 10 minutes, capped by session).
Changing parameters requires a new plan. Expired/session-mismatched plans cannot execute. Re-login
requires re-planning an unsubmitted plan; existing submitted operations are handled separately in S4.

`plan_ecs` is backend validation, not capacity reservation, billing quotation or cloud dry-run.
If Pardis supports Huawei's `dry_run`, treat its result as an additional check, not a future guarantee.

Tests: invalid/foreign IDs, bad names/disk limits, mismatched network/project, expired plans, changed
config, other-user/session access, secret-free output; assert zero create API calls.

Exit: a real client can preview a valid bounded plan without provisioning anything.

## 5. S3 — Submit one guarded creation

Contract: `create_ecs(plan_id)` returns `operation_id`, status and a safe summary. It accepts no
replacement parameters, user identity, credentials, endpoint or model-controlled approval boolean.

1. Resolve the current authenticated session, owner and immutable plan. Recheck expiry, config
   digest, limits and project access. Refresh project discovery for execution rather than relying
   only on an old cached project list. The cloud API must still authorize the actual create.
2. Require the client to show manual approval for this tool, with auto-approval off. Test rejection
   before the first live write. Tool annotations and possession of `plan_id` do not prove consent.
3. Atomically claim the plan and persist its operation before network submission, using Redis
   transaction/Lua or equivalent compare-and-set. Persist normalized request, owner, project,
   request digest and client token together. A process-local lock is insufficient across replicas.
4. Repeat/concurrent calls return that existing operation and do not submit another POST. Never
   deduplicate solely by ECS name. Distinct plans can still create distinct resources; user approval
   and demo resource limits remain necessary.
   Check for an existing owner-authorized operation before applying expiry rules for a new submission;
   an expired plan must not erase its operation or permit resubmission. After re-login, use
   `get_operation` to recover a submitted operation rather than executing the old session-bound plan.
5. Resolve current per-user temporary credentials through the existing adapter. Recheck session
   validity immediately before sending. Never fall back to a shared or more privileged identity.
6. Use SDK `CreateServersRequest` / `create_servers`, fixed endpoint/project, `count=1`, explicit
   image/flavor/NIC/security group/key pair/system disk and no public IP. Disable automatic POST retries.
7. Persist returned job/server IDs and a sanitized audit event. A returned job ID means submitted,
   not successful. If submission may have occurred but the response/storage is lost, report unknown.

The installed SDK exposes `x_client_token` and `dry_run`. Huawei documents `X-Client-Token`
idempotency, including an eight-hour validity window; **Pardis support is not yet verified**.
Persist one UUID per operation if supported. Do not claim exactly-once semantics, and do not retry
an ambiguous POST until its outcome is reconciled. In particular, never regenerate the token or
create a new plan automatically to bypass an uncertain operation.

If Redis is unavailable before recording/claiming the operation, do not submit. A crash after claiming
but before recording the cloud response requires reconciliation; do not automatically recycle the claim.
Use persistent Redis without eviction of operation records; retain operation/deduplication records
through the demo and cleanup, longer than plan TTL. Do not expire unresolved operations automatically.

Tests: permission denial, revoked session, duplicate/concurrent calls, cloud timeout after acceptance,
worker crash, malformed response, Redis failure before/after POST, rate limits and quota errors.
Use simulated network failures, not deliberate live quota exhaustion.

Exit: mocks prove safe submission and unknown handling; write-tool rejection is verified in the client.

## 6. S4 — Track and verify without a worker service

`get_operation(operation_id)` performs one or a small bounded number of cloud reads per call.
No long-running background queue is required. Use SDK `ShowJobRequest` / `show_job` and
`ShowServerRequest` / `show_server` with the authenticated user's credentials.

```text
submitting -> pending -> verifying -> succeeded
     |           |          |
     +-----------+----------+----> failed (confirmed failure)
     +---------------------------> unknown (uncertain submission/outcome)
```

Polling timeout returns pending, not failed. Authentication/permission errors are retrieval errors,
not proof that the cloud operation failed. Job success leads to server inspection; report succeeded
only after the expected server is ACTIVE and its important configuration matches the plan.

Plans are session-bound, but submitted operations must remain inspectable after re-login by the
same issuer+subject with current project access. Enforce this in the backend; possession of an
operation/job/server ID is insufficient. Do not store credentials in operation records.

Unknown operations need operator reconciliation using recorded request/job/resource identifiers;
name matching alone is not proof. Record partial resources and cleanup requirements even on failure.
Revoking a session cannot undo an already submitted cloud create.

Audit: actor issuer/subject, mapped cloud identity, project, plan/operation correlation, timestamps,
action, safe outcome and resulting resource IDs. Treat logs as private identity data. Never log raw
tokens, keys, request headers, private keys or unsanitized SDK exceptions. Cloud-side audit/CTS
availability and actor mapping require separate live verification; application logs are not proof of it.

Tests: pending/failure/success jobs, job success before ACTIVE, config mismatch, partial resources,
other-user access, re-login, revoked permissions, restart and unknown outcomes.

Exit: status survives restart/re-login and never reports premature success.

## 7. S5 — Live test and handoff

1. Finish locked Docker installation and run the complete mock suite, Helm checks and image smoke test.
2. Verify TLS/callback, persistent restricted Redis, stable encryption key and non-root OKD execution.
3. Start with writes disabled. Confirm tool discovery, login, project listing, options and preview.
4. Review the test project/profile and cost limits. Explicitly enable creation and reconnect the client.
5. Reject the first write request; confirm no cloud POST. Then approve one new controlled request.
6. Track its operation to actual ACTIVE; compare project/name/image/flavor/disk/network/private IP in
   API and console. Retrying the same plan must return the same operation/resource, not a new ECS.
7. Complete denied-user, expiry, restart and secret-redaction acceptance. Test risky failure paths
   with mocks unless a separately authorized safe live case exists.
8. Inventory and manually remove only this test's created resources; inspect leftover disks/ports.
   Do not delete shared networks, security groups, images or key pairs. Disable writes after testing.
9. Record date, commit/image digest, acceptance IDs/results, limitations and cleanup result without
   sensitive identifiers in public docs. Keep detailed evidence privately.

Proposed logical implementation commits: auth/session safeguards; write-gate configuration;
options/plans/details; guarded create/operation state; status/audit; locked build and runbook.
Each includes relevant tests. Commit/push remains a separate owner-approved action.

## 8. Official references and compatibility boundary

These describe Huawei behavior, not independent proof of Pardis deployment compatibility.

- [Huawei Python SDK](https://github.com/huaweicloud/huaweicloud-sdk-python-v3)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [IAM ID-token federation](https://support.huaweicloud.com/intl/en-us/api-iam/iam_13_0605.html)
- [IAM temporary credentials](https://support.huaweicloud.com/intl/en-us/api-iam/iam_04_0003.html)
- [Create pay-per-use ECS](https://support.huaweicloud.com/intl/en-us/api-ecs/en-us_topic_0020212668.html)
- [ECS creation walkthrough](https://support.huaweicloud.com/intl/en-us/api-ecs/ecs_04_0001.html)
- [Idempotent requests](https://support.huaweicloud.com/intl/en-us/api-ecs/en-us_topic_0000001105258814.html)
- [Query a job](https://support.huaweicloud.com/intl/en-us/api-ecs/ecs_02_0901.html)
- [ECS permissions](https://support.huaweicloud.com/intl/en-us/api-ecs/ecs_06_0002.html)
