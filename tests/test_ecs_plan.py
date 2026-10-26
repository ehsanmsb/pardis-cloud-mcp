import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from pardis_cloud_mcp.config import PlanningConfig
from pardis_cloud_mcp.cloud import CloudError
from pardis_cloud_mcp.deployment import SourceAssessment
from pardis_cloud_mcp.discovery import DiscoveryQuery, EcsDiscovery
from pardis_cloud_mcp.ecs_plan import EcsPlanVerifier, EcsSelection
from pardis_cloud_mcp.planning import PlanningError, PlanningService, StoredPlan
from pardis_cloud_mcp.server import build_server
from test_cloud import PROJECT, REAL_ASYNC_CLIENT, login
from test_discovery import CASES, EP, VPC, configured, routes
from test_planning import TOOLS, assessment


CHOICES = dict(enterprise_project_id=EP, vpc_id=VPC, subnet_id="subnet-1", security_group_id="sg-1",
               image_id="image-1", flavor_id="small.1", availability_zone="zone-a", root_disk_gib=40)


def catalogs(monkeypatch, *, changes=None, denied=None):
    rows = {kind: copy.deepcopy(case[3]) for kind, case in CASES.items()}
    rows["flavors"].update(vcpus="2", ram=4096)
    rows["images"].update(status="active", __imagetype="gold")
    # Shared infrastructure is legitimate; the selected ECS's EP need not own its VPC.
    rows["vpcs"].update(status="OK", enterprise_project_id="0")
    rows["subnets"].update(status="ACTIVE")
    for kind, fields in (changes or {}).items():
        rows[kind].update(fields)

    def handler(request):
        kind = next(kind for kind, case in CASES.items() if request.url.path == case[1])
        if kind == denied:
            return httpx.Response(403, json={"message": "PRIVATE-CLOUD-RESPONSE"})
        return httpx.Response(200, json={CASES[kind][2]: [rows[kind]]})
    calls, iam = routes(monkeypatch, handler)
    return rows, calls, iam


@asynccontextmanager
async def running(provider, settings):
    app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url=settings.oauth_issuer_url) as client:
            async def rpc(token, name, arguments):
                response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                assert response.status_code == 200, response.text
                return response.json()["result"]
            yield rpc


@pytest.mark.parametrize("role", ["team-hr-readers", "team-hr-full"])
def test_http_selection_uses_fresh_cloud_facts_and_saves_exact_owner_only_document(monkeypatch, role):
    async def scenario():
        settings, provider, cloud = configured()
        settings = replace(settings, planning=PlanningConfig(), enabled_tools=(*TOOLS, "list_ecs_options"))
        _, calls, iam = catalogs(monkeypatch, changes={"images": {"name": "Linux <script> ![bad](https://untrusted.example)"}})
        _, alice = await login(provider, claims={"pardis_roles": [role]})
        async with running(provider, settings) as rpc:
            args = dict(assessment=assessment(), application_name="Example", target="ecs", ecs_selection=CHOICES)
            result = await rpc(alice.access_token, "prepare_deployment", args)
            assert not result.get("isError"), result
            plan = result["structuredContent"]
            selection = plan["ecs_selection"]
            assert plan["executable"] is False and selection["write_access"] == "not_checked"
            assert selection["selection"] == {**CHOICES, "project_id": PROJECT, "image_type": "gold"}
            assert selection["resources"] == dict(cpu_millicores=2000, memory_mib=4096, disk_gib=40, replicas=1)
            assert len(selection["catalog_audit_ids"]) == 7 and selection["unknowns"]
            assert "CPU: 2 vCPU" in plan["document_markdown"] and "Memory: 4096 MiB" in plan["document_markdown"]
            assert "Disk: 40 GiB" in plan["document_markdown"]
            assert "<script>" not in plan["document_markdown"] and "![bad](" not in plan["document_markdown"]
            assert "not verified" in plan["document_markdown"]
            assert len(calls) == 7 and len(iam) == 3
            flavor_request = next(call for call in calls if call.url.path.endswith("/flavors"))
            assert flavor_request.url.params["availability_zone"] == "zone-a"
            key = provider._key("deployment_plan", plan["plan_id"])
            stored = await provider._get_model(key, StoredPlan)
            assert stored.preset.resources.model_dump() == selection["resources"]
            canonical = json.dumps({"assessment": stored.assessment.model_dump(), "preset": stored.preset.model_dump(),
                                    "document": stored.document.model_dump()}, sort_keys=True, separators=(",", ":"))
            assert stored.content_hash == hashlib.sha256(canonical.encode()).hexdigest()
            assert "private-ak" not in stored.model_dump_json() and "private-id-token" not in stored.model_dump_json()
            encrypted = await provider.redis.get(key)
            assert "Example" not in encrypted and "small.1" not in encrypted
            ttl = await provider.redis.ttl(key)
            assert 0 < ttl <= 1800
            # Retrieval/restart preserves the observed snapshot and does not call cloud or extend TTL.
            assert (await PlanningService(provider, settings.planning).get(alice.access_token)).model_dump() == plan
            assert (await rpc(alice.access_token, "get_deployment_plan", {}))["structuredContent"] == plan
            assert await provider.redis.ttl(key) <= ttl and len(calls) == 7
            _, bob = await login(provider, subject="bob", claims={"pardis_roles": [role]})
            denied = await rpc(bob.access_token, "get_deployment_plan", {"plan_id": plan["plan_id"]})
            assert denied["isError"] and "small.1" not in json.dumps(denied)
            revised = await rpc(alice.access_token, "prepare_deployment", {**args, "ecs_selection": {**CHOICES, "root_disk_gib": 60}})
            assert not revised.get("isError"), revised
            second = revised["structuredContent"]
            assert second["plan_id"] != plan["plan_id"] and "Disk: 60 GiB" in second["document_markdown"]
            assert (await rpc(alice.access_token, "get_deployment_plan", {"plan_id": plan["plan_id"]}))["structuredContent"] == plan
            # Access changes preserve draft ownership, never authorize another cloud check.
            _, alice = await login(provider, claims={"pardis_roles": ["team-marketing-readers"]})
            assert (await rpc(alice.access_token, "get_deployment_plan", {}))["structuredContent"] == second
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,choices,error", [
    ({}, {"vpc_id": "invented-vpc"}, "no longer visible"),
    ({}, {"flavor_id": "invented-flavor"}, "no longer visible"),
    ({"subnets": {"vpc_id": "other-vpc"}}, {}, "outside the selected VPC"),
    ({"security_groups": {"vpc_id": "other-vpc"}}, {}, "different VPC"),
    ({"images": {"min_disk": 80}}, {}, "minimum requirement"),
    ({"images": {"min_ram": 8192}}, {}, "minimum requirement"),
    ({"images": {"min_ram": True}}, {}, "invalid sizing metadata"),
    ({"flavors": {"vcpus": "0"}}, {}, "planning bounds"),
    ({"flavors": {"ram": None}}, {}, "invalid sizing metadata"),
    ({"flavors": {"OS-FLV-DISABLED:disabled": True}}, {}, "disabled"),
    ({"images": {"status": "deleted"}}, {}, "usable status"),
    ({"images": {"__imagetype": "private"}}, {}, "image category"),
    ({"enterprise_projects": {"status": 2}}, {}, "usable status"),
    ({"vpcs": {"status": "CREATING"}}, {}, "usable status"),
])
def test_invalid_selection_does_not_create_or_replace_a_saved_plan(monkeypatch, changes, choices, error):
    async def scenario():
        settings, provider, cloud = configured()
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(EcsDiscovery(cloud)))
        _, token = await login(provider)
        source = SourceAssessment.model_validate(assessment())
        previous = await service.prepare(token.access_token, source, "Previous", "ecs")
        catalogs(monkeypatch, changes=changes)
        before = set(await provider.redis.keys("*:deployment_plan:*"))
        with pytest.raises(PlanningError, match=error):
            await service.prepare(token.access_token, source, "Invalid", "ecs", EcsSelection(**{**CHOICES, **choices}))
        assert set(await provider.redis.keys("*:deployment_plan:*")) == before
        assert await service.get(token.access_token) == previous
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_missing_metadata_is_explicitly_unverified(monkeypatch):
    async def scenario():
        _, provider, cloud = configured()
        catalogs(monkeypatch, changes={"security_groups": {"vpc_id": ""},
            "images": {"min_disk": None, "min_ram": None}, "vpcs": {"status": None}})
        _, token = await login(provider)
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(EcsDiscovery(cloud)))
        plan = await service.prepare(token.access_token, SourceAssessment.model_validate(assessment()), "Example", "ecs", EcsSelection(**CHOICES))
        assert "network compatibility is unverified" in plan.document_markdown
        assert "min\\_disk" in plan.document_markdown
        assert not any("security group belongs" in check for check in plan.ecs_selection.checks)
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("denied", list(CASES))
def test_catalog_denial_fails_closed_without_echoing_cloud_response(monkeypatch, denied):
    async def scenario():
        _, provider, cloud = configured()
        _, token = await login(provider)
        catalogs(monkeypatch, denied=denied)
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(EcsDiscovery(cloud)))
        with pytest.raises(PlanningError, match="access denied") as exc:
            await service.prepare(token.access_token, SourceAssessment.model_validate(assessment()), "Example", "ecs", EcsSelection(**CHOICES))
        assert "PRIVATE-CLOUD-RESPONSE" not in str(exc.value)
        assert await provider.redis.keys("*:deployment_plan:*") == []
        assert not await provider.redis.exists(cloud._cache_key(await provider.session_for_access_token(token.access_token)))
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_selection_requires_enabled_discovery_and_rejects_forged_snapshot_or_target(monkeypatch):
    async def scenario():
        settings, provider, _ = configured()
        settings = replace(settings, planning=PlanningConfig(), enabled_tools=TOOLS)
        _, tokens = await login(provider)
        _, calls, iam = catalogs(monkeypatch)
        async with running(provider, settings) as rpc:
            args = dict(assessment=assessment(), application_name="Example", target="ecs", ecs_selection=CHOICES)
            for changes in ({"resources": {"cpu_millicores": 1}}, {"enterprise_project_id": "all_granted_eps"},
                            {"vpc_id": "../PRIVATE-INVALID-INPUT"}, {"root_disk_gib": True},
                            {"write_access": "PRIVATE-INVALID-INPUT"}):
                response = await rpc(tokens.access_token, "prepare_deployment", {**args, "ecs_selection": {**CHOICES, **changes}})
                assert response["isError"] and "PRIVATE-INVALID-INPUT" not in json.dumps(response)
            response = await rpc(tokens.access_token, "prepare_deployment", {**args, "target": "cci"})
            assert response["isError"] and "target=ecs" in json.dumps(response)
            response = await rpc(tokens.access_token, "prepare_deployment", args)
            assert response["isError"] and "enabled list_ecs_options" in json.dumps(response)
            assert calls == [] and iam == []
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,selection_field", [("enterprise_projects", "enterprise_project_id"), ("flavors", "flavor_id")])
def test_verification_finds_real_selections_beyond_first_page(monkeypatch, kind, selection_field):
    async def scenario():
        _, provider, cloud = configured()
        selected = copy.deepcopy(CASES[kind][3])
        host, path, key, _ = CASES[kind]
        first_page = [{**selected, "id": f"earlier-{n}"} for n in range(100)]
        def handler(request):
            paged = request.url.params.get("offset") == "100" if kind == "enterprise_projects" else request.url.params.get("marker") == "earlier-99"
            return httpx.Response(200, json={key: [selected] if paged else first_page})
        calls, _ = routes(monkeypatch, handler)
        _, token = await login(provider)
        verifier = EcsPlanVerifier(EcsDiscovery(cloud))
        query = DiscoveryQuery(resource_type=kind, limit=100,
                               **({"project_id": PROJECT, "availability_zone": "zone-a"} if kind == "flavors" else {}))
        option, page = await verifier._find(token.access_token, CHOICES[selection_field], query)
        assert option.id == CHOICES[selection_field] and len(calls) == 2
        if kind == "flavors":
            assert all(call.url.params["availability_zone"] == "zone-a" for call in calls)
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("advances", [False, True])
def test_scan_is_bounded_and_repeated_cursors_fail_closed(monkeypatch, advances):
    async def scenario():
        _, provider, cloud = configured()
        def handler(request):
            prefix = request.url.params.get("marker", "start") + "x" if advances else "same"
            rows = [{**CASES["flavors"][3], "id": f"{prefix}-{n}"} for n in range(100)]
            return httpx.Response(200, json={"flavors": rows})
        calls, _ = routes(monkeypatch, handler)
        _, token = await login(provider)
        with pytest.raises(CloudError, match="scan limit" if advances else "did not advance"):
            await EcsPlanVerifier(EcsDiscovery(cloud))._find(token.access_token, "missing", DiscoveryQuery(
                resource_type="flavors", project_id=PROJECT, limit=100))
        assert len(calls) == (10 if advances else 2)
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["roles", "revoke"])
def test_access_change_during_catalog_checks_never_saves_mixed_generation(monkeypatch, change):
    async def scenario():
        _, provider, cloud = configured()
        session, token = await login(provider, claims={"pardis_roles": ["team-hr-full"]})
        discovery = EcsDiscovery(cloud)
        original = discovery.list
        catalogs(monkeypatch)
        async def changing(access_token, query):
            page = await original(access_token, query)
            if query.resource_type == "images":
                if change == "roles":
                    updated = session.model_copy(update={"authorization_version": "changed", "claims": {
                        **session.claims, "pardis_roles": ["team-hr-readers"]}})
                    await provider._set_model(provider._key("session", session.session_id), updated, 3600)
                else:
                    await provider._revoke_session(session.session_id)
            return page
        monkeypatch.setattr(discovery, "list", changing)
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(discovery))
        with pytest.raises(PlanningError, match="access changed"):
            await service.prepare(token.access_token, SourceAssessment.model_validate(assessment()), "Example", "ecs", EcsSelection(**CHOICES))
        assert await provider.redis.keys("*:deployment_plan:*") == []
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["eps_endpoint", "vpc_endpoint", "ims_endpoint"])
def test_unconfigured_endpoint_does_not_fall_back_to_public_cloud(monkeypatch, field):
    async def scenario():
        _, provider, cloud = configured()
        cloud.settings = replace(cloud.settings, **{field: None})
        catalogs(monkeypatch)
        _, token = await login(provider)
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(EcsDiscovery(cloud)))
        with pytest.raises(PlanningError, match="configure PARDIS_"):
            await service.prepare(token.access_token, SourceAssessment.model_validate(assessment()), "Example", "ecs", EcsSelection(**CHOICES))
        assert await provider.redis.keys("*:deployment_plan:*") == []
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_verification_deadline_cancels_lookup_without_saving(monkeypatch):
    async def scenario():
        _, provider, cloud = configured()
        _, token = await login(provider)
        discovery = EcsDiscovery(cloud)
        async def slow(*args):
            await asyncio.sleep(1)
            pytest.fail("Lookup must be cancelled")
        monkeypatch.setattr(discovery, "list", slow)
        timeout = asyncio.timeout
        monkeypatch.setattr("pardis_cloud_mcp.ecs_plan.asyncio.timeout", lambda seconds: timeout(0.01))
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(discovery))
        with pytest.raises(PlanningError, match="timed out"):
            await service.prepare(token.access_token, SourceAssessment.model_validate(assessment()), "Example", "ecs", EcsSelection(**CHOICES))
        assert await provider.redis.keys("*:deployment_plan:*") == []
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_session_change_at_redis_commit_preserves_previous_plan_and_pointer(monkeypatch):
    async def scenario():
        _, provider, cloud = configured()
        session, tokens = await login(provider)
        catalogs(monkeypatch)
        service = PlanningService(provider, PlanningConfig(), EcsPlanVerifier(EcsDiscovery(cloud)))
        source = SourceAssessment.model_validate(assessment())
        previous = await service.prepare(tokens.access_token, source, "Previous", "ecs")
        original = provider.redis.pipeline
        triggered = False
        def pipeline(*args, **kwargs):
            pipe = original(*args, **kwargs)
            execute = pipe.execute
            async def competing(*args, **kwargs):
                nonlocal triggered
                if any(":deployment_plan:" in str(command) for command, options in pipe.command_stack):
                    triggered = True
                    await provider._set_model(provider._key("session", session.session_id),
                        session.model_copy(update={"authorization_version": "changed-at-commit"}), 3600)
                return await execute(*args, **kwargs)
            pipe.execute = competing
            return pipe
        monkeypatch.setattr(provider.redis, "pipeline", pipeline)
        with pytest.raises(PlanningError, match="session changed while saving"):
            await service.prepare(tokens.access_token, source, "New", "ecs", EcsSelection(**CHOICES))
        assert triggered
        assert len(await provider.redis.keys("*:deployment_plan:*")) == 1
        assert await service.get(tokens.access_token) == previous
        await provider.redis.aclose()
    asyncio.run(scenario())
