# Pardis Cloud MCP

An HTTP MCP server with browser login through Keycloak. This phase authenticates users only; it does not call Pardis Cloud APIs yet.

## Architecture

```text
MCP agent                 Pardis MCP backend                 Keycloak
    | POST /mcp (no token)       |                              |
    |<---- 401 + discovery ------|                              |
    | register + /authorize ---->|                              |
    |<---- redirect to login ----|                              |
    |---------------- browser login --------------------------->|
    |                          callback + code <----------------|
    |                          code + client secret ----------->|
    |                          Keycloak tokens <----------------|
    |<---- opaque MCP token -----|                              |
    | POST /mcp + MCP token ---->|                              |
```

Redis stores the registered MCP clients, pending browser-login state, user sessions, Keycloak tokens, and MCP tokens. Every stored value containing credentials or identity data is encrypted with Fernet. Redis keys contain only hashes of bearer tokens.

The two clients are different:

- The **Keycloak client** (`OIDC_CLIENT_ID`) is confidential. Its secret exists only in this backend/container.
- The **MCP agent client** is dynamically registered and uses Authorization Code + PKCE. The MCP SDK may issue it a separate registration secret, but it never receives the Keycloak client secret or Keycloak tokens.

## Keycloak client

Create a dedicated OpenID Connect client, for example `pardis-mcp-backend`:

- Client authentication: **On** (confidential client)
- Standard flow: **On**
- Direct access grants: **Off**
- Valid redirect URI: `http://127.0.0.1:8000/oauth/keycloak/callback` for local use
- Web origins: not needed for the server-side code exchange
- ID token signing algorithm: RS256

Copy the generated secret into `OIDC_CLIENT_SECRET`. In production, use the public HTTPS callback URL instead of localhost and inject secrets through your deployment secret manager.

The Keycloak client does not need the `mcp:tools` client scope. That scope belongs to the outer MCP authorization server implemented by this application.

## Run locally

Prerequisites: Python 3.11+, Redis, and [`uv`](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Put the generated key and the Keycloak client secret in .env.

uv sync --extra dev
uv run pardis-cloud-mcp
```

Useful URLs:

- MCP: `http://127.0.0.1:8000/mcp`
- Health: `http://127.0.0.1:8000/health`
- OAuth metadata: `http://127.0.0.1:8000/.well-known/oauth-authorization-server`
- Protected resource metadata: `http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp`

Run automated tests (Keycloak and Redis are simulated in these tests):

```bash
uv run pytest
```

## Run in containers

After filling `.env`:

```bash
docker compose up --build
```

The compose stack starts both the MCP backend and a persistent Redis container. If you prefer your existing local Redis, run the MCP process directly with `REDIS_URL=redis://127.0.0.1:6379/0`.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `OIDC_ISSUER_URL` | yes | Keycloak realm URL |
| `OIDC_CLIENT_ID` | yes | Confidential Keycloak client ID |
| `OIDC_CLIENT_SECRET` | yes | Confidential Keycloak client secret |
| `MCP_OAUTH_ISSUER_URL` | yes | Public base URL of this OAuth/MCP backend |
| `MCP_RESOURCE_URL` | yes | Exact public MCP endpoint URL |
| `MCP_REQUIRED_SCOPE` | no | Outer MCP scope; defaults to `mcp:tools` |
| `REDIS_URL` | yes | Redis connection URL |
| `REDIS_PREFIX` | no | Prefix used for Redis keys |
| `TOKEN_ENCRYPTION_KEY` | yes | Fernet key used to encrypt Redis values |
| `MCP_HOST` | no | Bind host; defaults to `127.0.0.1` |
| `MCP_PORT` | no | Bind port; defaults to `8000` |

## Security boundaries

- Never put `OIDC_CLIENT_SECRET` or `TOKEN_ENCRYPTION_KEY` in Git.
- Use HTTPS for every public URL in production.
- Redis persistence is not a substitute for encryption; keep the Fernet key outside Redis.
- This implementation revokes every MCP access/refresh token in the same login session when one is revoked.
- Put rate limiting in front of public `/register`, `/authorize`, and `/token` endpoints before production deployment.
- Connecting the authenticated identity to Pardis Cloud permissions is intentionally deferred to the next phase.
