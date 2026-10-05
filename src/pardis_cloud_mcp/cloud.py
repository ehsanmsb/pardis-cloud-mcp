"""Read-only Pardis integration using Huawei's federation API and ECS SDK."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider, UserSession

logger = logging.getLogger(__name__)


class CloudError(Exception):
    """Safe, fixed messages only; never include upstream bodies or credentials."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CloudSettings:
    iam_endpoint: str
    ecs_endpoint: str
    project_id: str | None
    idp_id: str
    ca_bundle: str | None = None

    def __post_init__(self) -> None:
        for endpoint in (self.iam_endpoint, self.ecs_endpoint):
            url = urlsplit(endpoint)
            if (
                url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment
            ):
                raise ValueError("Pardis endpoints must be HTTPS origins without credentials or paths")
        if self.project_id is not None and not re.fullmatch(r"[a-fA-F0-9]{32}", self.project_id):
            raise ValueError("PARDIS_PROJECT_ID must be a 32-character hexadecimal project ID")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.idp_id):
            raise ValueError("PARDIS_IDP_ID contains unsupported characters")

    @classmethod
    def from_env(cls) -> CloudSettings | None:
        names = ("PARDIS_IAM_ENDPOINT", "PARDIS_ECS_ENDPOINT", "PARDIS_IDP_ID")
        values = [os.getenv(name, "").strip() for name in names]
        if not any(values):
            return None
        if not all(values):
            raise ValueError("Set all three PARDIS_IAM_ENDPOINT, PARDIS_ECS_ENDPOINT, PARDIS_IDP_ID values")
        return cls(values[0], values[1], os.getenv("PARDIS_PROJECT_ID") or None, values[2],
                   ca_bundle=os.getenv("PARDIS_CA_BUNDLE") or None)


class CloudProject(BaseModel):
    id: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    name: str


class CloudCredentials(BaseModel):
    access: str = Field(min_length=1, repr=False)
    secret: str = Field(min_length=1, repr=False)
    securitytoken: str = Field(min_length=1, repr=False)
    expires_at: datetime
    user_id: str
    user_name: str
    projects: list[CloudProject]


class EcsSummary(BaseModel):
    id: str
    name: str
    status: str


class EcsPage(BaseModel):
    project_id: str
    cloud_user_id: str
    cloud_user_name: str
    servers: list[EcsSummary]
    next_marker: str | None
    audit_id: str


def _http_error(stage: str, status: int) -> CloudError:
    if status == 401:
        return CloudError(f"{stage}: authentication rejected. Sign in again; check the IdP trust configuration.", status)
    if status == 403:
        return CloudError(f"{stage}: access denied. Check the user's IAM group mapping and project permissions.", status)
    if status == 429:
        return CloudError(f"{stage}: rate limited. Try again later.")
    return CloudError(f"{stage}: upstream request failed (HTTP {status}). Check the configured endpoint and API support.")


class PardisCloud:
    def __init__(self, settings: CloudSettings, provider: RedisKeycloakOAuthProvider) -> None:
        self.settings = settings
        self.provider = provider
        self._tls = ssl.create_default_context(cafile=settings.ca_bundle)

    def _cache_key(self, session: UserSession) -> str:
        # Include trust configuration so changing projects/IdPs cannot reuse old credentials.
        identity = json.dumps([
            "federation-projects-v1",
            session.session_id, session.subject, self.provider.settings.keycloak_issuer_url,
            self.provider.settings.keycloak_client_id, self.settings.iam_endpoint,
            self.settings.ecs_endpoint, self.settings.project_id, self.settings.idp_id,
        ])
        return self.provider._key("cloud", hashlib.sha256(identity.encode()).hexdigest())

    async def _exchange(self, session: UserSession) -> CloudCredentials:
        # The claims and token were validated together at login and stored encrypted.
        # ponytail: require fresh login on ID-token expiry; add coordinated Keycloak refresh later.
        if float(session.claims.get("exp", 0)) <= time.time() + 30:
            raise CloudError("Keycloak ID token expired or is about to expire. Sign in to MCP again.")
        id_token = session.keycloak_tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise CloudError("Keycloak ID token is unavailable. Sign in to MCP again.")
        async with httpx.AsyncClient(timeout=15, verify=self._tls, follow_redirects=False) as client:
            response = await client.post(
                f"{self.settings.iam_endpoint.rstrip('/')}/v3.0/OS-AUTH/id-token/tokens",
                headers={"X-Idp-Id": self.settings.idp_id},
                json={"auth": {"id_token": {"id": id_token}}},
            )
            if response.status_code != 201:
                raise _http_error("IAM federation", response.status_code)
            try:
                unscoped_token = response.headers["X-Subject-Token"]
                user = response.json()["token"]["user"]
                if user["OS-FEDERATION"]["identity_provider"]["id"] != self.settings.idp_id:
                    raise CloudError("IAM returned an unexpected identity provider.")
                user_id, user_name = user["id"], user["name"]
                if not unscoped_token or not user_id or not user_name:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise CloudError("IAM federation returned an incomplete response.") from None
            response = await client.get(
                f"{self.settings.iam_endpoint.rstrip('/')}/v3/OS-FEDERATION/projects",
                headers={"X-Auth-Token": unscoped_token},
            )
            if response.status_code != 200:
                raise _http_error("IAM accessible projects", response.status_code)
            try:
                data = response.json()
                # Do not follow upstream links: they could move credentials to another host.
                if data.get("links", {}).get("next"):
                    raise CloudError("IAM returned paginated projects; complete project discovery is required.")
                projects = [CloudProject.model_validate(p) for p in data["projects"] if p.get("enabled") is True]
            except (KeyError, TypeError, ValueError):
                raise CloudError("IAM returned an invalid project list.") from None
            response = await client.post(
                f"{self.settings.iam_endpoint.rstrip('/')}/v3.0/OS-CREDENTIAL/securitytokens",
                json={"auth": {"identity": {"methods": ["token"], "token": {
                    "id": unscoped_token, "duration_seconds": 900,
                }}}},
            )
            if response.status_code != 201:
                raise _http_error("IAM temporary credentials", response.status_code)
            try:
                credentials = CloudCredentials(
                    **response.json()["credential"], user_id=user_id, user_name=user_name, projects=projects,
                )
                if credentials.expires_at.tzinfo is None:
                    raise ValueError
                return credentials
            except (KeyError, TypeError, ValueError):
                raise CloudError("IAM returned invalid temporary credentials.") from None

    async def _credentials(self, session: UserSession) -> CloudCredentials:
        audience = session.claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if (
            session.claims.get("iss") != self.provider.settings.keycloak_issuer_url
            or self.provider.settings.keycloak_client_id not in audiences
            or session.claims.get("sub") != session.subject
        ):
            raise CloudError("Session identity does not match the current OIDC configuration. Sign in again.")
        key = self._cache_key(session)
        cached = await self.provider._get_model(key, CloudCredentials)
        if isinstance(cached, CloudCredentials) and cached.expires_at.timestamp() > time.time() + 30:
            return cached
        credentials = await self._exchange(session)
        session_ttl = await self.provider.redis.ttl(self.provider._key("session", session.session_id))
        ttl = min(int(credentials.expires_at.timestamp() - time.time()) - 30, session_ttl)
        if ttl <= 0:
            raise CloudError("Session or cloud credentials expired. Sign in to MCP again.")
        # Cache is encrypted with the existing OAuth store's key and indexed for revocation.
        await self.provider._save_token(key, credentials, session.session_id, ttl)
        return credentials

    def _list_servers(self, credentials: CloudCredentials, project_id: str, limit: int, marker: str | None) -> list[EcsSummary]:
        # Import only when cloud integration is used; authentication-only mode stays independent.
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.exceptions.exceptions import ClientRequestException, SdkException
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest

        config = HttpConfig(timeout=(5, 15), retry_times=0, allow_redirects=False,
                            ssl_ca_cert=self.settings.ca_bundle)
        auth = BasicCredentials(credentials.access, credentials.secret, project_id)
        auth.with_security_token(credentials.securitytoken)
        client = (EcsClient.new_builder().with_http_config(config).with_credentials(auth)
                  .with_endpoint(self.settings.ecs_endpoint.rstrip('/')).build())
        try:
            response = client.list_servers_details(ListServersDetailsRequest(limit=limit, marker=marker))
            return [EcsSummary(id=item.id, name=item.name, status=item.status) for item in response.servers]
        except ClientRequestException as exc:
            raise _http_error("ECS", exc.status_code) from None
        except SdkException:
            raise CloudError("ECS request failed. Check connectivity, TLS trust, and service availability.") from None
        finally:
            client.close()

    async def list_projects(self, access_token: str) -> list[CloudProject]:
        session = await self.provider.session_for_access_token(access_token)
        if session is None:
            raise CloudError("MCP session is no longer valid. Sign in again.")
        try:
            credentials = await self._credentials(session)
            if await self.provider.session_for_access_token(access_token) is None:
                raise CloudError("MCP session was revoked or expired. Sign in again.")
            return credentials.projects
        except httpx.HTTPError:
            raise CloudError("IAM request failed. Check connectivity and TLS trust.") from None
        except (ValueError, TypeError, KeyError):
            raise CloudError("IAM returned an invalid response.") from None

    async def list_ecs(self, access_token: str, limit: int = 25, marker: str | None = None,
                       project_id: str | None = None) -> EcsPage:
        if not 1 <= limit <= 100 or (marker is not None and not re.fullmatch(r"[A-Za-z0-9-]{1,128}", marker)):
            raise CloudError("Use a limit of 1 to 100 and a valid server ID as the pagination marker.")
        audit_id = str(uuid.uuid4())
        session = await self.provider.session_for_access_token(access_token)
        if session is None:
            raise CloudError("MCP session is no longer valid. Sign in again.")
        outcome = "failed"
        try:
            credentials = await self._credentials(session)
            project_id = project_id or self.settings.project_id
            if project_id is None:
                if len(credentials.projects) != 1:
                    raise CloudError("Call list_projects and select a project_id explicitly; there is not exactly one accessible project.")
                project_id = credentials.projects[0].id
            if project_id not in {p.id for p in credentials.projects}:
                raise CloudError("The selected project is not in this user's accessible project list.")
            if await self.provider.session_for_access_token(access_token) is None:
                raise CloudError("MCP session was revoked or expired. Sign in again.")
            servers = await asyncio.to_thread(self._list_servers, credentials, project_id, limit, marker)
            if await self.provider.session_for_access_token(access_token) is None:
                raise CloudError("MCP session was revoked or expired. Sign in again.")
            outcome = "succeeded"
            return EcsPage(
                project_id=project_id, cloud_user_id=credentials.user_id,
                cloud_user_name=credentials.user_name, servers=servers,
                next_marker=servers[-1].id if len(servers) == limit else None, audit_id=audit_id,
            )
        except CloudError as exc:
            if exc.status_code in (401, 403):
                await self.provider.redis.delete(self._cache_key(session))
            raise
        except httpx.HTTPError:
            raise CloudError("Cloud request failed. Check connectivity, TLS trust, and service availability.") from None
        except (ValueError, TypeError, KeyError):
            raise CloudError("Cloud returned an invalid response.") from None
        finally:
            logger.info("cloud_audit %s", json.dumps({
                "audit_id": audit_id, "timestamp": datetime.now(timezone.utc).isoformat(),
                "operation": "list_ecs", "outcome": outcome, "subject": session.subject,
                "issuer": session.claims.get("iss"), "project_id": project_id,
            }))
