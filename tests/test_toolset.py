import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from mcp_types import LATEST_PROTOCOL_VERSION
from pardis_cloud_mcp.cloud import PardisCloud
from pardis_cloud_mcp.config import load_config
from pardis_cloud_mcp.server import Settings, build_server
from test_auth import make_settings
from test_cloud import login, make_cloud


@pytest.mark.parametrize("claims,expected", [
    ({"Department": "Tech", "Team": "DevOps", "Venture": "Snapp",
      "pardis_roles": ["team-marketing-readonly"]},
     {"department": "Tech", "team": "DevOps", "venture": "Snapp", "vertical": None,
      "pardis_roles": ["team-marketing-readonly"]}),
    ({"Vertical": "Platform", "Venture": "Snapp",
      "pardis_roles": ["team-hr-readonly", "team-marketing-readonly"]},
     {"department": None, "team": None, "venture": "Snapp", "vertical": "Platform",
      "pardis_roles": ["team-hr-readonly", "team-marketing-readonly"]}),
    ({}, {"department": None, "team": None, "venture": None, "vertical": None, "pardis_roles": []}),
    ({"pardis_roles": None},
     {"department": None, "team": None, "venture": None, "vertical": None, "pardis_roles": []}),
    ({"pardis_roles": []},
     {"department": None, "team": None, "venture": None, "vertical": None, "pardis_roles": []}),
])
def test_whoami_organization_claims_over_authenticated_http(claims, expected):
    async def scenario():
        settings, provider, _ = make_cloud()
        settings = replace(settings, cloud=None, enabled_tools=("whoami",))
        _, tokens = await login(provider, claims={**claims, "private_claim": "not-for-output"})
        _, other_tokens = await login(provider, subject="bob")
        app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=settings.oauth_issuer_url) as client:
                async def rpc(token, method, params=None):
                    response = await client.post("/mcp", headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                    }, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                    assert response.status_code == 200, response.text
                    return response.json()["result"]

                await rpc(tokens.access_token, "initialize", {
                    "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "identity-test", "version": "1"},
                })
                tools = (await rpc(tokens.access_token, "tools/list"))["tools"]
                assert set(expected) <= tools[0]["outputSchema"]["properties"].keys()
                assert tools[0]["inputSchema"]["properties"]["include_details"]["default"] is False
                details = {"subject": "alice", "client_id": "test-client", "scopes": ["mcp:tools"]}
                assert not set(details) & set(tools[0]["outputSchema"].get("required", []))
                for arguments in ({}, {"include_details": False}, {"include_details": True}):
                    result = await rpc(tokens.access_token, "tools/call", {
                        "name": "whoami", "arguments": arguments,
                    })
                    assert not result.get("isError")
                    identity = {"username": None, "email": None, **expected}
                    if arguments.get("include_details"):
                        identity.update(details)
                    assert result["structuredContent"] == identity
                    assert json.loads(result["content"][0]["text"]) == identity
                other = await rpc(other_tokens.access_token, "tools/call", {
                    "name": "whoami", "arguments": {"include_details": True},
                })
                assert other["structuredContent"]["subject"] == "bob"
                assert all(other["structuredContent"][field] is None
                           for field in ("department", "team", "venture", "vertical"))
                assert other["structuredContent"]["pardis_roles"] == []
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled,disabled,with_cloud,expected", [
    (("*",), (), True, {"whoami", "list_projects", "list_ecs"}),
    (("*",), (), False, {"whoami"}),
    (("whoami", "list_ecs"), ("list_ecs",), True, {"whoami"}),
    (("list_projects", "list_projects"), (), True, {"list_projects"}),
    (("list_ecs",), (), True, {"list_ecs"}),
    ((), (), True, set()),
    (("*",), ("whoami", "list_projects", "list_ecs"), True, set()),
    (("*",), ("whoami",), False, set()),
    (("whoami", "list_ecs"), ("list_ecs",), False, {"whoami"}),
])
def test_selected_tools_are_the_only_discoverable_and_callable_tools(
    monkeypatch, tmp_path, enabled, disabled, with_cloud, expected,
):
    async def scenario():
        settings, provider, _ = make_cloud()
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"tools": {"enabled": list(enabled), "disabled": list(disabled)}}))
        config = load_config(str(path))
        settings = replace(settings, enabled_tools=tuple(config.tools.enabled),
                           disabled_tools=tuple(config.tools.disabled),
                           cloud=settings.cloud if with_cloud else None)
        list_ecs = AsyncMock(side_effect=AssertionError("Disabled ECS tool was executed"))
        list_projects = AsyncMock(side_effect=AssertionError("Disabled projects tool was executed"))
        monkeypatch.setattr(PardisCloud, "list_ecs", list_ecs)
        monkeypatch.setattr(PardisCloud, "list_projects", list_projects)
        _, tokens = await login(provider)
        app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=settings.oauth_issuer_url) as client:
                headers = {"Authorization": f"Bearer {tokens.access_token}",
                           "Accept": "application/json, text/event-stream"}

                async def rpc(method, params=None):
                    response = await client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                    })
                    assert response.status_code == 200, response.text
                    return response.json()["result"]

                await rpc("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {}, "clientInfo": {"name": "toolset-test", "version": "1"}})
                tools = (await rpc("tools/list"))["tools"]
                assert {t["name"] for t in tools} == expected
                assert all("keycloak" not in t.get("description", "").lower() for t in tools)
                for name in {"whoami", "list_projects", "list_ecs"} - expected:
                    result = await rpc("tools/call", {"name": name, "arguments": {}})
                    assert result["isError"] is True
                    assert f"Unknown tool: {name}" in result["content"][0]["text"]
                if "whoami" in expected:
                    result = await rpc("tools/call", {"name": "whoami"})
                    assert "subject" not in result["structuredContent"]
                    assert result["structuredContent"]["pardis_roles"] == []
                assert (await client.get("/health")).json()["status"] == "ok"
                assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 200
                assert (await client.post("/mcp", json={})).status_code == 401
        list_ecs.assert_not_awaited()
        list_projects.assert_not_awaited()
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled,disabled,with_cloud,message", [
    (("list_ec",), (), True, "Unknown MCP tools"),
    (("*",), ("list_ec",), True, "Unknown MCP tools"),
    (("*", "whoami"), (), True, 'must contain "\\*" alone'),
    (("*",), ("*",), True, "Unknown MCP tools"),
    (("list_ecs",), (), False, "Selected cloud tools require"),
    (("list_projects",), (), False, "Selected cloud tools require"),
])
def test_invalid_tool_selection_fails_startup(enabled, disabled, with_cloud, message):
    async def scenario():
        settings, provider, _ = make_cloud()
        settings = replace(settings, enabled_tools=enabled, disabled_tools=disabled,
                           cloud=settings.cloud if with_cloud else None)
        with pytest.raises(ValueError, match=message):
            build_server(settings, provider)
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_yaml_config_path_and_environment_settings(monkeypatch, tmp_path):
    settings = make_settings()
    values = {
        "OIDC_ISSUER_URL": settings.keycloak_issuer_url,
        "OIDC_CLIENT_ID": settings.keycloak_client_id,
        "OIDC_CLIENT_SECRET": settings.keycloak_client_secret,
        "MCP_OAUTH_ISSUER_URL": settings.oauth_issuer_url,
        "MCP_RESOURCE_URL": settings.resource_url,
        "REDIS_URL": settings.redis_url,
        "TOKEN_ENCRYPTION_KEY": settings.token_encryption_key,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("PARDIS_IAM_ENDPOINT", "PARDIS_ECS_ENDPOINT", "PARDIS_IDP_ID",
                "MCP_CONFIG_FILE", "MCP_ENABLED_TOOLS", "MCP_DISABLED_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    with pytest.raises(ValueError, match="Cannot read MCP YAML"):
        Settings.from_env()
    path.write_text("tools:\n  enabled:\n    - whoami\n  disabled: []\n")
    configured = Settings.from_env()
    assert configured.enabled_tools == ("whoami",)
    assert configured.disabled_tools == ()
    assert configured.keycloak_client_secret == settings.keycloak_client_secret
    override = tmp_path / "custom.yaml"
    override.write_text("tools:\n  enabled: []\n  disabled: [list_ecs]\n")
    monkeypatch.setenv("MCP_CONFIG_FILE", str(override))
    assert Settings.from_env().enabled_tools == ()
    assert Settings.from_env().disabled_tools == ("list_ecs",)
    monkeypatch.setenv("MCP_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(ValueError, match="Cannot read MCP YAML"):
        Settings.from_env()
    monkeypatch.setenv("MCP_CONFIG_FILE", str(path))
    for name in ("MCP_ENABLED_TOOLS", "MCP_DISABLED_TOOLS"):
        monkeypatch.setenv(name, "[]")
        with pytest.raises(ValueError, match="Move MCP_ENABLED_TOOLS/MCP_DISABLED_TOOLS"):
            Settings.from_env()
        monkeypatch.delenv(name)


@pytest.mark.parametrize("document", [
    "", "[]", "null", "tools: []", "tools: {}", "tools: {enabled: null}",
    "tools: {enabled: whoami}", "tools: {enabled: [whoami, 42]}",
    "tools: {enabled: [true]}", "tools: {enabled: [null]}",
    'tools: {enabled: [" "]}', "tools: {enabled: [], disabled: whoami}",
    "tools: {enabled: [], disabld: [list_ecs]}",
    "tool: {enabled: [whoami]}", "tools: {enabled: []}\nunknown: true",
    "tools: {enabled: [whoami", "!!python/object:builtins.object {}",
])
def test_invalid_or_unsafe_yaml_is_rejected(tmp_path, document):
    path = tmp_path / "config.yaml"
    path.write_text(document)
    with pytest.raises(ValueError):
        load_config(str(path))


def test_yaml_supports_comments_and_optional_disabled_list(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('''# Public tool configuration
tools:
  enabled:
    - whoami  # identity lookup
    - " list_ecs "
''')
    config = load_config(str(path))
    assert config.tools.enabled == ["whoami", "list_ecs"]
    assert config.tools.disabled == []
