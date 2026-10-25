import asyncio
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import httpx
import pytest

from mcp_types import LATEST_PROTOCOL_VERSION
from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider, UserSession
from pardis_cloud_mcp.cloud import CloudError, CloudSettings, EcsSummary, PardisCloud
from pardis_cloud_mcp.server import build_server
from test_auth import make_settings

PROJECT = "a" * 32
REAL_ASYNC_CLIENT = httpx.AsyncClient


def make_cloud():
    settings = replace(make_settings(), cloud=CloudSettings(
        "https://iam.cloud.example.com", "https://ecs.cloud.example.com", PROJECT, "mcp-idp",
    ))
    provider = RedisKeycloakOAuthProvider(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))
    return settings, provider, PardisCloud(settings.cloud, provider)


async def login(provider, subject="alice", expires_in=300, claims=None):
    session = UserSession(
        session_id=f"session-{subject}", subject=subject,
        claims={"sub": subject, "iss": provider.settings.keycloak_issuer_url,
                "aud": provider.settings.keycloak_client_id, "exp": time.time() + expires_in,
                **(claims or {})},
        keycloak_tokens={"id_token": f"private-id-token-{subject}"},
    )
    await provider._set_model(provider._key("session", session.session_id), session, 3600)
    tokens = await provider._mint_tokens(client_id="test-client", scopes=["mcp:tools"],
                                         resource=provider.settings.resource_url, session=session)
    return session, tokens


def mock_iam(monkeypatch, *, status=201, credential_status=201, credential_lifetime=900, revoke=None, projects=None):
    calls = []

    async def handler(request):
        assert str(request.url).startswith("https://iam.cloud.example.com/")
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.method == "GET":
            assert request.url.path == "/v3/OS-FEDERATION/projects"
            assert request.headers["X-Auth-Token"].startswith("private-unscoped-")
            return httpx.Response(200, json={"projects": projects if projects is not None else [
                {"id": PROJECT, "name": "test-region", "enabled": True},
            ]})
        if request.url.path.endswith("id-token/tokens"):
            assert request.headers["X-Idp-Id"] == "mcp-idp"
            assert "scope" not in body["auth"]
            subject = body["auth"]["id_token"]["id"].removeprefix("private-id-token-")
            return httpx.Response(status, headers={"X-Subject-Token": f"private-unscoped-{subject}"}, json={
                "token": {"user": {"id": f"cloud-{subject}", "name": f"FederationUser-{subject}",
                    "OS-FEDERATION": {"identity_provider": {"id": "mcp-idp"}}}},
                "error_msg": "private-upstream-error-must-not-escape",
            })
        assert request.url.path == "/v3.0/OS-CREDENTIAL/securitytokens"
        identity = body["auth"]["identity"]
        assert identity["methods"] == ["token"]
        assert identity["token"]["duration_seconds"] == 900
        subject = identity["token"]["id"].removeprefix("private-unscoped-")
        if revoke:
            await revoke()
        return httpx.Response(credential_status, json={"credential": {
            "access": f"private-ak-{subject}", "secret": f"private-sk-{subject}",
            "securitytoken": f"private-sts-{subject}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=credential_lifetime)).isoformat(),
        }})

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["verify"] is not False
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("pardis_cloud_mcp.cloud.httpx.AsyncClient", client)
    return calls


def test_exchange_cache_isolation_encryption_and_revocation(monkeypatch, caplog):
    async def scenario():
        settings, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch)
        observed = []

        def fake_ecs(credentials, project_id, limit, marker, enterprise_project_id):
            assert project_id == PROJECT
            assert enterprise_project_id == "all_granted_eps"
            observed.append(credentials.access)
            assert credentials.secret.startswith("private-sk-")
            return [EcsSummary(id="server-1", name="demo", status="ACTIVE")]

        monkeypatch.setattr(cloud, "_list_servers", fake_ecs)
        caplog.set_level(logging.INFO, logger="pardis_cloud_mcp.cloud")
        alice, a = await login(provider)
        bob, b = await login(provider, "bob")
        page = await cloud.list_ecs(a.access_token, limit=1)
        assert page.next_marker == "server-1"
        assert page.cloud_user_id == "cloud-alice"
        await cloud.list_ecs(a.access_token)
        assert len(calls) == 3  # Cached credentials, no second exchange.
        await cloud.list_ecs(b.access_token)
        assert len(calls) == 6
        assert observed == ["private-ak-alice", "private-ak-alice", "private-ak-bob"]
        assert cloud._cache_key(alice) != cloud._cache_key(bob)
        key = cloud._cache_key(alice)
        assert 0 < await provider.redis.ttl(key) <= 870
        assert "private-" not in await provider.redis.get(key)
        assert "private-" not in page.model_dump_json()
        assert "private-" not in caplog.text
        assert '"operation": "list_ecs"' in caplog.text
        assert '"subject": "alice"' in caplog.text
        access = await provider.load_access_token(a.access_token)
        await provider.revoke_token(access)
        assert await provider.load_access_token(a.access_token) is None
        assert not await provider.redis.exists(key)
        with pytest.raises(CloudError, match="session"):
            await cloud.list_ecs(a.access_token)
        assert len(observed) == 3
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_iam_errors_are_safe_and_do_not_call_ecs(monkeypatch, status):
    async def scenario():
        _, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch, status=status)
        _, tokens = await login(provider)
        with pytest.raises(CloudError) as error:
            await cloud.list_ecs(tokens.access_token)
        assert "private-" not in str(error.value)
        assert len(calls) == 1
        assert not [key async for key in provider.redis.scan_iter(match="*:cloud:*")]
        await provider.redis.aclose()

    asyncio.run(scenario())


def test_expired_identity_or_credentials_fail_closed(monkeypatch):
    async def scenario():
        _, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch, credential_lifetime=-1)
        _, expired = await login(provider, expires_in=-1)
        with pytest.raises(CloudError, match="Your sign-in has expired"):
            await cloud.list_ecs(expired.access_token)
        assert calls == []
        _, current = await login(provider, "bob")
        with pytest.raises(CloudError, match="credentials expired"):
            await cloud.list_ecs(current.access_token)
        await provider.redis.aclose()

    asyncio.run(scenario())


def test_revocation_during_exchange_cannot_reach_ecs(monkeypatch):
    async def scenario():
        _, provider, cloud = make_cloud()
        _, tokens = await login(provider)
        access = await provider.load_access_token(tokens.access_token)
        mock_iam(monkeypatch, revoke=lambda: provider.revoke_token(access))
        with pytest.raises(CloudError, match="expired"):
            await cloud.list_ecs(tokens.access_token)
        assert await provider.load_access_token(tokens.access_token) is None
        await provider.redis.aclose()

    asyncio.run(scenario())


def test_configuration_is_explicit_and_https_only(monkeypatch):
    for name in ("PARDIS_IAM_ENDPOINT", "PARDIS_ECS_ENDPOINT", "PARDIS_PROJECT_ID", "PARDIS_IDP_ID"):
        monkeypatch.delenv(name, raising=False)
    assert CloudSettings.from_env() is None
    monkeypatch.setenv("PARDIS_IDP_ID", "mcp-idp")
    with pytest.raises(ValueError, match="all three"):
        CloudSettings.from_env()
    for endpoint in ("http://cloud.example.com", "https://user:secret@cloud.example.com", "https://cloud.example.com/path"):
        with pytest.raises(ValueError, match="HTTPS origins"):
            CloudSettings(endpoint, "https://ecs.example.com", PROJECT, "mcp-idp")


@pytest.mark.parametrize("enterprise_only", [False, True])
def test_mcp_http_initialization_discovery_tool_call_and_revocation(monkeypatch, enterprise_only):
    async def scenario():
        settings, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch, projects=[] if enterprise_only else None)
        selected = []

        def fake_ecs(_cloud, credentials, project_id, limit, marker, enterprise_project_id):
            selected.append(enterprise_project_id)
            assert project_id == PROJECT
            return [EcsSummary(id="server-1", name="demo", status="ACTIVE")]

        monkeypatch.setattr(PardisCloud, "_list_servers", fake_ecs)
        _, tokens = await login(provider)
        app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url=settings.oauth_issuer_url) as client:
                headers = {"Authorization": f"Bearer {tokens.access_token}",
                           "Accept": "application/json, text/event-stream"}

                async def rpc(method, params=None):
                    return await client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                    })

                init = await rpc("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {}, "clientInfo": {"name": "read-only-test", "version": "1"}})
                assert init.status_code == 200, init.text
                tools = (await rpc("tools/list")).json()["result"]["tools"]
                tool = next(t for t in tools if t["name"] == "list_ecs")
                assert tool["annotations"]["readOnlyHint"] is True
                assert set(tool["inputSchema"]["properties"]) == {"limit", "marker", "project_id", "enterprise_project_id"}
                assert any(t["name"] == "list_projects" for t in tools)
                assert "empty" in next(t for t in tools if t["name"] == "list_projects")["description"]
                identity = (await rpc("tools/call", {"name": "whoami"})).json()["result"]
                assert "subject" not in identity["structuredContent"]
                assert identity["structuredContent"]["pardis_roles"] == []
                result = (await rpc("tools/call", {"name": "list_ecs", "arguments": {"limit": 1}})).json()["result"]
                assert not result.get("isError")
                assert result["structuredContent"]["servers"][0]["id"] == "server-1"
                assert result["structuredContent"]["enterprise_project_id"] == "all_granted_eps"
                assert selected == ["all_granted_eps"]
                filtered = (await rpc("tools/call", {"name": "list_ecs", "arguments": {
                    "enterprise_project_id": "0",
                }})).json()["result"]
                assert not filtered.get("isError")
                assert selected == ["all_granted_eps", "0"]
                bad_scope = (await rpc("tools/call", {"name": "list_ecs", "arguments": {
                    "enterprise_project_id": "../marketing",
                }})).json()["result"]
                assert bad_scope["isError"]
                assert selected == ["all_granted_eps", "0"]
                assert "private-" not in json.dumps(result)
                invalid = (await rpc("tools/call", {"name": "list_ecs", "arguments": {"limit": 0}})).json()["result"]
                assert invalid["isError"]
                assert len(calls) == 3
                access = await provider.load_access_token(tokens.access_token)
                await provider.revoke_token(access)
                assert (await rpc("tools/list")).status_code == 401
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 401, 403, 500])
@pytest.mark.parametrize("enterprise_project_id", ["all_granted_eps", "0", "12345678-1234-1234-1234-123456789abc"])
def test_huawei_sdk_signs_only_configured_endpoint(monkeypatch, status, enterprise_project_id):
    import requests
    from pardis_cloud_mcp.cloud import CloudCredentials

    _, _, cloud = make_cloud()
    credentials = CloudCredentials(access="test-ak", secret="test-sk", securitytoken="test-security-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15), user_id="test-user", user_name="test-user", projects=[])
    calls = []

    def send(_session, request, **kwargs):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.startswith(f"https://ecs.cloud.example.com/v1/{PROJECT}/cloudservers/detail?")
        assert parse_qs(urlsplit(request.url).query) == {
            "limit": ["25"], "marker": ["previous-server"], "enterprise_project_id": [enterprise_project_id],
        }
        assert request.headers["X-Security-Token"] == "test-security-token"
        assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256")
        assert kwargs.get("verify", True) is not False
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json"
        response._content = (
            b'{"servers":[{"id":"server-1","name":"demo","status":"ACTIVE"}],"count":1}'
            if status == 200 else b'{"error":{"code":"denied","message":"private-upstream-error"}}'
        )
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    if status == 200:
        result = cloud._list_servers(credentials, PROJECT, 25, "previous-server", enterprise_project_id)
        assert result == [EcsSummary(id="server-1", name="demo", status="ACTIVE")]
    else:
        with pytest.raises(CloudError) as error:
            cloud._list_servers(credentials, PROJECT, 25, "previous-server", enterprise_project_id)
        assert "private-" not in str(error.value)
        if status == 403:
            assert "access denied" in str(error.value)
    assert len(calls) == 1


def test_expired_cache_reexchanges_and_changed_trust_does_not_reuse(monkeypatch):
    async def scenario():
        settings, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch)
        monkeypatch.setattr(cloud, "_list_servers", lambda *args: [])
        session, tokens = await login(provider)
        await cloud.list_ecs(tokens.access_token)
        await provider.redis.delete(cloud._cache_key(session))
        await cloud.list_ecs(tokens.access_token)
        assert len(calls) == 6
        other = PardisCloud(replace(settings.cloud, project_id="b" * 32), provider)
        assert other._cache_key(session) != cloud._cache_key(session)
        provider.settings = replace(settings, keycloak_issuer_url="https://other.example.com/realm")
        with pytest.raises(CloudError, match="OIDC configuration"):
            await cloud.list_ecs(tokens.access_token)
        assert len(calls) == 6
        await provider.redis.aclose()

    asyncio.run(scenario())


def test_live_access_token_cannot_outlive_deleted_session():
    async def scenario():
        _, provider, _ = make_cloud()
        session, tokens = await login(provider)
        await provider.redis.delete(provider._key("session", session.session_id))
        # Leave the access-token key behind, as in a revocation/refresh race.
        assert await provider.redis.exists(provider._key("access", provider._digest(tokens.access_token)))
        assert await provider.load_access_token(tokens.access_token) is None
        assert await provider.session_for_access_token(tokens.access_token) is None
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("project_count", [0, 1, 2])
def test_project_discovery_selection_and_access_boundary(monkeypatch, project_count):
    async def scenario():
        settings, provider, _ = make_cloud()
        cloud = PardisCloud(replace(settings.cloud, project_id=None), provider)
        projects = [{"id": char * 32, "name": f"project-{char}", "enabled": True}
                    for char in ["a", "b"][:project_count]]
        mock_iam(monkeypatch, projects=projects)
        selected = []
        def fake_ecs(credentials, project_id, limit, marker, enterprise_project_id):
            selected.append(project_id)
            return []
        monkeypatch.setattr(cloud, "_list_servers", fake_ecs)
        _, tokens = await login(provider)
        assert [p.id for p in await cloud.list_projects(tokens.access_token)] == [p["id"] for p in projects]
        if project_count == 1:
            assert (await cloud.list_ecs(tokens.access_token)).project_id == PROJECT
        elif project_count == 0:
            with pytest.raises(CloudError, match="configure PARDIS_PROJECT_ID"):
                await cloud.list_ecs(tokens.access_token)
        else:
            with pytest.raises(CloudError, match="select a project_id"):
                await cloud.list_ecs(tokens.access_token)
        if project_count == 2:
            assert (await cloud.list_ecs(tokens.access_token, project_id="b" * 32)).project_id == "b" * 32
        before = len(selected)
        with pytest.raises(CloudError, match="not in this user's"):
            await cloud.list_ecs(tokens.access_token, project_id="f" * 32)
        assert len(selected) == before
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_enterprise_only_users_use_configured_route_and_their_own_credentials(monkeypatch, caplog):
    async def scenario():
        _, provider, cloud = make_cloud()
        mock_iam(monkeypatch, projects=[])
        hr = "11111111-1111-1111-1111-111111111111"
        marketing = "22222222-2222-2222-2222-222222222222"
        calls = []

        def fake_ecs(credentials, project_id, limit, marker, enterprise_project_id):
            calls.append((credentials.user_id, project_id, enterprise_project_id, marker))
            assert credentials.access == f"private-ak-{credentials.user_id.removeprefix('cloud-')}"
            allowed = hr if credentials.user_id == "cloud-alice" else marketing
            if enterprise_project_id not in ("all_granted_eps", allowed):
                raise CloudError("ECS: access denied.", 403)
            return [EcsSummary(id=allowed, name="team-server", status="ACTIVE")]

        monkeypatch.setattr(cloud, "_list_servers", fake_ecs)
        caplog.set_level(logging.INFO, logger="pardis_cloud_mcp.cloud")
        session, alice = await login(provider)
        _, bob = await login(provider, "bob")
        assert await cloud.list_projects(alice.access_token) == []
        assert (await cloud.list_ecs(alice.access_token)).servers[0].id == hr
        assert (await cloud.list_ecs(bob.access_token)).servers[0].id == marketing
        page = await cloud.list_ecs(alice.access_token, limit=1, project_id=PROJECT, enterprise_project_id=hr)
        assert page.enterprise_project_id == hr
        assert page.next_marker == hr
        await cloud.list_ecs(alice.access_token, marker=page.next_marker, enterprise_project_id=hr)
        assert calls[-1] == ("cloud-alice", PROJECT, hr, hr)
        with pytest.raises(CloudError, match="access denied"):
            await cloud.list_ecs(alice.access_token, enterprise_project_id=marketing)
        assert not await provider.redis.exists(cloud._cache_key(session))
        before = len(calls)
        with pytest.raises(CloudError, match="not in this user's"):
            await cloud.list_ecs(alice.access_token, project_id="b" * 32)
        assert len(calls) == before
        assert '"enterprise_project_id"' in caplog.text
        assert "private-" not in caplog.text
        await provider.redis.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    {"project_id": "../other"},
    {"enterprise_project_id": "hr"},
    {"enterprise_project_id": "all"},
    {"enterprise_project_id": ""},
    {"enterprise_project_id": "0&limit=1000"},
])
def test_invalid_cloud_scope_is_rejected_before_iam(monkeypatch, arguments):
    async def scenario():
        _, provider, cloud = make_cloud()
        calls = mock_iam(monkeypatch)
        _, tokens = await login(provider)
        with pytest.raises(CloudError):
            await cloud.list_ecs(tokens.access_token, **arguments)
        assert calls == []
        await provider.redis.aclose()

    asyncio.run(scenario())
