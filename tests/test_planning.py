import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp_types import LATEST_PROTOCOL_VERSION
from redis.exceptions import ConnectionError as RedisConnectionError

from pardis_cloud_mcp.config import PlanningConfig, load_config
from pardis_cloud_mcp.planning import PlanningService, StoredPlan
from pardis_cloud_mcp.server import build_server
from test_cloud import login, make_cloud


TOOLS = ("get_deployment_options", "prepare_deployment", "get_deployment_plan")
ROLES = ["team-hr-readonly", "team-marketing-readonly"]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "deployment"


def assessment(name="assessment.json"):
    return json.loads((EXAMPLES / name).read_text())


@asynccontextmanager
async def running():
    settings, provider, _ = make_cloud()
    settings = replace(settings, cloud=None, planning=PlanningConfig(), enabled_tools=TOOLS)
    app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.oauth_issuer_url) as client:
            async def rpc(token, name, arguments=None, method="tools/call"):
                params = {"name": name, "arguments": arguments or {}} if method == "tools/call" else arguments or {}
                response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream"}, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                assert response.status_code == 200, response.text
                return response.json()["result"]
            yield settings, provider, client, rpc
    await provider.redis.aclose()


@pytest.mark.parametrize("target,fixture", [("cci", "assessment.json"), ("ecs", "assessment.json"),
                                           ("cci", "assessment.needs-input.json")])
def test_prompt_planning_flow_saves_and_retrieves_document_without_cloud_calls(monkeypatch, target, fixture):
    async def scenario():
        cloud = AsyncMock(side_effect=AssertionError("Preview must never call cloud"))
        monkeypatch.setattr("pardis_cloud_mcp.cloud.PardisCloud._exchange", cloud)
        async with running() as (settings, provider, _, rpc):
            _, tokens = await login(provider, claims={"pardis_roles": [ROLES[1 if target == "ecs" else 0]]})
            init = await rpc(tokens.access_token, "", {"protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {}, "clientInfo": {"name": "planning-agent", "version": "1"}}, "initialize")
            assert "Do not send raw source" in init["instructions"]
            tools = (await rpc(tokens.access_token, "", method="tools/list"))["tools"]
            assert {tool["name"] for tool in tools} == set(TOOLS)
            prepare = next(tool for tool in tools if tool["name"] == "prepare_deployment")
            assert prepare["annotations"]["readOnlyHint"] is False
            assert set(prepare["inputSchema"]["properties"]) == {"assessment", "application_name", "target", "ecs_selection"}
            options = (await rpc(tokens.access_token, "get_deployment_options"))["structuredContent"]
            assert options["targets"] == ["cci", "ecs"]
            assert options["deployment_available"] is options["cloud_access_verified"] is False
            args = {"assessment": assessment(fixture), "application_name": "My sample app", "target": target}
            prepared = await rpc(tokens.access_token, "prepare_deployment", args)
            assert not prepared.get("isError"), prepared
            plan = prepared["structuredContent"]
            assert plan["executable"] is False and plan["status"] == "draft"
            assert f"**{target.upper()}**" in plan["document_markdown"]
            assert "Cost: **unknown**" in plan["document_markdown"]
            if fixture == "assessment.needs-input.json":
                assert "More information is needed" in plan["document_markdown"]
                assert "DATABASE\\_URL" in plan["document_markdown"]
            for args in ({}, {"plan_id": plan["plan_id"]}):
                retrieved = await rpc(tokens.access_token, "get_deployment_plan", args)
                assert retrieved["structuredContent"] == plan
            key = provider._key("deployment_plan", plan["plan_id"])
            encrypted = await provider.redis.get(key)
            assert "My sample app" not in encrypted
            assert 0 < await provider.redis.ttl(key) <= settings.planning.plan_ttl_seconds
            stored = await provider._get_model(key, StoredPlan)
            assert stored.owner.subject == "alice"
            assert len(stored.content_hash) == 64
            assert "private-id-token" not in stored.model_dump_json()
            # A restart does not regenerate the document or bypass authorization.
            service = PlanningService(provider, settings.planning)
            assert (await service.get(tokens.access_token)).model_dump() == plan
            # Revisions produce a different record; previous documents stay unchanged.
            second = await rpc(tokens.access_token, "prepare_deployment", {
                "assessment": assessment(), "application_name": "Revised app", "target": target})
            assert second["structuredContent"]["plan_id"] != plan["plan_id"]
            assert (await rpc(tokens.access_token, "get_deployment_plan"))["structuredContent"] == second["structuredContent"]
            assert (await rpc(tokens.access_token, "get_deployment_plan", {"plan_id": plan["plan_id"]}))["structuredContent"] == plan
        cloud.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("roles", [[], ["team-hr-readers"], ["team-hr-full"],
                                    ["team-marketing-readers"], ["team-marketing-full"], None])
def test_planning_is_available_to_authenticated_users_independent_of_cloud_roles(roles, caplog):
    async def scenario():
        async with running() as (_, provider, _, rpc):
            _, tokens = await login(provider, claims={"pardis_roles": roles, "Department": "Admin", "Team": "DevOps"})
            options = await rpc(tokens.access_token, "get_deployment_options")
            assert options["structuredContent"]["cloud_access_verified"] is False
            result = await rpc(tokens.access_token, "prepare_deployment", {
                "assessment": assessment(), "application_name": "My app"})
            assert not result.get("isError"), result
            plan = result["structuredContent"]
            assert plan["executable"] is False
            assert (await rpc(tokens.access_token, "get_deployment_plan"))["structuredContent"] == plan
    with caplog.at_level(logging.INFO, logger="pardis_cloud_mcp.planning"):
        asyncio.run(scenario())
    events = [json.loads(record.message.split("planning_audit ", 1)[1]) for record in caplog.records
              if record.message.startswith("planning_audit ")]
    assert len(events) == 3
    assert all(event["actor_hash"] and event["outcome"] == "succeeded" for event in events)
    assert "private-id-token" not in caplog.text


def test_owner_expiry_and_revocation_still_apply_when_roles_change():
    async def scenario():
        async with running() as (_, provider, client, rpc):
            for name, arguments in (("get_deployment_options", {}), ("get_deployment_plan", {}),
                                    ("prepare_deployment", {"assessment": assessment(), "application_name": "Anonymous"})):
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                           "params": {"name": name, "arguments": arguments}})
                assert response.status_code == 401
            _, alice = await login(provider, claims={"pardis_roles": [ROLES[0]]})
            plan = (await rpc(alice.access_token, "prepare_deployment", {
                "assessment": assessment(), "application_name": "Alice private plan"}))["structuredContent"]
            _, bob = await login(provider, subject="bob", claims={"pardis_roles": [ROLES[0]]})
            for args in ({}, {"plan_id": plan["plan_id"]}, {"plan_id": "plan-" + "0" * 32}):
                result = await rpc(bob.access_token, "get_deployment_plan", args)
                assert result["isError"]
                assert "Alice private plan" not in json.dumps(result)
            # A code-review draft belongs to the user, independently of team/cloud role changes.
            _, changed = await login(provider, claims={"pardis_roles": [ROLES[1]]})
            assert (await rpc(changed.access_token, "get_deployment_plan"))["structuredContent"] == plan
            _, alice = await login(provider, claims={"pardis_roles": []})
            # Old chart configuration is accepted but cannot restrict draft access.
            legacy_config = PlanningConfig(allowed_roles=["some-other-team"], plan_ttl_seconds=60)
            assert (await PlanningService(provider, legacy_config).get(alice.access_token)).model_dump() == plan
            key = provider._key("deployment_plan", plan["plan_id"])
            stored = await provider._get_model(key, StoredPlan)
            from pydantic import RootModel
            await provider._set_model(key, RootModel({**stored.model_dump(), "granted_roles": [ROLES[0]],
                                                      "policy_hash": "legacy-policy"}), 60)
            assert (await rpc(alice.access_token, "get_deployment_plan"))["structuredContent"] == plan
            await provider._set_model(key, stored.model_copy(update={"expires_at": 1}), 60)
            assert (await rpc(alice.access_token, "get_deployment_plan", {"plan_id": plan["plan_id"]}))["isError"]
            await provider.revoke_token(await provider.load_access_token(alice.access_token))
            response = await client.post("/mcp", headers={"Authorization": f"Bearer {alice.access_token}"},
                                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
    asyncio.run(scenario())


def test_input_safety_unknown_revision_and_storage_failure(monkeypatch):
    async def scenario():
        async with running() as (_, provider, _, rpc):
            _, tokens = await login(provider, claims={"pardis_roles": ROLES})
            source = assessment()
            source["source"].pop("revision")
            source["stack"] = "node-http"
            source["findings"][0]["message"] = '<script>alert(1)</script> ![image](https://untrusted.example)'
            result = await rpc(tokens.access_token, "prepare_deployment", {
                "assessment": source, "application_name": "Example"})
            doc = result["structuredContent"]["document_markdown"]
            assert "Not yet pinned" in doc
            assert "<script>" not in doc and "![image](" not in doc
            for source in (assessment("assessment.invalid.json"), {**assessment(), "pardis_roles": ["admin"]}):
                result = await rpc(tokens.access_token, "prepare_deployment", {"assessment": source, "application_name": "Bad"})
                assert result["isError"]
                assert "SYNTHETIC-DO-NOT-ECHO" not in json.dumps(result)
            original = provider._set_model
            async def fail_storage(key, value, ttl=None):
                if ":deployment_plan:" in key:
                    raise RedisConnectionError("private connection string must not escape")
                return await original(key, value, ttl)
            monkeypatch.setattr(provider, "_set_model", fail_storage)
            result = await rpc(tokens.access_token, "prepare_deployment", {"assessment": assessment(), "application_name": "Storage failure"})
            assert result["isError"]
            assert "temporarily unavailable" in result["content"][0]["text"]
            assert "private connection" not in json.dumps(result)
    asyncio.run(scenario())


def test_planning_configuration_and_tool_selection(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tools:\n  enabled: [get_deployment_options]\nplanning: {}\n")
    assert load_config(str(path)).planning.plan_ttl_seconds == 1800
    settings, provider, _ = make_cloud()
    with pytest.raises(ValueError, match="planning block"):
        build_server(replace(settings, enabled_tools=TOOLS), provider)
    async def scenario():
        configured = replace(settings, cloud=None, planning=PlanningConfig(),
                             enabled_tools=("*",), disabled_tools=("prepare_deployment",))
        server = build_server(configured, provider)
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {"whoami", "get_deployment_options", "get_deployment_plan"}
        await provider.redis.aclose()
    asyncio.run(scenario())
