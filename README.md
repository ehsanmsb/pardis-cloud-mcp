# Pardis Cloud MCP

A minimal MCP server protected by Keycloak/OIDC. The first tool, `whoami`, proves that the server receives and validates the caller's access token.

This version authenticates the user to the MCP server. It does **not** yet exchange the Keycloak token for Pardis Cloud credentials or call Huawei/Pardis APIs.

## How authentication works

1. An MCP client connects to `/mcp` without a token.
2. The server responds with `401` and publishes OAuth Protected Resource Metadata.
3. The client discovers Keycloak from that metadata and completes Authorization Code + PKCE in the browser.
4. The client sends the Keycloak access token to this server.
5. The server verifies the JWT signature, issuer, audience, expiry, and required scope before any tool runs.

## Keycloak setup

Use a dedicated client/client-scope for this MCP server; do not reuse the Pardis console SSO client.

- Issuer: `https://<keycloak>/realms/<realm>`
- Flow: Authorization Code with PKCE S256
- Access-token audience: `pardis-mcp`
- Required scope: `mcp:tools`
- Signing algorithm: RS256

Add an Audience mapper so `pardis-mcp` appears in the access token's `aud` claim, and expose `mcp:tools` in its `scope` claim.

For automatic browser login, the MCP client must either be pre-registered in Keycloak or Keycloak Dynamic Client Registration must be enabled with a restricted policy. Do not enable unrestricted anonymous registration in production.

## Run locally

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) are recommended.

```bash
cp .env.example .env
# Edit .env, then export its values into your shell.
set -a; source .env; set +a

uv sync --extra dev
uv run pardis-cloud-mcp
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. Its discovery document is:

```text
http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
```

Run the tests with:

```bash
uv run pytest
```

## Configuration

| Variable | Required | Example |
| --- | --- | --- |
| `OIDC_ISSUER_URL` | yes | `https://keycloak.example.com/realms/snapp` |
| `OIDC_AUDIENCE` | yes | `pardis-mcp` |
| `MCP_RESOURCE_URL` | yes | `https://mcp.pardiscloud.ir/mcp` |
| `MCP_REQUIRED_SCOPE` | no | `mcp:tools` |
| `MCP_HOST` | no | `0.0.0.0` |
| `MCP_PORT` | no | `8000` |

In production, `MCP_RESOURCE_URL` must be the exact public HTTPS URL used by clients.

## Next safe increment

After login works end to end, add one read-only Pardis Cloud tool. Per-user cloud access should use an OIDC-to-STS exchange for temporary AK/SK/security-token credentials; never treat the Keycloak access token itself as a Huawei API credential.
