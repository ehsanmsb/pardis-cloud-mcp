import asyncio
import time
from types import SimpleNamespace

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from pardis_cloud_mcp.server import KeycloakJWTVerifier, Settings, build_server


def make_verifier_and_token(audience: str = "pardis-mcp"):
    settings = Settings(
        issuer_url="https://keycloak.example.com/realms/snapp",
        audience="pardis-mcp",
        resource_url="http://127.0.0.1:8000/mcp",
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.issuer_url,
            "sub": "user-123",
            "aud": audience,
            "azp": "mcp-client",
            "scope": "openid mcp:tools",
            "iat": now,
            "exp": now + 300,
            "preferred_username": "ehsan",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    verifier = KeycloakJWTVerifier(settings)
    verifier._jwks_client.get_signing_key_from_jwt = lambda _: SimpleNamespace(key=private_key.public_key())
    return verifier, token


def test_accepts_valid_keycloak_access_token():
    verifier, token = make_verifier_and_token()

    result = asyncio.run(verifier.verify_token(token))

    assert result is not None
    assert result.subject == "user-123"
    assert result.client_id == "mcp-client"
    assert result.scopes == ["openid", "mcp:tools"]


def test_rejects_token_for_another_audience():
    verifier, token = make_verifier_and_token("another-service")

    assert asyncio.run(verifier.verify_token(token)) is None


def test_http_requires_auth_and_publishes_discovery():
    settings = Settings(
        issuer_url="https://keycloak.example.com/realms/snapp",
        audience="pardis-mcp",
        resource_url="http://127.0.0.1:8000/mcp",
    )
    app = build_server(settings).streamable_http_app()

    async def request():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            unauthorized = await client.post("/mcp", json={})
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
        return unauthorized, metadata

    unauthorized, metadata = asyncio.run(request())

    assert unauthorized.status_code == 401
    assert "resource_metadata=" in unauthorized.headers["www-authenticate"]
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == settings.resource_url
    assert metadata.json()["authorization_servers"] == [settings.issuer_url]
