# ECS deployment discovery

Available from v3.3.0; live Pardis acceptance is pending. This increment adds
`list_ecs_options` to inspect real cloud catalogs before recommending ECS placement. It makes
signed GET requests only and reuses the existing login, temporary credentials and revocation path.
Readers and full users have the same tool; Pardis IAM authorizes each request with their own credentials.

## Operator setup

Enable `list_ecs_options` in `config.yaml` alongside the existing tools. The checked-in local
configuration already selects it. Restart the server and reconnect the client after changing tools.

Flavors and AZs use the existing `PARDIS_ECS_ENDPOINT`. Other catalogs require independently verified
API origins in the private environment, or under Helm `env`:

```dotenv
PARDIS_VPC_ENDPOINT=https://vpc.example.com
PARDIS_IMS_ENDPOINT=https://ims.example.com
PARDIS_EPS_ENDPOINT=https://eps.example.com
```

These are placeholders, not Pardis addresses. Do not infer origins from other hostnames or use Console
URLs. Only HTTPS origins without paths, embedded credentials or queries are accepted. Missing optional
endpoints produce a configuration error for the requested catalog; no public Huawei endpoint is used
as a fallback. No additional SDK package, static AK/SK, identity client or Secret key is required.
The existing `PARDIS_CA_BUNDLE` applies to the new services too.

The Helm ConfigMap already supports the new tool name. The chart now exposes the three optional
endpoint environment entries; enable the tool in your override only after selecting an image containing
this increment. A v3.2.1 image does not implement this tool and rejects its name at startup.

## Agent workflow

Give the agent read access to your sample application and send:

> Review my project and use Pardis MCP to discover real ECS deployment options available with my
> account: enterprise projects, VPCs, subnets, security groups, VM images, flavors and availability
> zones. Explain the choices in plain English, keeping technical IDs internal. Then prepare an
> advisory deployment document for ECS using the facts you could verify, and show any unavailable
> catalogs or unresolved choices separately. Do not execute source code, build anything, create
> resources or change permissions. Do not assume that listing an option grants deployment access.

The agent calls `list_ecs_options` once per catalog/page, selects networks by meaningful names and
asks ordinary questions when more than one choice is appropriate. It should discover a VPC before
listing its subnets, and may narrow flavors by an observed AZ. IDs belong in tool calls, not forms the
user has to complete. A catalog failure must remain explicit; an empty list does not justify granting
broader permissions. Shared infrastructure and public images can legitimately appear in these catalogs.

For a focused test:

> Use Pardis MCP to list the ECS availability zones and flavors I can query. Do not modify anything.

Or:

> Use Pardis MCP to show the VPCs visible to me, then list subnets of the VPC I choose by name.
> Keep all cloud operations read-only.

## Tool and API behavior

Input is one typed `query`; the agent supplies it. `resource_type` selects the fixed API:

| Resource type | Origin setting | GET path | Pagination |
| --- | --- | --- | --- |
| `enterprise_projects` | `PARDIS_EPS_ENDPOINT` | `/v1.0/enterprise-projects` | `offset` |
| `vpcs` | `PARDIS_VPC_ENDPOINT` | `/v1/{project_id}/vpcs` | `marker` |
| `subnets` | `PARDIS_VPC_ENDPOINT` | `/v1/{project_id}/subnets` | `marker`; discovered `vpc_id` required |
| `security_groups` | `PARDIS_VPC_ENDPOINT` | `/v1/{project_id}/security-groups` | `marker`; optional `vpc_id` |
| `images` | `PARDIS_IMS_ENDPOINT` | `/v2/cloudimages` | `marker` |
| `flavors` | `PARDIS_ECS_ENDPOINT` | `/v1/{project_id}/cloudservers/flavors` | `marker`; optional `availability_zone` |
| `availability_zones` | `PARDIS_ECS_ENDPOINT` | `/v1/{project_id}/availability-zones` | No pagination; bounded full catalog |

- Regional routing uses the existing configured `PARDIS_PROJECT_ID`, otherwise a sole discovered
  IAM project. Other caller-selected routes must be discovered; EPS is global and takes no project ID.
  The configured route bypasses discovery membership only, never service authorization.
- VPC, security-group and image lists default to `enterprise_project_id=all_granted_eps`. An explicit
  Enterprise Project ID narrows these calls. Unsupported Enterprise Project filters on other catalogs
  are rejected rather than silently ignored. Subnets are filtered by their selected VPC; this alone
  does not prove team ownership, permission to attach a NIC, or isolation between subnets.
- EPS lists projects manageable through that API, not an authoritative enumeration of every team
  grant. A reader may be denied EPS listing and still have ECS read access. Do not create a synthetic
  grant from role names, organization attributes, visible resource IDs or a configured project route.
- Images default to active public VM images. Private images use the regional project owner filter;
  shared images request accepted shares. Marketplace images are not selected by this tool.
- Page size is 1–100, default 25. Full pages return a conservative next cursor; the final follow-up
  page may be empty. Keep all original selectors unchanged. AZs return the whole catalog, up to 1000
  entries, because that endpoint has no pagination API. Responses larger than 2 MiB are rejected.
- Returned pages include the original query, regional route, observation time and audit ID. Options
  expose only allowlisted names, IDs and useful scalar details. Raw responses, credentials, group rules,
  host topology and upstream links are not forwarded. No upstream pagination URL is followed.
- Requests are signed by the installed Huawei SDK core using the same temporary AK/SK/security token.
  TLS verification stays enabled. Redirects and automatic retries are disabled. No automatic SDK
  project/domain discovery or public-endpoint selection occurs in the signing path.
- Revocation is checked before the service request and before returning results. Authentication/403
  errors evict cached cloud credentials and return safe messages; they do not trigger a broader login.
  Catalog audit events include operation/type, actor, selected scope, outcome and correlation ID.

## What this stage does not establish

Catalog visibility is not permission to create an ECS, a capacity reservation, compatible image/flavor/
subnet/zone combination, a quota check, pricing or approval. Availability-zone entries are catalog
facts, not a guarantee that a particular flavor is in stock. Existing ID-token freshness and cloud
credential caching behavior still applies; see [the integration runbook](READ_ONLY_INTEGRATION.md).

The v3.4.0 [ECS selection increment](ECS_PLAN_SELECTION.md) extends `prepare_deployment` with optional
`ecs_selection`: backend revalidation, persisted normalized choices, actual flavor sizing, provenance
and unresolved checks. Generic drafts retain fixed initial suggestions. Neither form is execution-ready;
approval, idempotent operations, build/delivery and cloud creation are not available yet.

## Verification

```bash
uv run --locked pytest tests/test_discovery.py tests/test_cloud.py tests/test_toolset.py -q
uv run --locked pytest -q
```

Mocked HTTP tests cover seven fixed GET paths, real SDK signing headers, user isolation, default regional
routing when IAM discovery is empty, explicit filters, cursors, safe errors, no redirects/retries,
malformed/oversized responses, revocation, missing endpoints and authenticated MCP tool calls.
They do not prove live API compatibility or team grants on Pardis. For staging, test each catalog with
reader/full users from both teams, compare visible networks with Console, verify an explicit forbidden
Enterprise Project filter is denied or empty, and record catalog failures without broadening IAM grants.

## API references

These primary Huawei references define the initial compatible routes; Pardis behavior still needs
live confirmation:

- [Enterprise Project list](https://support.huaweicloud.com/intl/en-us/ally-visitor-1-api-em/en-us_topic_0121230880.html)
- [VPC list](https://support.huaweicloud.com/intl/en-us/api-vpc/vpc_api01_0003.html)
- [Subnet list](https://support.huaweicloud.com/intl/en-us/api-vpc/vpc_subnet01_0003.html)
- [Security group list](https://support.huaweicloud.com/intl/en-us/api-vpc/vpc_sg01_0003.html)
- [Image list and Enterprise Project filtering](https://support.huaweicloud.com/intl/en-us/api-ims/ims_03_0602.html)
- [Flavor discovery in the ECS example](https://support.huaweicloud.com/intl/en-us/api-ecs/ecs_04_0001.html)
- [Current AZ list](https://support.huaweicloud.com/intl/en-us/api-ecs/ecs_02_1501.html)
