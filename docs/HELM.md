# Deploy Pardis Cloud MCP with Helm

The chart is in `helm/`. It creates one MCP Deployment, a ClusterIP Service, a ConfigMap containing
`config.yaml`, and an optional Contour HTTPProxy. Redis is the optional Bitnami chart dependency
pinned to version **25.5.3**. Redis is standalone with a 1 GiB persistent volume by default.

The initial chart configuration enables only `whoami`. Cloud tools can be enabled after setting their
Pardis endpoints and IdP. This chart packages the current MVP; the authentication limitations in
`MVP_PLAN.md` still apply.

## 1. Prepare the image and cluster

The [release workflow](RELEASE.md) publishes images to `ghcr.io/ehsanmsb/pardis-cloud-mcp`.
Set `image.tag` to an existing release version. The default `1.0.0` assumes the first automated release
has succeeded; adding the workflow alone does not publish that image. CI currently builds for `linux/amd64`.
For private GHCR packages, configure `imagePullSecrets` with credentials authorized to pull the package.

To build manually or for another registry/CPU architecture, build the repository's Dockerfile and push
an image your cluster can pull:

```bash
docker build -t YOUR_REGISTRY/pardis-cloud-mcp:YOUR_TAG .
docker push YOUR_REGISTRY/pardis-cloud-mcp:YOUR_TAG
```

The cluster needs a default StorageClass for Redis (or set `redis.master.persistence.storageClass`).
If HTTPProxy is enabled, Contour and its CRDs must already be installed and the `private` ingress class
must be handled by that installation. Point the chosen hostname at its ingress endpoint.

The supplied certificate reference is `example-certificates/exxample-com-tls`; replace it with your
actual TLS Secret. Referencing a TLS Secret in another namespace also requires a
`TLSCertificateDelegation` there allowing the MCP namespace. The chart does not create certificates,
delegation, Contour, or DNS records. See the [Contour API reference](https://projectcontour.io/docs/main/config/api-reference/).

## 2. Create the existing Secret

The default MCP `envFrom` and Redis authentication both reference **snapp-support-mcp** in the release
namespace. Create it before installing. The required keys are:

| Key | Used by |
| --- | --- |
| `OIDC_CLIENT_SECRET` | MCP's confidential Keycloak client |
| `TOKEN_ENCRYPTION_KEY` | MCP's Fernet encryption for Redis data |
| `REDIS_URL` | MCP's complete Redis connection URL, including credentials |
| `REDIS_PASSWORD` | The Redis dependency |

Generate a stable encryption key and a URL-safe Redis password locally:

```bash
uv run --no-sync python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
openssl rand -hex 32
```

Create a private `.env.helm` file with these values. It is ignored by Git. Replace all placeholders:

```dotenv
OIDC_CLIENT_SECRET=REPLACE_WITH_KEYCLOAK_CLIENT_SECRET
TOKEN_ENCRYPTION_KEY=REPLACE_WITH_FERNET_KEY
REDIS_PASSWORD=REPLACE_WITH_REDIS_PASSWORD
REDIS_URL=redis://:REPLACE_WITH_REDIS_PASSWORD@snapp-support-mcp-redis-master:6379/0
```

Use the **same password** in both Redis entries. The generated hexadecimal password needs no URL
escaping. For an existing password with reserved characters such as `@`, `:`, `/`, `#` or `%`, percent-encode
only the password in `REDIS_URL`; keep the raw password in `REDIS_PASSWORD`. If you change
`redis.fullnameOverride`, update the Redis hostname in `REDIS_URL` too.

```bash
chmod 600 .env.helm
kubectl create namespace pardis-mcp
kubectl -n pardis-mcp create secret generic snapp-support-mcp --from-env-file=.env.helm
```

Use an existing namespace if applicable. You can manage this Secret through your normal secret manager
instead. Do not put credentials in Helm values: values are stored in the Helm release. Keep the Fernet
key stable across upgrades so existing encrypted sessions remain readable.

## 3. Set deployment values

Create `helm/values.local.yaml` (ignored by Git) with overrides for your environment:

```yaml
image:
  repository: YOUR_REGISTRY/pardis-cloud-mcp
  tag: YOUR_TAG

env:
  OIDC_ISSUER_URL: https://keycloak.example.com/realms/example
  OIDC_CLIENT_ID: pardis-mcp-backend
  MCP_OAUTH_ISSUER_URL: https://pardis.mcp.example.com
  MCP_RESOURCE_URL: https://pardis.mcp.example.com/mcp

httpProxy:
  virtualhost:
    fqdn: pardis.mcp.example.com
    tls:
      enableFallbackCertificate: false
      secretName: example-certificates/exxample-com-tls
```

Set Keycloak's valid redirect URI to:

```text
https://pardis.mcp.example.com/oauth/keycloak/callback
```

The public hostname, MCP URLs and Keycloak callback must agree. HTTPProxy routes the entire `/` prefix
to MCP, including discovery, authorization, token and callback paths. It uses a configurable 300-second
response/idle timeout for MCP requests; see [Contour timeout semantics](https://projectcontour.io/docs/main/config/api-reference/#timeoutpolicy).

`env` is a map: add or change any non-secret variable there. To remove a default entry from an override,
set its value to `null` (Helm removes that map key). Keep required application settings, the Kubernetes
bind address `MCP_HOST: 0.0.0.0`, and a valid absolute `MCP_CONFIG_FILE` path. The chart mounts the
ConfigMap at that path and derives the container port from `MCP_PORT` (default 8000).

`envFrom` accepts standard Kubernetes references, so you can replace the default Secret or add more:

```yaml
envFrom:
  - secretRef:
      name: snapp-support-mcp
  - configMapRef:
      name: extra-mcp-environment
```

Explicit `env` entries override `envFrom` entries with the same name. If changing the shared Secret's
name, update `redis.auth.existingSecret` as well. Private registries can use `imagePullSecrets`.

## 4. Install or upgrade

Run from the repository root:

```bash
helm dependency update ./helm
helm lint ./helm -f helm/values.local.yaml
helm upgrade --install pardis-mcp ./helm \
  --namespace pardis-mcp \
  -f helm/values.local.yaml \
  --wait --timeout 5m
```

Redis chart version, image and security-context settings are taken from the supplied values. Its
`global.security.allowInsecureImages` override permits the requested `bitnamilegacy/redis` image
repository in Bitnami's image-name validation; it does not disable Redis authentication or TLS checks.
Dependency downloads require access to the specified chart repository. This guide does not imply
that the Redis chart or image has been pulled or tested in your cluster.

The fixed Redis fullname and Secret name are intended for one installation per namespace. For multiple
releases in the same namespace, give Redis and its Secret distinct names and update each `REDIS_URL`.

## 5. Verify and connect a client

```bash
kubectl -n pardis-mcp rollout status deployment/pardis-mcp
kubectl -n pardis-mcp get pods,svc,pvc,httpproxy
kubectl -n pardis-mcp describe httpproxy pardis-mcp
curl --fail https://pardis.mcp.example.com/health
```

Health should return `{"status":"ok","redis":"up"}` and HTTPProxy should report a valid status.
MCP readiness checks `/health`, including Redis connectivity. Liveness checks the TCP listener so a
Redis outage does not repeatedly restart the application. These checks do not verify Keycloak or IAM.

Connect an MCP client to `https://pardis.mcp.example.com/mcp`, complete browser login, and invoke
`whoami`. To inspect a pod that does not become ready, check its events and logs. Do not share credentials
or enable SDK/HTTP debug logging when collecting evidence.

## Change tools

Set the tool configuration in Helm values; this is separate from the repository-root `config.yaml`:

```yaml
config:
  tools:
    enabled:
      - whoami
      - list_projects
      - list_ecs
    disabled: []
env:
  PARDIS_IAM_ENDPOINT: https://iam.cloud.example.com
  PARDIS_ECS_ENDPOINT: https://ecs.cloud.example.com
  PARDIS_IDP_ID: your-programmatic-idp
```

Replace the example endpoints with verified Pardis origins. Run the same `helm upgrade --install`
command after changes. A ConfigMap checksum changes the pod template and triggers a rollout when tools
change. Reconnect your MCP client to refresh discovery. `enabled: []` disables all tools;
`enabled: ["*"]` selects all configured tools. `disabled` takes precedence.

Changing `env` also triggers a rollout. Updating the external Secret alone does not: after changing
Secret values, run `kubectl -n pardis-mcp rollout restart deployment/pardis-mcp`. Redis password changes
must also be coordinated with the Redis workload and its persistent data.

## External Redis or a different ingress

To use your own Redis:

```yaml
redis:
  enabled: false
```

Set `REDIS_URL` in the MCP Secret to that service's authenticated URL (`rediss://` for TLS).
`REDIS_PASSWORD` is only required by the bundled Redis dependency. No application code changes are needed.

To disable Contour resources, set `httpProxy.enabled: false` and route your own ingress to the MCP
ClusterIP Service, port 8000 by default. Keep the entire `/` path available for OAuth and MCP discovery.

Redis credentials and sessions are persistent; uninstalling or reinstalling a chart is not a data-reset
procedure. Manage PVC retention and backups according to your cluster's storage policy.

## Local template tests

```bash
uv run --locked --extra dev pytest -q tests/test_helm.py
```

These tests require the Helm CLI and render only the MCP templates from a temporary copy without the
Redis dependency. They cover ConfigMap rollout checksums, environment overrides/removal, Secret
references, service ports and HTTPProxy routing. They do not download or validate Redis, contact a
cluster, or verify a real deployment. `helm lint ./helm` after `helm dependency update` checks the
complete downloaded chart at deployment time.
