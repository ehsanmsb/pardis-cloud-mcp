# Online session validation and automatic refresh

Available from v3.3.1; live Central Auth/Pardis acceptance is pending. No new callback,
Secret, scope, cloud permission, Admin API access or identity provider client is required.
The deployed v3.3.0 image retains the previous manual re-login behavior until upgraded.

## Why this change exists

Previously, MCP refreshed only its own opaque tokens. The original Central Auth ID token was never
renewed. When cloud credentials expired, another IAM exchange could fail because that original ID
token had expired. A role change or disabled account was also invisible to an existing local session.

The backend now uses the existing confidential client's upstream refresh token to request a new
ID/access token before accepting each authenticated HTTP request to `/mcp`. This checks the current
upstream session and obtains newly mapped `pardis_roles`; it does not wait for the old ID token to
expire. Every protected request does this, including planning and `whoami`. There is no positive
freshness cache between separate requests.

```mermaid
sequenceDiagram
    participant A as Agent
    participant M as MCP
    participant R as Redis
    participant I as Central Auth
    participant P as Pardis IAM / APIs
    A->>M: Request with opaque MCP token
    M->>R: Load live session; acquire session refresh lease
    M->>I: Refresh grant with confidential client authentication
    I-->>M: New tokens or session rejection
    M->>M: Verify signature, identity and login binding
    M->>R: Atomically save rotated tokens; keep absolute expiry
    Note over M,R: Changed roles invalidate previous cloud credential generation
    M->>P: Exchange new ID token if needed; use this user's credentials
    P-->>M: IAM-authorized result
    M-->>A: Safe tool output, or 401 / temporary failure
```

## Role changes and disabled accounts

- `whoami` returns current session claims, even if the opaque MCP token was originally issued with
  different roles. Old claims in stored token snapshots are never the authority for tool output.
- Authorization identity includes issuer, subject, client audience/authorized party and normalized
  `pardis_roles`. Organization attributes are display metadata, not permissions.
- Changing this identity/role fingerprint creates a new internal authorization version and evicts
  indexed cloud credentials. Cloud cache keys include that version. A late exchange using the previous
  version cannot repopulate its cache or reach another dispatch through the current session.
- Cloud calls compare session authorization version before dispatch and before returning results.
  A role change noticed by another request therefore invalidates an in-progress request's old context.
- On upstream `invalid_grant`, invalid refreshed identity, or a missing upstream refresh token, the
  local session and its indexed MCP/cloud tokens are revoked. Subsequent protected calls get `401`
  with MCP's OAuth discovery challenge. Browser re-login is initiated by the MCP client, not a redirect
  response from the cloud API. A disabled user must be denied by Central Auth during that login.
- Reader and full users can both prepare advisory documents. Actual Pardis resource access remains
  enforced by IAM with the user's newly mapped identity. This increment adds no create/delete tool.

The enforcement point is the next online authorization check after the change is visible to Central
Auth. A request already sent to Pardis cannot be canceled or undone. Removing a local credential
does not remotely revoke IAM's independently issued credential. No unsolicited browser popup or
instant push/event listener is implemented. Backchannel logout alone is not a general role-change
notification mechanism. Future queued operations and writes must perform a new online check at actual
dispatch, rather than reusing an earlier approval/build request's authentication context.

## Validation and coordination

- ID tokens are verified with the configured JWKS and RS256, expected issuer/audience, expiry and
  issued-at checks. Subject and authorized party must match the original client/user.
- Login nonce is mandatory at initial authentication. A refresh ID token may omit it; if included,
  it must match the original login. Original nonce/session/auth-time binding is retained independently
  so omitting it once does not lose the binding on later refreshes. Changed session IDs are rejected.
- Refresh requests use HTTPS client authentication, no redirects and no automatic retries. Neither
  the upstream refresh token nor upstream error bodies are returned to the agent.
- A Redis lease serializes refresh for the same session across replicas: a 30-second lease, at most
  12 seconds waiting for ownership, and a 12-second total upstream-refresh/verification budget. Each
  separate request still validates online after obtaining the lease; this is intentionally stricter
  than coalescing different requests onto an earlier positive decision.
- Compare-and-set transactions verify lease ownership and the original session before saving or
  revoking. An expired lease cannot overwrite/revoke a successor's session or unlock its lease.
- Within one active HTTP request, repeated internal session checks reuse that request's validation
  while still reading Redis for revocation/version changes. This avoids repeated upstream refreshes
  through nested tool/provider calls. The request context is invalidated when the response ends.
- MCP access/refresh token issuance and refresh-token consumption are atomic against the live
  session. Replayed MCP refresh tokens fail. Tokens and index TTLs never extend the absolute session.
  Revocation deletes the session before removing indexed tokens; late writers cannot restore it.

## Timeouts and failure behavior

| Condition | Result |
| --- | --- |
| Active upstream session; old ID token expired | Silent server-side refresh; no browser login required |
| Upstream session rejected / user disabled | Revoke local session; `401` on the protected request |
| Invalid refreshed signature, issuer, audience, subject or login binding | Revoke and reject |
| Central Auth timeout, 5xx, redirect or client-authentication configuration error | Fail closed; initial request validation returns `503 temporarily_unavailable`, without a login challenge or session deletion |
| Redis unavailable or refresh lease busy/lost | Fail closed with a temporary failure; never use stale credentials as fallback |
| MCP refresh endpoint hits an upstream temporary failure | `503`; its outer refresh token remains available for a retry |
| Explicit local token revocation | Works without contacting Central Auth |

Temporary errors do not automatically revoke the session. If an upstream refresh rotated its token
but its response was lost in transit, the backend cannot recover the new secret; a later authoritative
`invalid_grant` may still require re-login. Refresh requests are not blindly replayed to conceal this.
Mid-operation errors may appear as MCP tool errors after the HTTP response has begun; they do not
permit cloud dispatch with a stale identity.

The local session has a fixed absolute lifetime of eight hours by default. MCP access tokens are
at most 15 minutes and refresh tokens are capped to the remaining session lifetime. Upstream idle/max
timeouts can end the login earlier. Cloud credential duration remains requested as 900 seconds with
the existing expiry margin, but cached credentials can be used only after successful online session
validation and only under their current authorization version.

## Central Auth setup and migration

Use the existing confidential OIDC client with Standard Flow enabled. Confirm the login response
contains a normal upstream `refresh_token`, and refresh responses contain a newly generated ID token
with the `pardis_roles` mapper. Do not add `offline_access` to bypass normal session/logout controls.

Keep access/ID tokens short-lived. Configure SSO/client session idle and maximum lifetimes for the
expected work session and align client overrides; increasing token lifetime is not the fix. If refresh
rotation is enabled, keep its security settings and use the Redis coordination rather than broadening
token-reuse allowances to hide races. These choices require acceptance against the deployed Keycloak
version and existing client policies; the backend never edits them automatically.

Existing encrypted sessions with a valid upstream refresh token can be upgraded on their first
protected request. Old sessions without one need one new browser login. Keep the Fernet key and Redis
prefix stable. All replicas participating in strict enforcement must run the updated image; an older
replica can still accept stale claims, so drain it before asserting the new guarantee. No chart
template or environment change is needed beyond selecting the updated application image when released.

Online validation adds a Central Auth request and latency per protected MCP HTTP request. This is
the deliberate availability/load tradeoff for prompt role/disable enforcement; never silently fall
back to cached positive authorization if Central Auth is unreachable.

## Staging acceptance

1. Login once, call `whoami` and a permitted ECS read, then wait beyond the previous ID-token lifetime.
   Repeat without `/mcp` reauthentication; the calls should succeed while the upstream session is active.
2. Change full to readers in Central Auth. Without logging out, ask:
   `Use Pardis MCP to show my current identity and roles, then list the ECSs I can read. Do not modify anything.`
   Expect fresh reader claims and a new IAM exchange using the new ID token, without old cached full
   credentials. Compare resource visibility with the expected team grants.
3. Change team role and repeat. Confirm old-team data is not returned by protected cloud calls.
4. Disable the test user and make another MCP call. Expect `401` before a cloud call; re-login must
   fail at Central Auth. Re-enable and log in again for subsequent tests.
5. End the test user's SSO session and repeat: local MCP access must stop on the next online check.
6. Temporarily make Central Auth unreachable in the test environment. Expect temporary failure and
   no cloud calls, not an automatic login loop. Restore connectivity and retry the same login.
7. Send simultaneous calls with multiple MCP replicas and refresh-token rotation enabled. Verify
   successful serialized refreshes, no stale-token reuse and no token/session resurrection after revoke.

Automated tests use signed JWTs, mocked token/IAM endpoints and simulated Redis transactions:

```bash
uv run --locked pytest tests/test_session_refresh.py -q
uv run --locked pytest -q
```

They cover expiry, current `whoami`, reader downgrade reaching IAM with the new ID token, disable,
safe temporary failures, invalid identity/binding, two replicas, lost leases, replay, capped TTLs,
revocation during refresh/issuance and local logout. Live acceptance and future write authorization
cannot be proven by these tests alone.

References: [OIDC refresh response validation](https://openid.net/specs/openid-connect-core-1_0.html#RefreshTokenResponse),
[Keycloak token endpoints](https://www.keycloak.org/securing-apps/oidc-layers),
[Keycloak session/token timeouts](https://www.keycloak.org/docs/latest/server_admin/#_timeouts).
