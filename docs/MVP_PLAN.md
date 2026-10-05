# MVP Execution Plan: Provisioning an ECS Through Prompts and MCP

Baseline review date: 2026-10-04

Latest implementation update: 2026-10-05. P1 code and simulated tests are available;
live Pardis acceptance remains open. See [the read-only runbook](READ_ONLY_INTEGRATION.md).

This document is the working reference for MVP development and acceptance. Status reflects the latest review, not the current availability of running services. Update the checklist and evidence after completing each phase.

Do not record internal domains, real service addresses, personal filesystem paths, actual project or user identifiers, secrets, tokens, or access keys in this document. Variable and tool names describe the design only. Keep actual values in private environment configuration.

## 1. Expected Outcome

A user signs in through a compatible MCP client, such as Claude CLI, and requests an ECS using a prompt. MCP presents the allowed options and final configuration. After user approval, it provisions the ECS in Pardis Cloud with that user's permissions and reports its actual state.

Example prompt using a fictional resource name:

> Create an ECS named demo-vm in the test project using Ubuntu and the small profile on the existing network. Show me the final configuration before creating it. After I approve, report its status and IP address.

Success means the ECS appears in both the Pardis API and console with the agreed configuration and an `ACTIVE` status. An initial creation response alone does not mean provisioning has completed successfully.

## 2. Completed Work and Available Evidence

| Item | Status | Evidence and verification limits |
| --- | --- | --- |
| MCP server over Streamable HTTP | Completed | The server has run over HTTP and its discovery metadata has been checked |
| Backend connection to Keycloak using a confidential client | Implemented | The backend uses a client ID and secret; the user tested and confirmed the login flow through Claude CLI |
| Embedded OAuth broker | Implemented | Registration, authorization, callback, token, and revocation endpoints exist; the fixes in Section 3 remain open |
| Authorization Code and PKCE | Implemented | A simulated flow through MCP token issuance has been tested |
| Redis session and token storage | Implemented | Sensitive values are encrypted with Fernet; bearer tokens are hashed when used in Redis keys |
| Separation of MCP and Keycloak tokens | Completed | The MCP client receives an opaque token; Keycloak tokens remain in the backend |
| `whoami` tool | Implemented and locally tested | Authenticated HTTP tool call is covered by an automated test |
| Health checks and endpoint protection | Tested | Health checks succeeded with real Redis; unauthenticated MCP requests returned 401 |
| Client registration and real redirect | Tested | Registration returned 201 and redirection to Keycloak returned 302 |
| Current automated tests | 21 passing | Includes HTTP MCP calls, project discovery/selection, federation/cache isolation, expiry/revocation, and real SDK signing with simulated network responses; Redis and Keycloak exchange/validation remain simulated |
| Read-only `list_ecs` integration | Implemented; live verification open | Uses the existing session, IAM federation, encrypted temporary credentials, and the official Huawei SDK with configured endpoints |
| Dockerfile and Compose | Prepared; final execution unverified | MCP and Redis container definitions exist; the previous build stalled while retrieving the base image |
| Initial documentation and configuration example | Completed | Setup explicitly loads the private configuration; read-only runbook and optional cloud settings added |
| Baseline Git changes | Completed | Authentication, deployment, and documentation were committed separately and pushed; private configuration was excluded |

The user reports that Pardis identity-provider integration with Keycloak and console login are working. This does not yet verify programmatic access from MCP to Pardis.

Pardis credential exchange and read-only ECS listing are implemented and tested with simulated cloud
responses. No live Pardis API test or resource-creation test has been completed. ECS provisioning is
not implemented.

## 3. Required Fixes Before Enabling Resource Creation

### AUTH-01: Consent for Each MCP Client

Dynamic client registration is enabled, but authorization redirects directly to Keycloak without separate consent for the requesting MCP client. An existing SSO session does not establish consent for a new client.

- [ ] Display the client name, requested access, and redirect destination on the consent page.
- [ ] Bind consent to the user and client; do not share consent between clients.
- [ ] Add CSRF protection and bind the authorization/callback flow to the browser session.
- [ ] Test that a new client cannot obtain tokens through an existing SSO session without consent.

### AUTH-02: Session, Refresh, and Revocation Coordination

The baseline review found a refresh/revocation race that left tokens accepted after session deletion.
Access-token validation now checks the active session, and the cloud path rechecks it before sending
the request and before returning results. Full atomic refresh/revocation coordination remains open.
These checks use simulated Redis and do not modify real sessions.

- [x] Check that the session is active when accepting tokens and before cloud operations (local tests).
- [ ] Coordinate refresh and revocation using atomic controls or validated session versions.
- [ ] Define a maximum session lifetime and cap access/refresh token lifetimes accordingly.
- [ ] Ensure a revoked session cannot regain access through a refresh/revocation race.
- [ ] Test replay, expiry, and concurrent requests.

### AUTH-03: Keycloak and Cloud Credential Expiry

The current refresh flow renews only MCP tokens and leaves stored Keycloak tokens unchanged. Sessions also have an independent lifetime; renewing an MCP token does not renew the session or Pardis credentials.

- [x] Record and check Keycloak token and Pardis credential expiry.
- [x] Explicitly require a new login when a fresh ID token is needed; automatic Keycloak refresh is deferred.
- [ ] Stop cloud operations with an invalid session if Keycloak rejects refresh.
- [ ] Define logout/revocation behavior: validation points and maximum enforcement delay.
- [ ] Test expiry transitions without long waits in automated tests.

### TEST-01 and DEPLOY-01: Test Coverage and Reproducible Execution

- [ ] Exercise actual JWT validation in tests: signature, issuer, audience, nonce, and expiry.
- [x] Test MCP initialization, tool listing, and authenticated `tools/call` (simulated cloud services).
- [ ] Build the image using locked dependencies matching the tested versions.
- [ ] Run the container and verify Redis connectivity and state persistence after restart.
- [x] Correct the setup instructions to load the private configuration file explicitly.

## 4. Delivery Scope

### Included in the MVP

- One region and one test project with controlled access.
- One valid image and one or two explicitly supported flavors.
- An existing VPC, subnet, security group, and SSH key pair.
- One ECS per request, with a specified system disk and agreed resource limits.
- Option selection, plan preview, user approval, provisioning, and status tracking.
- Cloud API execution using temporary credentials tied to the authenticated user.
- Audit records containing actor, project, operation, timestamp, outcome, and created resource, without credentials.
- Testing through Claude CLI, repeatable setup instructions, and a test-resource cleanup procedure.

### Deferred Until After the MVP

Network creation from scratch, new EIPs, bulk ECS creation, management across all regions, Kubernetes, general storage operations, billing, an administration dashboard, a general-purpose deletion tool, and broad compatibility testing across multiple MCP clients.

A private IP is sufficient for the baseline demo. If SSH access is an acceptance requirement, prepare network access before live testing. Private keys must never enter prompts or tool outputs.

## 5. Target Architecture

```text
User and MCP client
    -> Keycloak browser login and consent for the MCP client
    -> Receive an opaque MCP token
    -> Call a tool using that token
    -> Backend retrieves the valid user session
    -> Use an ID token trusted by Pardis IAM
    -> Obtain a federated / unscoped token
    -> Obtain temporary AK + SK + Security Token for the user
    -> Call the ECS API and track the result
```

Huawei documents this federation flow. The supported endpoints, API versions, and configuration on Pardis must be verified in P1. API compatibility is an initial integration assumption, not a completed test result.

The Keycloak client used by MCP may differ from the client trusted by the console's identity provider. Verify issuer, audience/client ID, signing key, and group mappings. If needed, configure a dedicated identity provider for MCP programmatic access using the same Keycloak instance.

The `mcp:tools` scope permits MCP access only. ECS creation permissions must come from Pardis IAM and project policies.

Proposed implementation structure:

| Component | Responsibility |
| --- | --- |
| Existing authentication layer | Login, consent, sessions, refresh, and revocation |
| `cloud_auth` module | Exchange identity for temporary credentials and manage expiry, bound to the user and session |
| `ecs` module | Validate options and call Pardis APIs |
| MCP tools | Typed inputs and outputs for planning, creation, and status |
| Redis | Encrypted sessions and credentials, immutable plans, and operation records |

The existing backend is sufficient for the MVP. The initial choice for request signing is the official Huawei SDK with explicit Pardis endpoints and project configuration. Verify compatibility in P1 and limit dependencies to the services required by this MVP.

## 6. Execution Phases and Exit Criteria

### P0: Prepare the Environment and Required Information

Status: Open. Primary owner: Pardis environment owner, supported by the developer.

- [ ] Select the test project and region, and agree on test resource/cost limits.
- [ ] Configure IAM, ECS, and any option-discovery endpoints in private configuration.
- [ ] Identify the IdP, issuer, audience, and group mappings; verify Programmatic Access.
- [ ] Provide one authorized user and one user without creation permissions.
- [ ] Prepare the image, flavors, network, security group, SSH key pair, and required quota.
- [ ] Verify network and TLS access from the backend environment; use the appropriate trust store for an internal CA.
- [ ] Assign cleanup ownership and define how test-created resources will be identified.

Exit criterion: Required information is available privately and all demo dependencies are identified. No actual environment values are recorded in this document.

### P1: Prove Pardis Access Using the User's Identity

Status: In Progress. Dependency: P0. Primary owner: Developer, with IAM configuration support from the environment owner.

Implementation and simulated tests are complete for the initial read-only path. All live checks below
remain open; the private cloud configuration is not yet populated. This is not a production rollout.

- [ ] Obtain a fresh ID token through the existing MCP login flow.
- [ ] Test exchanging it for a Pardis federated token and temporary credentials.
- [ ] Verify the resulting identity and permissions against the expected mapping.
- [ ] Execute a read-only request, such as listing ECS instances in the test project.
- [ ] Check behavior for a user without project access; determine the minimum creation policy and network/disk dependencies.
- [ ] Record status codes, timestamps, and sanitized errors without tokens or credentials.

Exit criterion: Reading ECS instances succeeds with the authenticated user's temporary credentials, and unauthorized access is denied.

Decision point: Complete the initial investigation within the first half-day. If it fails, narrow the cause to API support, audience, mapping, permissions, or networking and assign it to the relevant owner. Update the schedule once the blocker is understood. Shared service credentials are not equivalent to user-based access and would require an explicit scope change.

### P2: Complete Authentication and Read-Only Integration

Status: Open. Dependency: P1 for integration; authentication fixes can proceed while a P1 blocker is being resolved.

- [ ] Complete AUTH-01 through AUTH-03 and their tests.
- [ ] Securely bind each request's authentication context to that user's session.
- [ ] Isolate stored credentials by user, session, project, and expiry.
- [ ] Reject arbitrary endpoints or credentials supplied through model inputs.
- [ ] Implement supported provisioning options and ECS detail retrieval.
- [ ] Return understandable authentication, permission, rate-limit, and timeout errors.

Exit criterion: Read-only tools work through a real MCP client, users cannot use each other's sessions or credentials, and session revocation prevents further access.

### P3: Implement ECS Creation and Status Tracking

Status: Open. Dependency: P2.

- [ ] Define tool input/output schemas according to Section 7.
- [ ] Create immutable, expiring plans bound to the user and project, using valid discovered IDs.
- [ ] Revalidate permissions and options when executing a plan.
- [ ] Enable manual approval for write tools in the demo client and test that rejection prevents execution.
- [ ] Record an operation before submitting the request and atomically prevent duplicate submission of the same plan.
- [ ] Store returned job/server identifiers and implement bounded, repeatable status checks.
- [ ] Handle unknown outcomes after timeouts without blindly retrying creation requests.
- [ ] Record created resources for audit and cleanup; use tags/metadata if supported by the verified API.

Exit criterion: An ECS is created through MCP, its configuration is verified, and its actual `ACTIVE` state is reported. Duplicate-submission and unknown-outcome tests also pass.

### P4: Complete Testing, Container Deployment, and Handoff

Status: Open. Dependency: P3.

- [ ] Complete the test matrix in Section 9 and record sanitized evidence.
- [ ] Complete DEPLOY-01 and keep the encryption key stable across restarts.
- [ ] Set the public URL and callback for the delivery environment in private configuration; for remote deployment, use TLS and restrict Redis access.
- [ ] Apply rate limits to registration and OAuth endpoints for multi-user access.
- [ ] Run tests that do not access the cloud in CI; run cost-incurring cloud tests manually in the test environment.
- [ ] Complete client setup instructions, example prompts, troubleshooting, and cleanup guidance.
- [ ] Review changes, create logical commits, and push according to the project owner's approval.
- [ ] Run the final demo from a fresh environment and clean up test resources.

Exit criterion: Another person can follow the guide to sign in, provision an ECS, and inspect it. The delivered version's limitations are clearly documented.

## 7. MVP Tools and Execution Contract

New tool names are proposals and will be finalized during implementation.

| Tool | Behavior | Main output |
| --- | --- | --- |
| `whoami` | Show the authenticated identity; already implemented | Identity and MCP scopes, without credentials |
| `get_ecs_options` | Return valid, authorized options for the limited demo environment | Images, flavors, network options, and limits |
| `plan_ecs` | Validate and store a configuration without creating resources | `plan_id`, configuration, plan expiry, and validation errors |
| `create_ecs` | Execute the stored plan after user approval in the client | `operation_id` and initial status |
| `get_operation` | Read and update operation status with bounded checks | In progress, succeeded, failed, or unknown |
| `get_ecs` | Read the actual state of an authorized resource | Identifier, status, IP, and relevant configuration |

Contract rules:

1. `plan_ecs` performs backend validation. It is not a cloud dry run, capacity reservation, or guarantee of success.
2. A `plan_id` is bound to identity, project, and immutable parameters. Configuration changes require a new plan.
3. One plan maps to one operation. Repeated calls for that plan return the existing operation. An ECS name alone is not a duplicate-prevention key.
4. If the response is lost after submission, record an `unknown` outcome and investigate using request, job, or resource identifiers. Do not resubmit creation until the outcome is resolved. Do not claim exactly-once guarantees beyond the API's actual capabilities.
5. Record `success` only after checking the ECS's actual state and configuration. Ending polling within one call does not mean creation failed; the client can query the same operation again.
6. Only the authorized owner may read or execute a plan or operation. Enforce project access and cloud permissions in the backend.
7. For the demo, human approval comes from Claude's tool-execution controls, with auto-approval disabled for write tools. A `confirmed=true` field or possession of a `plan_id` does not prove human approval.
8. Outputs and logs must not contain tokens, AK/SK, client secrets, encryption keys, or private keys.

## 8. Proposed Schedule

Estimate: Four working days plus one contingency day, conditional on successful P1 validation and available access. This is not a firm delivery commitment before federation is tested.

| Day | Focus | Demonstrable result |
| --- | --- | --- |
| 1 | P0, proof of P1, and initial authentication fixes | A real read-only request using the user's identity, with permission checks verified |
| 2 | Complete P2 and authentication testing | Provisioning options and ECS reads through MCP with user isolation |
| 3 | P3 | Create one ECS and track it to `ACTIVE`; prevent duplicate submission |
| 4 | P4 | Claude demo, runnable container, tests, and handoff guide |
| 5, contingency | Resolve API, IAM, quota, networking, or build issues | Repeat the demo and close remaining acceptance items |

If time is constrained, reduce image/flavor variety and the number of tested clients. Do not remove authorization checks, duplicate prevention, or accurate outcome reporting from acceptance criteria.

## 9. Acceptance Test Matrix

All tests below remain open until execution evidence is recorded. The two existing tests do not replace this matrix.

| ID | Test | Expected result | Status |
| --- | --- | --- | --- |
| T01 | Sign in through a real client and call `whoami` | Correct user identity and successful authenticated tool call | Open |
| T02 | Invalid/expired ID token, bad signature, or incorrect audience/nonce | Authentication rejected | Open |
| T03 | New MCP client with an existing SSO session | No token issued without client consent | Open |
| T04 | Code/refresh replay and concurrent refresh/revocation | No access issued or accepted after revocation | Open |
| T05 | Session, Keycloak token, or cloud credential expiry | Valid refresh or reauthentication required; no false success | Open |
| T06 | User without creation permissions or unauthorized project | No resource created | Open |
| T07 | A second user attempts to use another user's plan/operation | Access denied | Open |
| T08 | User rejects creation-tool execution in the client | No creation request submitted | Open |
| T09 | Insufficient quota, invalid image/flavor, or incompatible network | Clear error; any partial resources inspected and recorded | Open |
| T10 | Repeat the same plan, including concurrent calls | One operation and no duplicate creation submission | Open |
| T11 | Response lost after cloud request submission | Unknown state investigated; no automatic repeated POST | Open |
| T12 | Provision a real ECS from a prompt | Agreed configuration and `ACTIVE` state verified in API and console | Open |
| T13 | Restart the container with persistent Redis | Existing operation remains trackable; a valid session remains usable | Open |
| T14 | Inspect tool outputs and logs | No credential disclosure | Open |
| T15 | Clean up after live testing | Created resources removed according to the recorded inventory; leftover disks/ports checked | Open |

Run mock and local integration tests in CI. Run live cloud tests manually, with resource limits, in the test environment. MVP cleanup can use the console or a script restricted to that test's resource identifiers. Existing shared resources must not be targeted for cleanup.

## 10. Definition of Done

- [ ] Login and tool invocation through a real MCP client are verified.
- [ ] Pardis credentials are temporary and tied to the user; user isolation and project boundaries are tested.
- [ ] The user sees the final configuration and approves execution of the creation tool.
- [ ] A real ECS is provisioned with the agreed configuration and its state is read from the API.
- [ ] Repeated requests and network failures do not cause uncontrolled creation of extra resources.
- [ ] Permission, expiry, and quota errors are understandable and traceable.
- [ ] Critical acceptance tests pass and no critical defects remain in the write path.
- [ ] Container execution, persistent Redis, setup instructions, and cleanup are verified.
- [ ] Demo evidence, limitations, and the delivered version are recorded; sensitive data is absent from documentation and Git.

## 11. Progress and Decision Log

Allowed statuses: Open, In Progress, Blocked, and Done. Mark a phase Done only when its exit criterion is met. Label unverified conclusions as "Needs verification."

| Phase | Current status | Evidence / blocker | Next action |
| --- | --- | --- | --- |
| HTTP, Keycloak, and Redis foundation | Done with outstanding fixes | Evidence in Section 2; Section 3 fixes remain open | Complete P0 and start P1 |
| P0 | Open | Actual environment values are excluded from this document | Complete private configuration and prepare existing resources |
| P1 | In Progress | Federation, project discovery and ECS list code implemented; 21 local tests pass; no live cloud verification | Resolve the IAM endpoint, complete private configuration, reconnect, and run allowed/denied user tests |
| P2 | Open | Authentication fixes and cloud integration remain | Execute the checklist after proving P1 |
| P3 | Open | Only read-only listing exists; provisioning tools do not exist yet | Implement plan/create/status after P2 |
| P4 | Open | Live cloud tests and final container execution are unverified | Execute the test matrix and complete handoff after P3 |

For each significant run, record the date, code version, test ID, result, and sanitized notes. Keep actual resource identifiers, raw logs, and credentials in private evidence storage. Explicitly record any scope change or exception to an acceptance criterion.

Verification on 2026-10-05, uncommitted read-only integration: 21 tests passed using simulated services,
including HTTP tool calls and the installed SDK's request signing. Four deprecation warnings come from
the SDK's use of `datetime.utcnow`; they do not indicate failed tests. No cloud resources were accessed
or changed, no private configuration was changed, and no commit or push was performed for this phase.

Next executable step: Populate the verified private cloud configuration and prove P1 by reading ECS
instances using the signed-in user's temporary credentials, then verifying denial for an unauthorized user.

Follow-up on 2026-10-05: `PARDIS_PROJECT_ID` is now optional. `list_projects` discovers enabled
projects through the user's unscoped IAM token; `list_ecs` accepts an accessible project ID, uses
the optional configured default, or auto-selects the sole accessible project. The ECS endpoint remains
fixed to the deployment region. Tests cover zero/one/multiple projects and rejection of unknown IDs.
The local HTTP service was added to the user's Claude CLI configuration without secrets. An unauthenticated
ECS endpoint probe received HTTP 401, confirming DNS/TLS reachability. The documented IAM hostname
appears incomplete and the corrected candidate did not resolve; no token was sent to that candidate.
Live federation and ECS listing remain unverified pending a confirmed IAM API endpoint.
