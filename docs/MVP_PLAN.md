# MVP Execution Plan: Provisioning an ECS Through Prompts and MCP

> Historical ECS-only roadmap. As of 2026-10-25, use the
> [application deployment development plan](APPLICATION_DEPLOYMENT_PLAN.md) for current scope,
> baseline and staged acceptance. Status and prerequisites below reflect the earlier review.

Baseline: 2026-10-04. Progress review: 2026-10-06, against commit `d0ca1ec`.

This is the status and acceptance reference. See [ECS implementation plan](ECS_IMPLEMENTATION_PLAN.md)
for ordered engineering steps. This documentation update does not implement or enable writes.

Do not record internal domains, real endpoints, personal paths, actual user/project/resource IDs,
tokens, keys or secrets. Keep configuration and detailed live evidence in private storage.

## 1. Outcome and scope

A user logs in through an HTTP MCP client, previews a bounded ECS configuration, approves the write
tool, and receives the actual state of an ECS created with their Pardis permissions. Success means
`ACTIVE` and a configuration match in the API and console, not merely a successful create response.

> In the test project, plan one demo-vm using the approved small profile and existing network.
> Show me the final configuration. Create it only after I approve, then report its status.

Include one region, one designated test project, one approved image, one or two flavors, existing
VPC/subnet/security group/key pair, one system disk and one ECS per plan. Agree resource/cost limits
and cleanup ownership before writing. No public IP is needed. SSH is optional; private keys never
enter prompts or MCP.

Defer new networks/EIPs, bulk/multi-region provisioning, general deletion, storage/Kubernetes/billing
tools, a new dashboard, automatic upstream Keycloak refresh, and broad multi-client certification.
Manual inventory-based cleanup is sufficient for MVP.

## 2. Progress against the previous plan

Implementation, automated tests and live acceptance are separate evidence levels.

| Previous phase | Status | Completed evidence | Remaining exit criteria |
| --- | --- | --- | --- |
| P0: Environment | Partial | Working private IAM/ECS/IdP configuration evidenced by successful live listing | Write-test resources, limits, quota, authorized/denied users, cleanup owner and target-container connectivity |
| P1: User-based Pardis access | Positive live path user-confirmed; acceptance partial | User explicitly reported ECS listing through Claude CLI after login; federation, project discovery and SDK integration exist | Sanitized run record, mapped-actor/console comparison and live denied-user/project test |
| P2: Auth and read-only integration | Partial | Session checks, encrypted user-bound credentials, project selection, fixed HTTPS endpoints, three tools and YAML selection | AUTH-01/02, JWT tests, expiry acceptance, provisioning options and ECS detail tool |
| P3: Create and track ECS | Not started | No provisioning tools in the current catalog | Immutable plans, approval, guarded creation, duplicate prevention, status and actual ECS verification |
| P4: Test, deploy and handoff | Packaging implemented; acceptance pending | Docker/Compose, Helm with Redis/ConfigMap/Secret/HTTPProxy, release workflow and guides | Locked image installation, container restart, target OKD smoke test, write acceptance and cleanup |

Earlier statements that private IAM configuration and *all* live Pardis access were unverified are
superseded by the user's successful read-only test. This does not prove create permission or
Pardis compatibility with create/job/idempotency APIs.

### Implemented foundation

| Capability | Evidence and limits |
| --- | --- |
| Streamable HTTP and embedded OAuth | `server.py`, `auth.py`; real client login reported working |
| Confidential Keycloak client, Authorization Code + PKCE | Backend owns the secret; ID-token validator checks signature/issuer/audience/nonce/expiry; direct validator tests still needed |
| Opaque MCP tokens and encrypted Redis sessions | `auth.py`; simulated browser/token tests; Keycloak tokens remain in the backend |
| Per-user federation and temporary credentials | `cloud.py`; HTTPX for IAM, official Huawei ECS/core SDK for signed ECS calls; no permanent AK/SK fallback |
| `whoami`, `list_projects`, `list_ecs` | `toolset.py`; simulated project selection and access-boundary tests |
| YAML toolset selection | `config.py`, `config.yaml`; unknown names fail startup; disabled tools are not callable |
| Home, 404, authentication completion pages | Packaged, agent-neutral pages; completion page is not client consent |
| Helm and semantic-release automation | Template/workflow tests exist; configuration alone does not prove successful publication or OKD deployment |

Reuse the current backend, Redis and SDK. No separate backend or generic workflow framework is needed.

### Evidence baseline

- Automated suite: 63 tests passed on 2026-10-06, with four Huawei SDK `datetime.utcnow`
  deprecation warnings. Cloud responses and Redis are simulated. The OAuth flow test substitutes
  ID-token decoding; it does not establish actual JWT-validator coverage.
  The planning-time rerun also reported a local pytest cache-write permission warning; no test failed.
- Live: user confirmed Keycloak login and ECS listing through Claude CLI. Exact live timestamp/version
  and detailed negative-case results have not been recorded.
- Live ECS creation, quota-failure acceptance, container-restart acceptance and target OKD runtime
  acceptance are not established by this review.
- Source/tests were checked directly. The local code graph is older than the current authentication
  implementation and is not used as evidence of completion.

## 3. Required fixes before enabling creation

### AUTH-01: Client consent and browser binding — open

Dynamic registration currently redirects to Keycloak without separate consent for the requesting
MCP client. An existing SSO session does not establish consent for a new MCP client.

- [ ] Display requesting client, requested access and redirect destination; escape client metadata.
- [ ] Bind consent to user/client and authorization flow, with browser binding and CSRF protection.
- [ ] Test existing SSO + new client, rejection, replay and cross-browser callback attempts.

OAuth consent is separate from approval of an individual `create_ecs` call.

### AUTH-02: Session, refresh and revocation coordination — partial

- [x] Require a live matching session for tokens and recheck it around cloud calls.
- [x] Consume authorization codes and refresh tokens with Redis `GETDEL`.
- [ ] Coordinate session validation, token issuance/indexing and revocation atomically.
- [ ] Cap access/refresh token lifetimes to the remaining absolute session lifetime.
- [ ] Test concurrent refresh/revocation, replay and expiry, not only leftover-token rejection.

Session TTL is fixed at login (default eight hours). MCP refresh issues MCP tokens but does not
extend the session. Active-session checks do not prove the whole issuance/revocation sequence atomic.

### AUTH-03: Expiry policy — implemented fallback, acceptance partial

- [x] Check ID-token expiry before IAM exchange and cloud expiry before credential reuse.
- [x] Require fresh login when another ID token is needed; automatic Keycloak refresh is deferred.
- [ ] Verify expiry/re-login in a real client, including recovery of an existing operation.
- [ ] Record logout/revocation enforcement limits in the handoff.

The backend requests 900-second cloud credentials, uses IAM's returned expiry, and caches with a
30-second margin and remaining-session TTL cap. Cached credentials can outlive the original ID token.
Keycloak logout is not propagated immediately: no back-channel logout or continuous upstream session
check exists. Local revocation cannot cancel a request already sent or remotely revoke IAM credentials.
Do not promise immediate Keycloak-logout enforcement.

### TEST-01 and DEPLOY-01 — partial

- [x] HTTP initialization/discovery/tool calls, project boundaries, SDK signing and cache-isolation tests.
- [ ] Actual signed JWT tests: signature, issuer, audience, nonce, required claims and expiry.
- [x] `uv.lock`, locked CI test installation, Docker/Compose, Helm and release configuration.
- [ ] Install locked runtime dependencies in Docker: current `pip install .` resolves version ranges
  independently of `uv.lock`.
- [ ] Verify image startup, OKD/non-root runtime, Redis connectivity/persistence, health and HTTPS callback.
- [ ] Verify restart recovery with a stable encryption key and recorded image digest.

The MVP is private and controlled. Multi-user/public exposure additionally needs registration/OAuth
abuse controls and operational hardening; a successful demo is not production approval.

## 4. Ordered delivery and schedule

Detailed contracts and tests are in [the implementation plan](ECS_IMPLEMENTATION_PLAN.md).

| Step | Deliverable | Exit gate |
| --- | --- | --- |
| S0 | Environment and read-only live evidence | Authorized/denied scenarios, private inventory and least-privilege policy agreed |
| S1 | Auth safety and explicit write enablement | AUTH-01/02 and JWT tests pass; wildcard cannot silently enable create |
| S2 | `get_ecs_options`, `plan_ecs`, `get_ecs` | Immutable, expiring, user/project-bound plans; zero cloud mutations |
| S3 | `create_ecs` and operation records | Mock permission/concurrency/unknown-outcome tests and client rejection test pass |
| S4 | `get_operation` and actual ECS verification | Bounded polling, owner checks, re-login recovery and accurate status |
| S5 | Controlled live test and deployment handoff | Critical acceptance tests, container/OKD verification and cleanup complete |

Read-only options and mocks can proceed while the environment owner prepares permissions. Keep
real writes disabled until gates pass. `mcp:tools` grants MCP access, not cloud creation;
a mapped `ecs-readonly` role is not a create permission.

Tentative estimate from this baseline: four working days plus one contingency day, conditional on
environment readiness and auth review. This is not a delivery guarantee.

- Day 1: S0/S1, permissions/resources and authentication/write gating.
- Day 2: S2, preview through the client and no-write tests.
- Day 3: S3/S4, failure tests and first approved live creation if gates pass.
- Day 4: S5, deployment, repeatable demo, evidence and cleanup.
- Contingency: Pardis API differences, policy, quota, networking, builds or auth issues.

Reduce image/flavor variety if time is short, not owner checks, approval, duplicate prevention or
accurate unknown-outcome reporting.

## 5. Acceptance matrix

Record date, code/image version and sanitized result per live run. Keep identifiers/raw logs private.
Partial automated coverage does not close a live acceptance test.

| ID | Expected result | Current evidence / remaining work |
| --- | --- | --- |
| T01 | Real login, correct identity/project and ECS list | Login/list user-confirmed; mapped-actor and console comparison record open |
| T02 | Invalid JWT/signature/issuer/audience/nonce/expiry rejected | Validator exists; direct test matrix open |
| T03 | New client with existing SSO cannot bypass consent | AUTH-01 open |
| T04 | Code/refresh replay and concurrent revocation cannot restore access | Sequential consumption/session checks present; concurrency tests open |
| T05 | Session/ID/cloud expiry has clear re-login behavior | Simulated coverage partial; live transition and operation recovery open |
| T06 | Denied user/project cannot create | Read-only boundaries simulated; live denial/write tests open |
| T07 | Another user cannot inspect/execute a plan or operation | Open |
| T08 | Reject write in client: zero cloud create requests | Open; auto-approval must be off |
| T09 | Bad resource/quota/permission errors without false success | Open; inventory any partial resources |
| T10 | Repeated/concurrent execution of one plan gives one operation | Open; include restart/Redis failure |
| T11 | Lost response after POST yields unknown, not blind resubmission | Open; reconcile before new create |
| T12 | Prompt creates one matching ACTIVE ECS visible in console | Open |
| T13 | Container restart preserves trackable operation in Redis | Open; preserve encryption key, reject expired sessions |
| T14 | No credentials/private keys in outputs/logs | Read-only checks exist; expand to writes/SDK errors and live inspection |
| T15 | Cleanup removes only test-created resources | Open; check disks/ports, preserve shared resources |
| T16 | Create disabled by default, including wildcard configurations | Open |
| T17 | Locked image and target deployment pass smoke tests | Packaging exists; DEPLOY-01 open |

Run mocks in CI. Live tests are manual, permission-scoped and cost-bounded. Simulate quota failures;
do not deliberately exhaust real quota without a separately approved safe test.

## 6. Definition of done and next action

- [ ] Identity, permissions, consent, session isolation and required auth tests verified.
- [ ] Preview and tested manual approval produce one tracked operation per plan.
- [ ] Real ECS configuration and ACTIVE state verified through API and console.
- [ ] Duplicate calls, expiry, permissions and uncertain outcomes behave safely.
- [ ] Container/Redis restart and target deployment verified; demo/cleanup instructions usable.
- [ ] Sanitized evidence/limitations recorded; no critical write-path defects remain.

**Next: S0.** Privately select test project, image, small flavor, disk limit, network, security group
and SSH key pair; confirm minimum create permissions and quota with the environment owner.
Repeat positive/negative read-only checks. Then implement S1, not an unguarded create tool.

## 7. Decision log

| Date | Decision / evidence |
| --- | --- |
| 2026-10-04 | Initial bounded per-user ECS MVP plan established |
| 2026-10-05 | Read-only federation/project selection and simulated SDK tests added; default project optional |
| 2026-10-06 review | User-confirmed ECS listing supersedes earlier IAM/read-only blocker |
| 2026-10-06 review | Toolset, Helm/release and UI work recognized; write tools remain unimplemented |
| 2026-10-06 planning | Reuse backend/Redis/SDK; operator-approved options first, explicit write gate, manual cleanup; documentation only |
