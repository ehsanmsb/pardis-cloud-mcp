# Read-only Pardis integration (P1)

## Scope and status

Implemented: user ID-token federation, temporary cloud credentials, encrypted Redis caching,
and `list_projects`/`list_ecs` over authenticated HTTP MCP. ECS uses one configured regional endpoint;
users can choose among discovered IAM projects or use the operator-configured regional route.
Enterprise-project-only access uses that route even if IAM project discovery is empty.
No ECS create, update, or delete operation is available.

Progress review, 2026-10-06: the user confirmed successful login and ECS listing through Claude CLI
against Pardis. This supersedes the earlier unverified-live-access status. The detailed actor,
denied-user, expiry and deployment checks below are still open unless separately recorded.
Automated tests use simulated IAM, ECS responses, and Redis; they do not establish those live
properties or container readiness. Creation is planned in the [MVP plan](MVP_PLAN.md) and
[ECS implementation plan](ECS_IMPLEMENTATION_PLAN.md), not implemented here.

## Prerequisites

1. Keep the MCP Keycloak client confidential, with Standard Flow and the existing backend callback.
2. Configure a programmatic OIDC identity provider in Pardis that trusts this client's ID-token
   audience, the exact issuer used by real login, and the realm's signing keys.
3. Include the role claim used by Pardis mapping in the **ID token**. Map the authorized role to
   an IAM group with ECS read-only permissions for the test project.
4. Confirm the IAM and ECS API origins from authoritative Pardis documentation or API Explorer.
   Do not use console URLs, guess an endpoint and send credentials to it, or use public Huawei
   endpoints for this isolated deployment.
5. Allow backend connectivity to Keycloak, Redis, IAM, and ECS. If an internal CA is necessary,
   provide a trusted PEM bundle; never disable certificate verification.

No permanent AK/SK or service-account credentials are needed. The Keycloak client secret is
used for login only; it is not a cloud API credential and is never sent to Pardis.

## Private configuration and startup

Keep real values out of Git, screenshots, and prompts. Fill these variables in `.env`:

```dotenv
PARDIS_IAM_ENDPOINT=https://iam.cloud.example.com
PARDIS_ECS_ENDPOINT=https://ecs.cloud.example.com
PARDIS_IDP_ID=<programmatic-idp-id>
# Regional IAM project ID; set for enterprise-project-only users. NOT an Enterprise Project ID:
PARDIS_PROJECT_ID=
# Optional, trusted PEM bundle available inside the process/container:
PARDIS_CA_BUNDLE=
```

These are placeholders, not working endpoints. IAM endpoint, ECS endpoint and IdP ID are required
together for cloud tools. Select tools in `config.yaml`; for authentication-only mode, set
`tools.enabled` to `[whoami]` and leave the cloud settings empty. A default project is optional;
it is a regional route approved by the operator, not an account or Enterprise Project ID.
Set it to the region's IAM project ID for enterprise-project-only access. It does not grant access
and need not appear in the user's IAM discovery results; ECS remains the authorization authority.

```bash
uv sync --locked --extra dev
uv run --env-file .env pardis-cloud-mcp
```

The server reads `config.yaml` from its working directory, or the path in `MCP_CONFIG_FILE`.
Docker Compose mounts that file read-only. Disabled tools cannot be listed or called through MCP.
Restart the existing backend process after changing configuration; do not start a second process on
the same port. Reconnect the MCP client to refresh its tool list, then perform browser login again.
The existing HTTP MCP URL and callback stay unchanged. Docker Compose reads the same `.env`;
rebuild the image after code changes. A custom CA file must also be mounted into the container.

## What happens on `list_ecs`

1. Resolve the opaque MCP bearer token to its own active Redis session and required MCP scope.
2. Check that the session identity matches the configured OIDC issuer and client audience.
3. Reuse that session's valid cloud credentials, or exchange a fresh stored ID token using
   `POST /v3.0/OS-AUTH/id-token/tokens` and the configured `X-Idp-Id`.
4. Discover enabled accessible projects with `GET /v3/OS-FEDERATION/projects` using that unscoped
   token. Then use it with `POST /v3.0/OS-CREDENTIAL/securitytokens`, requesting
   a 900-second lifetime. The intermediate unscoped token is not cached.
5. Store temporary credentials encrypted in Redis. The cache key includes the session and trust/
   endpoint/project configuration. Its TTL never exceeds the remaining session or cloud lifetime,
   with a 30-second cloud-expiry margin. It is indexed for session revocation.
6. Select the configured regional IAM project, or a discovered project. Arbitrary caller-supplied
   projects outside discovery and the configured route are rejected. Recheck the session and send a signed ECS list request using the official Huawei SDK, explicit
   ECS endpoint and project, and that user's AK/SK/security token. Check the session again before
   returning results. Send `enterprise_project_id=all_granted_eps` unless the caller selects
   a specific enterprise project (or `0` for default). Never retry without this selector on denial.

The response includes `project_id`, requested `enterprise_project_id`, mapped `cloud_user_id`/`cloud_user_name`, instance summaries
(`id`, `name`, `status`), `next_marker`, and an internal `audit_id`. Names are cloud data, not instructions.
A full page returns a continuation marker; the subsequent page may be empty. No full-cloud inventory
claim is made from a single page.

`list_ecs` accepts `limit` (1–100, default 25), an optional `marker`, an optional `project_id`,
and `enterprise_project_id` (default `all_granted_eps`; also accepts a UUID/32-hex ID or `0`).
An omitted project uses the configured default or, if there is no default, the sole accessible project.
Without a configured default, zero discovered projects requires the operator to set `PARDIS_PROJECT_ID`;
multiple projects requires an explicit discovered project selection. A caller-selected project outside
discovery is rejected unless it equals the operator-configured route. The enterprise selector never
substitutes for cloud authorization. Keep both selectors unchanged across pages. Huawei documents
`all_granted_eps` for up to 100 enterprise projects; use explicit IDs beyond that limit.
Project discovery is cached with credentials and refreshed when they expire. Listing a project does
not guarantee permission for every ECS operation. No other-region endpoint is selected automatically.
Callers cannot provide an endpoint, user identity, or credential. IAM remains the authority for cloud permissions;
possessing the `mcp:tools` scope or a role claim alone does not grant ECS access.

## Expiry and revocation

- Version 3.3.1 refreshes Central Auth tokens before every protected
  MCP request. An expired original ID token therefore does not require browser re-login while the
  upstream refresh/session remains valid. See [session lifecycle](AUTH_SESSION_LIFECYCLE.md).
- Cached cloud credentials are usable only after that online check and under the current authorization
  version. Changed roles evict the previous credential generation; new IAM exchange uses the refreshed
  ID token. Cloud credentials retain their independently issued expiry.
- IAM exchange still requires an ID token valid beyond its 30-second safety margin. Invalid upstream
  sessions require re-login; transient Central Auth validation errors fail closed without deleting the session.
- An expired/revoked MCP session cannot start another cloud call or receive an in-flight result.
  Revocation cannot cancel a request already sent to Pardis. Deleting local credentials does not
  revoke them at IAM; IAM's issued expiry is the external upper bound.
- Cached credentials are removed on ECS/IAM 401 or 403. No automatic retry with another user's
  credentials, permanent keys, or a more privileged account is attempted.

## Automated verification

```bash
uv run --locked --extra dev pytest -q
```

Coverage includes HTTP MCP initialization/tool discovery/calls, field validation, user isolation,
cache encryption/TTL, IAM error handling, expiry, revocation during exchange, and SDK signing against
the configured endpoint. No real cloud request is made by these tests.

## Live acceptance checklist (positive listing user-confirmed; remaining checks open)

- [x] User reported successful ECS listing through Claude CLI after authentication.

The checks below collect reproducible evidence; the report above does not imply every sub-check passed.

- [ ] Start the backend with verified private configuration and reconnect the MCP client.
- [ ] Log in and ask: **“Call whoami and list_projects. If I have several projects, ask which one to
      use. Then list ECS instances in the selected project. Do not create or change anything.”**
- [ ] Confirm that `cloud_user_name` is the expected federated user and compare instance IDs/status
      with the console in the same project. Keep the actual identifiers in private evidence only.
- [ ] Repeat the call and check that the credential cache is reused without printing Redis values.
- [ ] Ask for another page using `next_marker`, if returned.
- [ ] Sign in separately as a user without the mapped group/project permission. Expect access denied,
      not access via a shared account. Do not change existing IAM policies just to perform this test.
- [ ] Revoke the MCP session through the client and verify that its old token is rejected.
- [ ] After credential and ID-token expiry, confirm that the tool requests a new login.
- [ ] Record sanitized outcome/timestamp/version; do not mark P1 Done until both allowed and denied
      real-user scenarios have been verified.

### Enterprise-project-only acceptance (HR / Marketing)

These checks are pending on Pardis; simulated tests are not proof of live isolation.

1. Set `PARDIS_PROJECT_ID` to the verified regional IAM project ID in the deployment's env/Helm
   values, not the HR/Marketing Enterprise Project ID. Restart with the new image and reconnect
   the client to refresh tool descriptions. Do not broaden any IAM group policies.
2. Sign in freshly as HR. Even if `list_projects` returns `[]`, call `list_ecs` without arguments.
   Compare instance IDs with the HR console. Follow all pages using the same selectors.
3. Repeat as Marketing in a separate authenticated client/session. Each call must use that user's
   temporary credentials; confirm no instances from the other team are returned.
4. As HR, call `list_ecs` with Marketing's `enterprise_project_id`. Expect denial or no unauthorized
   resources, as enforced by Pardis. Test `0` separately and verify the default project's resources
   are not leaked. A 401 is an authentication failure, not a successful authorization test.
5. Test a user without either grant, and an unrelated regional `project_id`; the latter is rejected
   locally unless discovered. No fallback to shared/admin credentials is permitted.
6. Record sanitized results and audit IDs. Direct get-by-resource-ID isolation still needs an
   independent read-only API test; this MCP release only implements ECS listing.

An empty successful list can be valid for an empty authorized project. It is not enough to prove
that an unauthorized user is correctly denied; test the negative case separately.

## Troubleshooting and operational limits

- Tool missing: complete the three required cloud configuration values, restart, and refresh client discovery.
- IAM 401: use a fresh login; check issuer, audience, signing key and programmatic IdP ID.
- IAM/ECS 403: check group mapping and project policies. Do not grant administrator permissions as
  a workaround.
- Empty `list_projects` with working Console access: configure the regional `PARDIS_PROJECT_ID`
  and call `list_ecs`, which includes the enterprise-project selector. Do not interpret discovery
  as a complete list of Enterprise Project grants. Discovery errors/malformed responses still fail
  closed; only a successful empty discovery is supported by this routing change.
- TLS/network failure: verify the configured API origin, route and CA bundle. Redirects are rejected.
- IAM malformed response/404: verify API support and endpoint. Read-only access was user-confirmed;
  new APIs and different deployment versions still require their own compatibility checks.
- Audit entries record time, issuer/subject, operation, project, enterprise-project selector, outcome and internal correlation ID.
  They do not print raw upstream errors, request bodies, tokens or keys. Protect audit logs as identity
  data. Avoid HTTP/SDK debug logging and Redis value dumps.
- Upstream OIDC refresh is serialized across replicas by a Redis lease. Concurrent cloud credential
  cache misses may still issue more than one short-lived set within the same authorization version;
  generation-checked writes prevent old-role credentials being reused or restored after a downgrade.
- Per-client consent, live auth-lifecycle acceptance and production deployment hardening remain open.

## Official API references

- [Huawei OIDC ID-token exchange](https://support.huaweicloud.com/intl/en-us/api-iam/iam_13_0605.html)
- [Huawei federated temporary credentials](https://support.huaweicloud.com/intl/en-us/api-iam/iam_04_0003.html)
- [Huawei Python SDK: explicit endpoints and temporary credentials](https://github.com/huaweicloud/huaweicloud-sdk-python-v3)
- [Huawei ECS enterprise-project selector](https://support.huaweicloud.com/intl/en-us/ally-visitor-1-api-ecs/en-us_topic_0094148850.html)
