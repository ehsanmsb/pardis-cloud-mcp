import asyncio
import base64
import hashlib
import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import fakeredis.aioredis
import httpx
from cryptography.fernet import Fernet

from pardis_cloud_mcp.auth import RedisKeycloakOAuthProvider
from pardis_cloud_mcp.auth_page import is_loopback_callback
from pardis_cloud_mcp.server import Settings, build_server


def make_settings() -> Settings:
    return Settings(
        keycloak_issuer_url="https://keycloak.example.com/realms/snapp",
        keycloak_client_id="pardis-mcp-backend",
        keycloak_client_secret="keycloak-secret",
        oauth_issuer_url="http://127.0.0.1:8000",
        resource_url="http://127.0.0.1:8000/mcp",
        redis_url="redis://127.0.0.1:6379/15",
        token_encryption_key=Fernet.generate_key().decode(),
    )


def pkce_pair() -> tuple[str, str]:
    verifier = "test-verifier-which-is-long-enough-for-pkce-0123456789"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def register_client(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        "/register",
        json={
            "redirect_uris": ["http://127.0.0.1:8765/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Test MCP Agent",
            "scope": "mcp:tools",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_oauth_browser_flow_stores_tokens_encrypted_in_redis():
    async def scenario():
        settings = make_settings()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        provider = RedisKeycloakOAuthProvider(settings, redis=redis)

        async def fake_exchange(code: str, verifier: str) -> dict:
            assert code == "keycloak-code"
            assert verifier
            return {
                "access_token": "raw-keycloak-access-token",
                "refresh_token": "raw-keycloak-refresh-token",
                "id_token": "fake-id-token",
            }

        provider._exchange_keycloak_code = fake_exchange
        provider._decode_id_token = lambda token, nonce: {
            "iss": settings.keycloak_issuer_url,
            "sub": "user-123",
            "aud": settings.keycloak_client_id,
            "nonce": nonce,
            "preferred_username": "ehsan",
            "email": "ehsan@example.com",
        }

        app = build_server(settings, provider).streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=settings.oauth_issuer_url,
            follow_redirects=False,
        ) as client:
            registered = await register_client(client)
            verifier, challenge = pkce_pair()
            authorize = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": registered["client_id"],
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "scope": "mcp:tools",
                    "state": "agent-state",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": settings.resource_url,
                },
            )
            assert authorize.status_code == 302
            keycloak_url = urlparse(authorize.headers["location"])
            keycloak_query = parse_qs(keycloak_url.query)
            assert f"{keycloak_url.scheme}://{keycloak_url.netloc}{keycloak_url.path}" == settings.authorization_endpoint
            assert keycloak_query["client_id"] == [settings.keycloak_client_id]
            assert keycloak_query["redirect_uri"] == [settings.keycloak_redirect_uri]
            assert keycloak_query["code_challenge_method"] == ["S256"]

            callback = await client.get(
                "/oauth/keycloak/callback",
                params={"code": "keycloak-code", "state": keycloak_query["state"][0]},
            )
            assert callback.status_code == 200
            assert "Sign-in complete" in callback.text
            assert "Close this tab" not in callback.text
            assert "Continue to agent" not in callback.text
            assert "Keycloak</span>" not in callback.text
            assert "Claude" not in callback.text
            assert callback.headers["cache-control"] == "no-store"
            assert "frame-src http://127.0.0.1:8765" in callback.headers["content-security-policy"]
            handoff = re.search(r'<iframe class="agent-handoff" src="([^"]+)"', callback.text)
            assert handoff is not None
            callback_query = parse_qs(urlparse(unescape(handoff.group(1))).query)
            assert callback_query["state"] == ["agent-state"]

            token = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": callback_query["code"][0],
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "client_id": registered["client_id"],
                    "code_verifier": verifier,
                    "resource": settings.resource_url,
                },
            )
            assert token.status_code == 200, token.text
            issued = token.json()
            assert issued["access_token"] != "raw-keycloak-access-token"
            loaded = await provider.load_access_token(issued["access_token"])
            assert loaded is not None
            assert loaded.subject == "user-123"
            assert loaded.claims["preferred_username"] == "ehsan"

            encrypted_values = [value async for value in redis.scan_iter(match="pardis-mcp:*")]
            assert encrypted_values
            for key in encrypted_values:
                key_type = key.split(":")[-2]
                if key_type != "session_tokens":
                    stored = await redis.get(key)
                    assert "raw-keycloak-refresh-token" not in stored
                    assert "raw-keycloak-access-token" not in stored

        await redis.aclose()

    asyncio.run(scenario())


def test_only_loopback_http_callbacks_use_completion_page():
    assert is_loopback_callback("http://localhost:53123/callback")
    assert is_loopback_callback("http://[::1]:53123/callback")
    assert not is_loopback_callback("https://agent.example.com/callback")
    assert not is_loopback_callback("http://agent.example.com/callback")


def test_discovery_authentication_and_health_routes():
    async def scenario():
        settings = make_settings()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        provider = RedisKeycloakOAuthProvider(settings, redis=redis)
        app = build_server(settings, provider).streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=settings.oauth_issuer_url) as client:
            unauthorized = await client.post("/mcp", json={})
            protected = await client.get("/.well-known/oauth-protected-resource/mcp")
            oauth = await client.get("/.well-known/oauth-authorization-server")
            health = await client.get("/health")
            home = await client.get("/")
            missing = await client.get("/does-not-exist")

        assert unauthorized.status_code == 401
        assert "resource_metadata=" in unauthorized.headers["www-authenticate"]
        assert protected.json()["authorization_servers"] == [f"{settings.oauth_issuer_url}/"]
        assert oauth.json()["authorization_endpoint"] == f"{settings.oauth_issuer_url}/authorize"
        assert oauth.json()["registration_endpoint"] == f"{settings.oauth_issuer_url}/register"
        assert health.json() == {"status": "ok", "redis": "up"}
        assert home.status_code == 200
        assert settings.resource_url in home.text
        assert "Cloud operations, in your agent." in home.text
        assert missing.status_code == 404
        assert "Lost in the cloud?" in missing.text
        assert 'href="/"' in missing.text
        await redis.aclose()

    asyncio.run(scenario())
