import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from pardis_cloud_mcp.cloud import CloudError, CloudSettings, PardisCloud
from pardis_cloud_mcp.discovery import DiscoveryQuery, EcsDiscovery
from pardis_cloud_mcp.server import build_server
from test_cloud import PROJECT, REAL_ASYNC_CLIENT, login, make_cloud, mock_iam


VPC = "11111111-1111-1111-1111-111111111111"
EP = "22222222-2222-2222-2222-222222222222"
CASES = {
    "enterprise_projects": ("eps", "/v1.0/enterprise-projects", "enterprise_projects", {"id": EP, "name": "HR", "status": 1}),
    "vpcs": ("vpc", f"/v1/{PROJECT}/vpcs", "vpcs", {"id": VPC, "name": "team-vpc", "enterprise_project_id": EP, "cidr": "10.0.0.0/16"}),
    "subnets": ("vpc", f"/v1/{PROJECT}/subnets", "subnets", {"id": "subnet-1", "name": "apps", "vpc_id": VPC, "neutron_network_id": "network-1"}),
    "security_groups": ("vpc", f"/v1/{PROJECT}/security-groups", "security_groups", {"id": "sg-1", "name": "team-apps", "enterprise_project_id": EP, "vpc_id": VPC}),
    "images": ("ims", "/v2/cloudimages", "images", {"id": "image-1", "name": "Linux", "min_disk": 20, "min_ram": 1024, "__os_type": "Linux"}),
    "flavors": ("ecs", f"/v1/{PROJECT}/cloudservers/flavors", "flavors", {"id": "small.1", "name": "small", "vcpus": "1", "ram": 1024, "disk": "0"}),
    "availability_zones": ("ecs", f"/v1/{PROJECT}/availability-zones", "availability_zones", {"availability_zone_id": "zone-a", "alias": "Zone A", "type": "Center", "mode": "shared"}),
}


def configured():
    settings, provider, _ = make_cloud()
    cloud_settings = replace(settings.cloud, vpc_endpoint="https://vpc.cloud.example.com",
                             ims_endpoint="https://ims.cloud.example.com", eps_endpoint="https://eps.cloud.example.com")
    settings = replace(settings, cloud=cloud_settings)
    return settings, provider, PardisCloud(cloud_settings, provider)


def routes(monkeypatch, handler, projects=None):
    iam_calls = mock_iam(monkeypatch, projects=projects)
    iam_client = httpx.AsyncClient
    calls = []

    async def dispatch(request):
        if request.url.host == "iam.cloud.example.com":
            async with iam_client(verify=True, follow_redirects=False, timeout=15) as client:
                return await client.send(request)
        assert request.method == "GET"
        assert not request.content
        assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
        assert request.headers["X-Security-Token"].startswith("private-sts-")
        assert request.headers.get("X-Project-Id") == (None if request.url.host.startswith("eps.") else PROJECT)
        calls.append(request)
        response = handler(request)
        return await response if hasattr(response, "__await__") else response

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["verify"] is not False
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(dispatch), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls, iam_calls


@pytest.mark.parametrize("kind", list(CASES))
def test_catalog_paths_signing_pagination_and_response_allowlists(monkeypatch, kind, caplog):
    async def scenario():
        _, provider, cloud = configured()
        host, path, key, row = CASES[kind]
        calls, iam = routes(monkeypatch, lambda request: httpx.Response(200, json={
            key: [{**row, "secret": "PRIVATE-RESPONSE", "security_group_rules": [{"access": "PRIVATE-RESPONSE"}]}],
            "next": "https://untrusted.example/do-not-follow"}), projects=[])
        _, tokens = await login(provider, claims={"pardis_roles": ["team-hr-readers"]})
        query = DiscoveryQuery(resource_type=kind, limit=1,
                               **({"offset": 3} if kind == "enterprise_projects" else {}),
                               **({"vpc_id": VPC} if kind == "subnets" else {}))
        page = await EcsDiscovery(cloud).list(tokens.access_token, query)
        assert calls[0].url.host == f"{host}.cloud.example.com" and calls[0].url.path == path
        assert len(calls) == 1 and len(iam) == 3
        assert page.project_id == (None if kind == "enterprise_projects" else PROJECT)
        assert page.items[0].id == row.get("id", row.get("availability_zone_id"))
        assert "PRIVATE-RESPONSE" not in page.model_dump_json()
        assert "private-ak" not in page.model_dump_json()
        assert page.next_offset == (4 if kind == "enterprise_projects" else None)
        assert page.next_marker == (page.items[0].id if kind not in ("enterprise_projects", "availability_zones") else None)
        if kind in ("vpcs", "security_groups"):
            assert calls[0].url.params["enterprise_project_id"] == "all_granted_eps"
        if kind == "images":
            assert calls[0].url.params["status"] == "active"
            assert calls[0].url.params["visibility"] == "public"
            assert calls[0].url.params["enterprise_project_id"] == "all_granted_eps"
        assert page.query == query
        await provider.redis.aclose()
    with caplog.at_level(logging.INFO, logger="pardis_cloud_mcp.discovery"):
        asyncio.run(scenario())
    assert '"outcome": "succeeded"' in caplog.text
    assert "PRIVATE-RESPONSE" not in caplog.text and "private-sts" not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_errors_never_echo_upstream_bodies_retry_or_follow_redirects(monkeypatch, status):
    async def scenario():
        _, provider, cloud = configured()
        calls, _ = routes(monkeypatch, lambda request: httpx.Response(status,
            headers={"Location": "https://untrusted.example"}, json={"message": "PRIVATE-UPSTREAM"}))
        session, tokens = await login(provider)
        with pytest.raises(CloudError) as error:
            await EcsDiscovery(cloud).list(tokens.access_token, DiscoveryQuery(resource_type="vpcs"))
        assert "PRIVATE" not in str(error.value)
        assert len(calls) == 1
        if status in (401, 403):
            assert not await provider.redis.exists(cloud._cache_key(session))
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,payload,kwargs", [
    ("vpcs", {"vpcs": "invalid"}, {}),
    ("vpcs", {"vpcs": [{"name": "missing id"}]}, {}),
    ("vpcs", {"vpcs": [CASES["vpcs"][3]] * 2}, {"limit": 1}),
    ("vpcs", {"vpcs": [{**CASES["vpcs"][3], "enterprise_project_id": "another-team"}]}, {"enterprise_project_id": EP}),
    ("subnets", {"subnets": [{**CASES["subnets"][3], "vpc_id": "another-vpc"}]}, {"vpc_id": VPC}),
])
def test_malformed_and_out_of_filter_results_are_not_returned(monkeypatch, kind, payload, kwargs):
    async def scenario():
        _, provider, cloud = configured()
        routes(monkeypatch, lambda request: httpx.Response(200, json=payload))
        _, tokens = await login(provider)
        with pytest.raises(CloudError):
            await EcsDiscovery(cloud).list(tokens.access_token, DiscoveryQuery(resource_type=kind, **kwargs))
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_scope_revocation_user_isolation_and_bounded_responses(monkeypatch):
    async def scenario():
        _, provider, cloud = configured()
        payload = {"vpcs": [CASES["vpcs"][3]]}
        calls, iam = routes(monkeypatch, lambda request: httpx.Response(200, json=payload))
        _, alice = await login(provider)
        _, bob = await login(provider, subject="bob", claims={"pardis_roles": ["team-marketing-full"]})
        service = EcsDiscovery(cloud)
        query = DiscoveryQuery(resource_type="vpcs")
        await service.list(alice.access_token, query)
        await service.list(bob.access_token, query)
        assert len(iam) == 6
        assert calls[0].headers["X-Security-Token"] != calls[1].headers["X-Security-Token"]
        with pytest.raises(CloudError, match="accessible project"):
            await service.list(alice.access_token, query.model_copy(update={"project_id": "b" * 32}))
        assert len(calls) == 2
        payload["ignored"] = "X" * (2 * 1024 * 1024)
        with pytest.raises(CloudError, match="too large"):
            await service.list(alice.access_token, query)
        async def revoked(endpoint, path, params, credentials, project_id):
            await provider.revoke_token(await provider.load_access_token(alice.access_token))
            return {"vpcs": [CASES["vpcs"][3]]}
        monkeypatch.setattr(service, "_get", revoked)
        with pytest.raises(CloudError, match="revoked"):
            await service.list(alice.access_token, query)
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"resource_type": "subnets"}, {"resource_type": "flavors", "offset": 1},
    {"resource_type": "enterprise_projects", "marker": "id"},
    {"resource_type": "availability_zones", "enterprise_project_id": EP},
    {"resource_type": "vpcs", "availability_zone": "zone-a"},
])
def test_unsupported_filter_combinations_do_not_contact_cloud(monkeypatch, kwargs):
    async def scenario():
        _, provider, cloud = configured()
        calls, iam = routes(monkeypatch, lambda request: pytest.fail("No discovery call expected"))
        _, tokens = await login(provider)
        with pytest.raises(CloudError):
            await EcsDiscovery(cloud).list(tokens.access_token, DiscoveryQuery(**kwargs))
        assert calls == [] and iam == []
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_endpoint_configuration_and_input_validation(monkeypatch):
    for name in ("PARDIS_IAM_ENDPOINT", "PARDIS_ECS_ENDPOINT", "PARDIS_IDP_ID", "PARDIS_PROJECT_ID",
                 "PARDIS_VPC_ENDPOINT", "PARDIS_IMS_ENDPOINT", "PARDIS_EPS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PARDIS_VPC_ENDPOINT", "https://vpc.example.com")
    with pytest.raises(ValueError, match="cloud configuration"):
        CloudSettings.from_env()
    monkeypatch.setenv("PARDIS_IAM_ENDPOINT", "https://iam.example.com")
    monkeypatch.setenv("PARDIS_ECS_ENDPOINT", "https://ecs.example.com")
    monkeypatch.setenv("PARDIS_IDP_ID", "idp")
    assert CloudSettings.from_env().vpc_endpoint == "https://vpc.example.com"
    for value in ("http://vpc.example.com", "https://user:password@vpc.example.com", "https://vpc.example.com/path"):
        with pytest.raises(ValueError, match="HTTPS origins"):
            CloudSettings("https://iam.example.com", "https://ecs.example.com", PROJECT, "idp", vpc_endpoint=value)
    with pytest.raises(ValidationError):
        DiscoveryQuery(resource_type="vpcs", vpc_id="../escape", limit=1000)
    async def scenario():
        _, provider, cloud = make_cloud()
        routes(monkeypatch, lambda request: pytest.fail("No request to an inferred endpoint"))
        _, tokens = await login(provider)
        with pytest.raises(CloudError, match="PARDIS_VPC_ENDPOINT"):
            await EcsDiscovery(cloud).list(tokens.access_token, DiscoveryQuery(resource_type="vpcs"))
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs,expected", [
    ({"resource_type": "images", "image_type": "private", "marker": "image-previous"},
     {"owner": PROJECT, "__imagetype": "private", "marker": "image-previous"}),
    ({"resource_type": "images", "image_type": "shared"},
     {"member_status": "accepted", "visibility": "shared", "__imagetype": "shared"}),
    ({"resource_type": "images", "enterprise_project_id": EP}, {"enterprise_project_id": EP}),
    ({"resource_type": "security_groups", "vpc_id": VPC, "enterprise_project_id": EP, "marker": "sg-previous"},
     {"vpc_id": VPC, "enterprise_project_id": EP, "marker": "sg-previous"}),
])
def test_specific_image_and_network_filters_are_forwarded(monkeypatch, kwargs, expected):
    async def scenario():
        _, provider, cloud = configured()
        kind = kwargs["resource_type"]
        _, _, key, row = CASES[kind]
        calls, _ = routes(monkeypatch, lambda request: httpx.Response(200, json={key: [row]}))
        _, tokens = await login(provider)
        await EcsDiscovery(cloud).list(tokens.access_token, DiscoveryQuery(**kwargs))
        assert all(calls[0].url.params[name] == value for name, value in expected.items())
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_authenticated_http_tool_catalog_and_call(monkeypatch):
    async def scenario():
        settings, provider, _ = configured()
        app = build_server(replace(settings, enabled_tools=("list_ecs_options",)), provider).streamable_http_app(
            json_response=True, stateless_http=True)
        _, tokens = await login(provider, claims={"pardis_roles": ["team-hr-readers"]})
        calls, _ = routes(monkeypatch, lambda request: httpx.Response(200, json={"flavors": [CASES["flavors"][3]]}))
        async with app.router.lifespan_context(app):
            async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url=settings.oauth_issuer_url) as client:
                async def rpc(method, params):
                    return await client.post("/mcp", headers={"Authorization": f"Bearer {tokens.access_token}",
                        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                                                                                 "method": method, "params": params})
                anonymous = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert anonymous.status_code == 401
                tools = (await rpc("tools/list", {})).json()["result"]["tools"]
                assert [tool["name"] for tool in tools] == ["list_ecs_options"]
                assert tools[0]["annotations"]["readOnlyHint"] is True
                response = await rpc("tools/call", {"name": "list_ecs_options", "arguments": {
                    "query": {"resource_type": "flavors", "availability_zone": "zone-a"}}})
                result = response.json()["result"]
                assert result["structuredContent"]["items"][0]["id"] == "small.1"
                assert calls[0].url.params["availability_zone"] == "zone-a"
                bad = await rpc("tools/call", {"name": "list_ecs_options", "arguments": {
                    "query": {"resource_type": "vpcs", "endpoint": "PRIVATE-INVALID-INPUT"}}})
                assert bad.json()["result"]["isError"]
                assert "PRIVATE-INVALID-INPUT" not in bad.text
        await provider.redis.aclose()
    asyncio.run(scenario())
