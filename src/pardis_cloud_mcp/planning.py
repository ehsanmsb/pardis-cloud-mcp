"""Owner-only deployment drafts with optional read-only ECS selection checks."""
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel
from redis.exceptions import RedisError, WatchError

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider, UserSession
from pardis_cloud_mcp.config import PlanningConfig
from pardis_cloud_mcp.cloud import CloudError
from pardis_cloud_mcp.deployment import Actor, ResourcePreset, ResourceSize, SourceAssessment, Target
from pardis_cloud_mcp.deployment_document import render_document
from pardis_cloud_mcp.ecs_plan import EcsPlanSelection, EcsPlanVerifier, EcsSelection


logger = logging.getLogger(__name__)
PRESETS = {
    "cci": ResourcePreset(name="starter-cci", resources=ResourceSize(
        cpu_millicores=500, memory_mib=512, disk_gib=0, replicas=1)),
    "ecs": ResourcePreset(name="starter-ecs", resources=ResourceSize(
        cpu_millicores=1000, memory_mib=1024, disk_gib=20, replicas=1)),
}


class PlanningError(Exception):
    pass


class PlanningOptions(BaseModel):
    targets: list[Target]
    suggested_presets: dict[str, ResourcePreset]
    supported_review_stacks: list[str]
    cloud_access_verified: bool = False
    deployment_available: bool = False
    guidance: str


class PlanDocument(BaseModel):
    plan_id: str
    application_name: str
    target: Target
    status: Literal["draft"] = "draft"
    executable: Literal[False] = False
    expires_at: str
    document_markdown: str
    ecs_selection: EcsPlanSelection | None = None


class StoredPlan(BaseModel):
    owner: Actor
    created_at: int
    expires_at: int
    assessment: SourceAssessment
    preset: ResourcePreset
    content_hash: str
    document: PlanDocument


class LatestPlan(BaseModel):
    plan_id: str


class PlanningService:
    def __init__(self, provider: RedisKeycloakOAuthProvider, config: PlanningConfig,
                 ecs_verifier: EcsPlanVerifier | None = None):
        self.provider = provider
        self.config = config.model_copy(deep=True)
        self.ecs_verifier = ecs_verifier

    async def _identity(self, access_token: str) -> tuple[UserSession, Actor]:
        session = await self.provider.session_for_access_token(access_token)
        if session is None:
            raise PlanningError("Sign in again to prepare or view a deployment plan.")
        return session, Actor(issuer=self.provider.settings.keycloak_issuer_url, subject=session.subject)

    def _owner_key(self, owner: Actor) -> str:
        return self.provider._key("latest_plan", self.provider._digest(owner.model_dump_json()))

    async def _save_selected_plan(self, stored: StoredPlan, session: UserSession, ttl: int) -> None:
        """Commit both records only while the verified authorization generation is current."""
        provider = self.provider
        session_key = provider._key("session", session.session_id)
        async with provider.redis.pipeline() as pipe:
            try:
                await pipe.watch(session_key)
                current = await provider._get_model(session_key, UserSession)
                remaining = await pipe.ttl(session_key)
                ttl = min(ttl, remaining, stored.expires_at - int(time.time()))
                if (not isinstance(current, UserSession) or current.subject != session.subject
                        or current.authorization_version != session.authorization_version or ttl <= 0):
                    raise PlanningError("Your access changed before saving the ECS selection. Prepare the plan again.")
                record = provider._cipher.encrypt(stored.model_dump_json().encode()).decode()
                latest = provider._cipher.encrypt(LatestPlan(plan_id=stored.document.plan_id).model_dump_json().encode()).decode()
                pipe.multi()
                pipe.set(provider._key("deployment_plan", stored.document.plan_id), record, ex=ttl)
                pipe.set(self._owner_key(stored.owner), latest, ex=ttl)
                await pipe.execute()
            except WatchError:
                raise PlanningError("Your session changed while saving the ECS selection. Prepare the plan again.") from None

    async def options(self, access_token: str) -> PlanningOptions:
        await self._identity(access_token)
        return PlanningOptions(
            targets=["cci", "ecs"], suggested_presets=PRESETS,
            supported_review_stacks=["python-http", "node-http", "static-web", "unknown"],
            guidance=("These are advisory planning options, not verified cloud grants or available capacity. "
                      "Read the user's authorized source with your own file tools. Do not execute it or "
                      "send secrets. Use prepare_deployment with an evidence-based review. "
                      "Choose a candidate target and explain it; do not ask the user for cloud IDs. "
                      "For ECS, discover real choices with list_ecs_options and pass ecs_selection to "
                      "prepare_deployment for fresh backend checks. Omit it for a generic advisory draft. "
                      "The user must review the draft; nothing can be deployed by these tools."),
        )

    async def prepare(self, access_token: str, assessment: SourceAssessment,
                      application_name: str, target: Target, ecs_selection: EcsSelection | None = None) -> PlanDocument:
        session, owner = await self._identity(access_token)
        if len(assessment.model_dump_json().encode()) > 128 * 1024:
            raise PlanningError("The code review is too large. Send a shorter assessment without raw source.")
        verified = None
        if ecs_selection is not None:
            if target != "ecs":
                raise PlanningError("ECS selections require target=ecs. Omit them for a CCI advisory draft.")
            if self.ecs_verifier is None:
                raise PlanningError("ECS selection checks require cloud configuration and enabled list_ecs_options. Ask the operator to configure them, or omit ecs_selection for an advisory draft.")
            try:
                verified = await self.ecs_verifier.verify(access_token, ecs_selection)
            except CloudError as exc:
                raise PlanningError(f"ECS selection was not saved: {exc}") from None
            if not await self.provider.session_is_current(access_token, session):
                raise PlanningError("Your access changed while verifying ECS choices. Discover the options and prepare the plan again.")
        remaining = await self.provider.redis.ttl(self.provider._key("session", session.session_id))
        if remaining <= 0:
            raise PlanningError("Sign in again to prepare a deployment plan.")
        ttl = min(remaining, self.config.plan_ttl_seconds)
        created = int(time.time())
        expires = created + ttl
        preset = PRESETS[target].model_copy(deep=True)
        if verified is not None:
            preset = ResourcePreset(name="selected-ecs", resources=verified.resources)
        document = PlanDocument(
            plan_id="plan-" + secrets.token_hex(16), application_name=application_name,
            target=target, expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            document_markdown=render_document(application_name, assessment, target, preset, verified),
            ecs_selection=verified,
        )
        canonical = json.dumps({"assessment": assessment.model_dump(), "preset": preset.model_dump(),
                                "document": document.model_dump()},
                               sort_keys=True, separators=(",", ":"))
        stored = StoredPlan(owner=owner,
                            created_at=created, expires_at=expires, assessment=assessment, preset=preset,
                            content_hash=hashlib.sha256(canonical.encode()).hexdigest(), document=document)
        await self._identity(access_token)
        if verified is not None:
            await self._save_selected_plan(stored, session, ttl)
        else:
            await self.provider._set_model(self.provider._key("deployment_plan", document.plan_id), stored, ttl)
            await self.provider._set_model(self._owner_key(owner), LatestPlan(plan_id=document.plan_id), ttl)
        await self._identity(access_token)
        return document

    async def get(self, access_token: str, plan_id: str | None = None) -> PlanDocument:
        _, owner = await self._identity(access_token)
        if plan_id is None:
            latest = await self.provider._get_model(self._owner_key(owner), LatestPlan)
            plan_id = latest.plan_id if isinstance(latest, LatestPlan) else None
        unavailable = "This plan is unavailable or expired. Ask me to prepare a new deployment plan."
        if plan_id is None:
            raise PlanningError(unavailable)
        stored = await self.provider._get_model(self.provider._key("deployment_plan", plan_id), StoredPlan)
        if not isinstance(stored, StoredPlan) or stored.owner != owner or stored.expires_at <= time.time():
            raise PlanningError(unavailable)
        await self._identity(access_token)
        return stored.document

    async def call(self, action: str, access_token: str, **kwargs):
        """One audited boundary; no exception values, source text or credentials in audit logs."""
        outcome = "denied_or_failed"
        actor_hash = None
        result_plan_id = None
        try:
            session = await self.provider.session_for_access_token(access_token)
            if session is None:
                raise PlanningError("Sign in again to prepare or view a deployment plan.")
            owner = Actor(issuer=self.provider.settings.keycloak_issuer_url, subject=session.subject)
            actor_hash = self.provider._digest(owner.model_dump_json())
            result = await {"get_deployment_options": self.options,
                            "prepare_deployment": self.prepare,
                            "get_deployment_plan": self.get}[action](access_token, **kwargs)
            outcome = "succeeded"
            result_plan_id = result.plan_id if isinstance(result, PlanDocument) else None
            return result
        except RedisError:
            raise PlanningError("Plan storage is temporarily unavailable. No cloud operation was started.") from None
        except ValueError:
            raise PlanningError("Stored planning data is unavailable. Ask the MCP operator to check it.") from None
        finally:
            logger.info("planning_audit %s", json.dumps({"tool": action, "actor_hash": actor_hash,
                        "plan_id": result_plan_id,
                        "outcome": outcome, "timestamp": datetime.now(timezone.utc).isoformat()}))
