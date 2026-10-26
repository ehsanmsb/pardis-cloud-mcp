# Staging acceptance: deployment planning and cloud discovery

Dates: 2026-10-26–27 (Asia/Tehran). Contract under test: v3.4.0, application commit `4867d04`.
Status: **partial acceptance; real ECS selection remains blocked**.

Two dedicated test accounts were used, one with `team-hr-readonly` and one with
`team-marketing-readonly`. Endpoints, account emails, cloud identifiers and raw responses are
intentionally omitted. The live tool schema includes `prepare_deployment.ecs_selection`.
The running container digest was not inspected through a cluster connection.

## Method and boundaries

An isolated client used the installed official MCP SDK, Streamable HTTP, dynamic registration and
browser Authorization Code + PKCE. Each account used a separate client token store in memory.
No passwords were collected, no bearer/refresh tokens were written to disk, and no existing user's
CLI token files or backend Redis credentials were read. The source review used the repository's
synthetic Python HTTP fixture and its actual Git revision, without executing the application.

Cloud calls used existing read-only tools. The only application writes were short-lived deployment
drafts. No cloud resource, IAM grant, Central Auth role, service configuration or deployment changed.

## Passed live checks

| Check | Observed result |
| --- | --- |
| Health and OAuth discovery | Healthy MCP/Redis, authorization/protected-resource metadata, PKCE S256 |
| Anonymous and invalid bearer requests | 401 with OAuth challenge |
| Account identity | Both expected account emails and readonly roles returned by `whoami` |
| Tool catalog | Seven expected tools; selected ECS plan input present |
| Per-user federation | ECS responses contained different cloud-user identities for the two accounts |
| Regional routing | `list_projects` empty; configured regional route still permitted ECS API queries |
| Generic planning | Both reader accounts created a non-executable ECS advisory draft |
| Exact retrieval | Each account retrieved its own unchanged document and original expiry |
| Revision | HR created a second plan with a new ID; original remained unchanged and retrievable |
| Latest-plan stability after invalid input | Rejected assessment did not replace HR's latest valid plan |
| Caller role injection | Unexpected roles in the assessment rejected; test marker not echoed in the error |
| Owner isolation | Marketing denied access to both HR plans while those records were still unexpired; no HR document returned |
| Unknown plan | Safe unavailable/expired response |
| Availability zones | Both accounts received three catalog entries |
| Flavor catalog | Both accounts received five entries for the selected AZ with CPU/RAM metadata |
| Flavor pagination | Marketing received a second five-entry page with no overlap |
| Image catalog, larger request | Both accounts received 24 public active images with `limit=100` |
| Image continuation | A marker at the last observed image returned an empty final page |
| Missing subnet context | Query without a VPC rejected with an explicit selection prerequisite |

These passes do not establish a valid selected ECS placement, deployment permission, capacity or
resource-level cross-team isolation. ECS, VPC and security-group results were empty for both accounts;
a positive visible resource per team is needed to test actual cloud isolation. Owner isolation above
was tested in the Marketing-to-HR direction; the reverse direction has not been exercised live.

## Findings blocking full selection acceptance

### EPS authentication and missing domain header

Both accounts received an EPS 401 while their MCP identity, planning and other cloud catalog calls
continued to work. Repeating browser authentication is not evidence of a fix for this service error.

The current adapter constructs `GlobalCredentials(access, secret)` without a domain ID. A local
synthetic signing check confirmed that the outgoing global request lacks `X-Domain-Id`. The
[Huawei EPS list API](https://support.huaweicloud.com/intl/en-us/api-em/en-us_topic_0121230880.html)
documents this header as needed for AK/SK calls, with `X-Security-Token` additionally required for
temporary credentials. The latter header is already sent.

This is a confirmed request-construction omission and a likely contributor to the 401, not a
live-verified root-cause resolution. Next: derive the domain from trusted IAM federation data,
retain it with the user's temporary credentials, include it before signing global requests, test
cache/legacy credential behavior and retry on staging. Never accept a tool-supplied domain or guess
one from the regional project. This does not justify broadening user permissions.

### Empty network catalogs

VPC and security-group calls returned successful empty lists for both accounts. No accessible VPC
was available to discover real subnets or prepare a complete selection. The tests do not establish
whether resources are absent, filtered by grants or affected by service compatibility.

An operator should compare these accounts with Console and confirm read visibility of the intended
shared network and team security groups. Keep existing team isolation; no grant was changed during
these tests. Do not invent resource IDs or report a generic draft as a validated ECS selection.

### Image page-size compatibility

An HR image request with `limit=5` failed because the service returned more than the requested page
size. Increasing the request to 100 returned 24 images for each account. The adapter correctly
stopped the inconsistent small page rather than silently truncating it or claiming a complete list.

Investigate the deployed IMS pagination behavior and make the supported client contract explicit.
Do not silently skip records or trust fabricated continuation cursors when fixing this mismatch.

### A later IMS 401 without a browser-session failure

A subsequent Marketing image query with `limit=25` returned an authentication rejection. The next
cloud call succeeded after the adapter's credential-cache eviction; repeating the original `limit=25`
query later returned the same 24 images without a new browser login. Planning also kept working.
This is not evidence that the user's Central Auth session had expired.

The cause of that individual cloud authentication rejection was not proven. Investigate temporary
credential validity and sanitized upstream error/request identifiers before changing Central Auth
timeouts or adding any retry. If recovery is added, limit it to an authorized read with a fresh
current-session check and a bounded credential renewal; do not apply generic retries to future writes.

## Not yet accepted

- A complete real `ecs_selection`, actual flavor sizing in its document and a disk-only revision.
- Valid/invalid combinations involving actual visible VPCs, subnets and security groups.
- A corrected EPS request using the authenticated user's domain.
- Positive and negative cloud resource visibility across the two teams.
- Actual expiration of a staging draft, direct Redis TTL/encryption inspection and reverse owner isolation.
- Fresh role changes or account disable during this run; no Central Auth state was changed.
- Full-role provisioning, approval, builds and resource creation; no write adapter exists in this release.

The automated 244-test suite covers synthetic selection validation, expiry, encryption and concurrency;
it does not close the live gaps above. Resolve discovery and environment prerequisites before marking
D1/D3 complete or moving to a real deployment. D4 approval/idempotent fake execution remains the next
development stage after selection acceptance.
