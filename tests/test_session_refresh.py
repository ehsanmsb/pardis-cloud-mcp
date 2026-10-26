import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs

import fakeredis.aioredis
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider, UserSession, StoredAccessToken
from pardis_cloud_mcp.cloud import CloudCredentials, CloudError, CloudSettings, PardisCloud
from pardis_cloud_mcp.server import build_server
from pardis_cloud_mcp.session_guard import SessionUnavailable
from test_auth import make_settings


HTTP_CLIENT = httpx.AsyncClient
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PROJECT = "a" * 32


class Upstream:
    def __init__(self, provider):
        self.provider = provider
        self.roles = ["team-hr-full"]
        self.enabled = True
        self.fail = None
        self.calls = []
        self.expected_refresh = "SECRET-refresh-0"
        self.claim_overrides = {}
        self.omit_nonce = True
        self.entered = None
        self.release = None

    def claims(self, **changes):
        now = int(time.time())
        return {"iss": self.provider.settings.keycloak_issuer_url, "aud": self.provider.settings.keycloak_client_id,
                "sub": "alice", "iat": now, "exp": now + 300, "nonce": "login-nonce", "sid": "SSO-session",
                "preferred_username": "alice", "pardis_roles": self.roles.copy(), **changes}

    def signed(self, claims):
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})

    async def handler(self, request):
        assert str(request.url) == self.provider.settings.token_endpoint
        assert request.method == "POST"
        assert request.headers["Authorization"].startswith("Basic ")
        params = parse_qs(request.content.decode())
        assert params["grant_type"] == ["refresh_token"]
        self.calls.append(params["refresh_token"][0])
        if self.entered:
            self.entered.set()
            await self.release.wait()
        await asyncio.sleep(0.005)
        if self.fail == "timeout":
            raise httpx.ReadTimeout("SECRET-provider-detail", request=request)
        if self.fail:
            return httpx.Response(self.fail, json={"error": "invalid_client", "error_description": "SECRET-provider-detail"})
        if not self.enabled or params["refresh_token"][0] != self.expected_refresh:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "SECRET-disabled-or-stale"})
        self.expected_refresh = f"SECRET-refresh-{len(self.calls)}"
        claims = self.claims(**self.claim_overrides)
        if self.omit_nonce:
            claims.pop("nonce", None)
        return httpx.Response(200, json={"access_token": "SECRET-access", "id_token": self.signed(claims),
                                         "refresh_token": self.expected_refresh, "expires_in": 300})


async def setup(monkeypatch, *, session_ttl=180, expired=False):
    settings = replace(make_settings(), enabled_tools=("whoami",), cloud=CloudSettings(
        "https://iam.example.com", "https://ecs.example.com", PROJECT, "idp"))
    provider = RedisKeycloakOAuthProvider(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))
    provider._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=KEY.public_key()))
    upstream = Upstream(provider)
    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return HTTP_CLIENT(transport=httpx.MockTransport(upstream.handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    claims = upstream.claims(**({"exp": int(time.time()) - 1} if expired else {}))
    session = UserSession(session_id="login-session", subject="alice", claims=claims,
        keycloak_tokens={"id_token": upstream.signed(claims), "refresh_token": upstream.expected_refresh},
        authorization_version="initial-version")
    await provider._set_model(provider._key("session", session.session_id), session, session_ttl)
    token = await provider._mint_tokens(client_id="agent", scopes=["mcp:tools"], resource=settings.resource_url, session=session)
    return provider, upstream, session, token


def test_expired_id_token_is_refreshed_and_whoami_uses_latest_roles(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch, expired=True)
        upstream.roles = ["team-hr-readers"]
        app = build_server(provider.settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=provider.settings.oauth_issuer_url) as client:
                headers = {"Authorization": f"Bearer {token.access_token}", "Accept": "application/json, text/event-stream"}
                response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": "whoami", "arguments": {}}})
                assert response.status_code == 200, response.text
                assert response.json()["result"]["structuredContent"]["pardis_roles"] == ["team-hr-readers"]
                assert len(upstream.calls) == 1
                # No positive freshness cache across separate HTTP requests.
                await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                assert len(upstream.calls) == 2
        current = await provider._get_model(provider._key("session", session.session_id), UserSession)
        assert current.claims["exp"] > time.time()
        assert current.authorization_version != session.authorization_version
        assert current.oidc_binding["nonce"] == "login-nonce"
        # An omitted refresh nonce does not lose the original login binding.
        upstream.omit_nonce = False
        assert await provider.load_access_token(token.access_token)
        assert "SECRET" not in response.text
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_cloud_cache_is_evicted_and_old_generation_cannot_be_saved(monkeypatch):
    async def scenario():
        provider, upstream, original, token = await setup(monkeypatch)
        cloud = PardisCloud(provider.settings.cloud, provider)
        cache_key = cloud._cache_key(original)
        from datetime import datetime, timedelta, timezone
        credentials = CloudCredentials(access="SECRET-ak", secret="SECRET-sk", securitytoken="SECRET-sts",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=120), user_id="cloud-alice", user_name="alice", projects=[])
        assert await provider._save_token(cache_key, credentials, original.session_id, 120, original.authorization_version)
        upstream.roles = ["team-hr-readers"]
        current = await provider.session_for_access_token(token.access_token)
        assert current.authorization_version != original.authorization_version
        assert not await provider.redis.exists(cache_key)
        assert cloud._cache_key(current) != cache_key
        # A slow exchange started under full cannot repopulate the old cache after downgrade.
        assert not await provider._save_token(cache_key, credentials, original.session_id, 120, original.authorization_version)
        assert not await provider.session_is_current(token.access_token, original)
        assert await provider.session_is_current(token.access_token, current)
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_disable_returns_401_and_removes_local_tokens_and_credentials(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        await provider._save_token(provider._key("cloud", "old"), session, session.session_id, 60, session.authorization_version)
        upstream.enabled = False
        app = build_server(provider.settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=provider.settings.oauth_issuer_url) as client:
                response = await client.post("/mcp", headers={"Authorization": f"Bearer {token.access_token}"}, json={})
                assert response.status_code == 401
                assert "resource_metadata=" in response.headers["www-authenticate"]
                assert "SECRET" not in response.text
        assert not await provider.redis.exists(provider._key("session", session.session_id))
        assert not await provider.redis.exists(provider._key("access", provider._digest(token.access_token)))
        assert not await provider.redis.exists(provider._key("refresh", provider._digest(token.refresh_token)))
        assert not await provider.redis.exists(provider._key("cloud", "old"))
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", 401, 500, 302])
def test_transient_or_client_configuration_failure_returns_503_without_revocation(monkeypatch, failure):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        upstream.fail = failure
        before = await provider.redis.get(provider._key("session", session.session_id))
        app = build_server(provider.settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=provider.settings.oauth_issuer_url) as client:
                response = await client.post("/mcp", headers={"Authorization": f"Bearer {token.access_token}"}, json={})
                assert response.status_code == 503, response.text
                assert response.json()["error"] == "temporarily_unavailable"
                assert "www-authenticate" not in response.headers
                assert "SECRET" not in response.text
        assert await provider.redis.get(provider._key("session", session.session_id)) == before
        upstream.fail = None
        assert await provider.load_access_token(token.access_token)
        await provider.redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("claims", [{"sub": "bob"}, {"iss": "https://wrong.example"}, {"aud": "wrong-client"},
                                     {"sid": "different-session"}, {"azp": "wrong-client"}, {"pardis_roles": "full"},
                                     {"nonce": "different-login"}])
def test_untrusted_refreshed_identity_is_rejected(monkeypatch, claims):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        upstream.claim_overrides = claims
        if "nonce" in claims:
            upstream.omit_nonce = False
        assert await provider.load_access_token(token.access_token) is None
        assert not await provider.redis.exists(provider._key("session", session.session_id))
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_two_replicas_serialize_rotating_upstream_refresh(monkeypatch):
    async def scenario():
        first, upstream, _, token = await setup(monkeypatch)
        second = RedisKeycloakOAuthProvider(first.settings, first.redis)
        second._jwks = first._jwks
        results = await asyncio.gather(first.load_access_token(token.access_token), second.load_access_token(token.access_token))
        assert all(results)
        assert upstream.calls == ["SECRET-refresh-0", "SECRET-refresh-1"]
        assert not [key async for key in first.redis.scan_iter(match="*:session_refresh_lock:*")]
        await first.redis.aclose()
    asyncio.run(scenario())


def test_revocation_during_upstream_refresh_cannot_resurrect_session(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        upstream.entered, upstream.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(provider.load_access_token(token.access_token))
        await upstream.entered.wait()
        await provider._revoke_session(session.session_id)
        upstream.release.set()
        assert await task is None
        assert not await provider.redis.exists(provider._key("session", session.session_id))
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_outer_refresh_rotation_timeout_replay_and_session_lifetime(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch, session_ttl=60)
        client = OAuthClientInformationFull(client_id="agent", redirect_uris=["http://localhost/callback"])
        refresh = await provider.load_refresh_token(client, token.refresh_token)
        upstream.fail = "timeout"
        with pytest.raises(SessionUnavailable):
            await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        assert await provider.load_refresh_token(client, token.refresh_token)
        upstream.fail = None
        issued = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        assert 0 < issued.expires_in <= 60
        assert await provider.load_refresh_token(client, token.refresh_token) is None
        with pytest.raises(TokenError):
            await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        stored = await provider._get_model(provider._key("access", provider._digest(issued.access_token)), StoredAccessToken)
        assert stored.value.expires_at <= time.time() + 60
        assert 0 < await provider.redis.ttl(provider._key("session", session.session_id)) <= 60
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_request_validation_is_reused_inside_a_tool_but_not_across_requests(monkeypatch):
    async def scenario():
        from pardis_cloud_mcp.config import PlanningConfig
        provider, upstream, _, token = await setup(monkeypatch)
        settings = replace(provider.settings, enabled_tools=("get_deployment_options",), planning=PlanningConfig())
        app = build_server(settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=settings.oauth_issuer_url) as client:
                for i in range(2):
                    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token.access_token}",
                        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": i,
                        "method": "tools/call", "params": {"name": "get_deployment_options", "arguments": {}}})
                    assert response.status_code == 200 and not response.json()["result"].get("isError"), response.text
                    assert len(upstream.calls) == i + 1
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_lost_refresh_lease_cannot_revoke_session_or_unlock_successor(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        upstream.entered, upstream.release = asyncio.Event(), asyncio.Event()
        upstream.enabled = False
        task = asyncio.create_task(provider.load_access_token(token.access_token))
        await upstream.entered.wait()
        key = provider._key("session_refresh_lock", session.session_id)
        await provider.redis.set(key, "successor", ex=30)
        upstream.release.set()
        with pytest.raises(SessionUnavailable, match="lease"):
            await task
        assert await provider.redis.exists(provider._key("session", session.session_id))
        assert await provider.redis.get(key) == "successor"
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_revocation_racing_token_issuance_cannot_leave_new_tokens(monkeypatch):
    async def scenario():
        provider, _, session, _ = await setup(monkeypatch)
        real_pipeline = provider.redis.pipeline
        revoked = False
        def pipeline(*args, **kwargs):
            pipe = real_pipeline(*args, **kwargs)
            real_execute = pipe.execute
            async def execute(*args, **kwargs):
                nonlocal revoked
                if not revoked:
                    revoked = True
                    await provider._revoke_session(session.session_id)
                return await real_execute(*args, **kwargs)
            pipe.execute = execute
            return pipe
        monkeypatch.setattr(provider.redis, "pipeline", pipeline)
        with pytest.raises(TokenError):
            await provider._mint_tokens(client_id="agent", scopes=["mcp:tools"],
                                        resource=provider.settings.resource_url, session=session)
        assert not [key async for key in provider.redis.scan_iter(match="*:access:*")]
        assert not [key async for key in provider.redis.scan_iter(match="*:refresh:*")]
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_cloud_exchange_uses_refreshed_identity_after_downgrade_and_stops_on_disable(monkeypatch):
    async def scenario():
        from datetime import datetime, timedelta, timezone
        provider, upstream, _, token = await setup(monkeypatch)
        provider.settings = replace(provider.settings, enabled_tools=("list_ecs",))
        exchanged_roles, dispatched = [], []
        async def route(request):
            if request.url.host == "auth.example.com":
                return await upstream.handler(request)
            assert request.url.host == "iam.example.com"
            if request.url.path.endswith("id-token/tokens"):
                incoming = json.loads(request.content)["auth"]["id_token"]["id"]
                claims = jwt.decode(incoming, KEY.public_key(), algorithms=["RS256"],
                                    audience=provider.settings.keycloak_client_id)
                exchanged_roles.append(claims["pardis_roles"])
                return httpx.Response(201, headers={"X-Subject-Token": "SECRET-unscoped"}, json={"token": {
                    "user": {"id": "cloud-alice", "name": "alice", "OS-FEDERATION": {"identity_provider": {"id": "idp"}}}}})
            if request.method == "GET":
                return httpx.Response(200, json={"projects": []})
            return httpx.Response(201, json={"credential": {"access": exchanged_roles[-1][0], "secret": "SECRET-sk",
                "securitytoken": "SECRET-sts", "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(route), **kwargs))
        def ecs(self, credentials, *args):
            dispatched.append(credentials.access)
            return []
        monkeypatch.setattr(PardisCloud, "_list_servers", ecs)
        app = build_server(provider.settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=provider.settings.oauth_issuer_url) as client:
                async def call():
                    return await client.post("/mcp", headers={"Authorization": f"Bearer {token.access_token}",
                        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "list_ecs", "arguments": {}}})
                first = await call()
                assert not first.json()["result"].get("isError"), first.text
                upstream.roles = ["team-hr-readers"]
                second = await call()
                assert not second.json()["result"].get("isError"), second.text
                assert exchanged_roles == [["team-hr-full"], ["team-hr-readers"]]
                assert dispatched == ["team-hr-full", "team-hr-readers"]
                assert len(upstream.calls) == 2
                upstream.enabled = False
                assert (await call()).status_code == 401
                assert len(dispatched) == 2
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_http_outer_refresh_survives_outage_and_local_logout_does_not_need_provider(monkeypatch):
    async def scenario():
        provider, upstream, session, token = await setup(monkeypatch)
        await provider.register_client(OAuthClientInformationFull(client_id="agent", redirect_uris=["http://127.0.0.1/callback"],
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"], response_types=["code"]))
        app = build_server(provider.settings, provider).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with HTTP_CLIENT(transport=httpx.ASGITransport(app=app), base_url=provider.settings.oauth_issuer_url) as client:
                data = {"grant_type": "refresh_token", "refresh_token": token.refresh_token, "client_id": "agent",
                        "resource": provider.settings.resource_url}
                upstream.fail = "timeout"
                response = await client.post("/token", data=data)
                assert response.status_code == 503, response.text
                assert "SECRET" not in response.text
                upstream.fail = None
                upstream.roles = ["team-hr-readers"]
                response = await client.post("/token", data=data)
                assert response.status_code == 200, response.text
                fresh = response.json()
                assert fresh["access_token"] != token.access_token
                assert (await client.post("/token", data=data)).status_code == 400
                calls = len(upstream.calls)
                upstream.fail = "timeout"
                response = await client.post("/revoke", data={"token": fresh["access_token"], "client_id": "agent",
                                                              "client_secret": "", "token_type_hint": "access_token"})
                assert response.status_code == 200, response.text
                assert len(upstream.calls) == calls
        assert not await provider.redis.exists(provider._key("session", session.session_id))
        await provider.redis.aclose()
    asyncio.run(scenario())


def test_downgrade_during_slow_iam_exchange_cannot_reach_cloud_dispatch(monkeypatch):
    async def scenario():
        from datetime import datetime, timedelta, timezone
        provider, upstream, original, token = await setup(monkeypatch)
        cloud = PardisCloud(provider.settings.cloud, provider)
        started, release = asyncio.Event(), asyncio.Event()
        async def exchange(session):
            started.set()
            await release.wait()
            return CloudCredentials(access="OLD-full-ak", secret="SECRET-sk", securitytoken="SECRET-sts",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=900), user_id="alice", user_name="alice", projects=[])
        monkeypatch.setattr(cloud, "_exchange", exchange)
        monkeypatch.setattr(cloud, "_list_servers", lambda *args: pytest.fail("Stale full credential was dispatched"))
        task = asyncio.create_task(cloud.list_ecs(token.access_token))
        await started.wait()
        upstream.roles = ["team-hr-readers"]
        await provider.load_access_token(token.access_token)
        release.set()
        with pytest.raises(CloudError, match="access changed"):
            await task
        assert not await provider.redis.exists(cloud._cache_key(original))
        await provider.redis.aclose()
    asyncio.run(scenario())
