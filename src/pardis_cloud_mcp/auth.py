from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from jwt import PyJWKClient
from pydantic import BaseModel
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)


class PendingLogin(BaseModel):
    client_id: str
    params: AuthorizationParams
    keycloak_code_verifier: str
    nonce: str


class UserSession(BaseModel):
    session_id: str
    subject: str
    claims: dict[str, Any]
    keycloak_tokens: dict[str, Any]


class StoredAuthorizationCode(BaseModel):
    value: AuthorizationCode
    session_id: str


class StoredAccessToken(BaseModel):
    value: AccessToken
    session_id: str


class StoredRefreshToken(BaseModel):
    value: RefreshToken
    session_id: str


class RedisKeycloakOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, settings: Any, redis: Redis | None = None) -> None:
        self.settings = settings
        self.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        self._cipher = Fernet(settings.token_encryption_key.encode())
        self._jwks = PyJWKClient(settings.jwks_url, cache_keys=True)

    def _key(self, kind: str, value: str) -> str:
        return f"{self.settings.redis_prefix}:{kind}:{value}"

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    async def _set_model(self, key: str, value: BaseModel, ttl: int | None = None) -> None:
        encrypted = self._cipher.encrypt(value.model_dump_json().encode()).decode()
        await self.redis.set(key, encrypted, ex=ttl)

    async def _get_model(self, key: str, model: type[BaseModel]) -> BaseModel | None:
        encrypted = await self.redis.get(key)
        if not encrypted:
            return None
        try:
            payload = self._cipher.decrypt(encrypted.encode())
        except InvalidToken:
            logger.warning("Redis OAuth value could not be decrypted", extra={"key_type": key.split(":")[-2]})
            return None
        return model.model_validate_json(payload)

    async def _pop_model(self, key: str, model: type[BaseModel]) -> BaseModel | None:
        encrypted = await self.redis.getdel(key)
        if not encrypted:
            return None
        try:
            payload = self._cipher.decrypt(encrypted.encode())
        except InvalidToken:
            return None
        return model.model_validate_json(payload)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        result = await self._get_model(self._key("client", client_id), OAuthClientInformationFull)
        return result if isinstance(result, OAuthClientInformationFull) else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "At least one redirect URI is required")
        for redirect_uri in client_info.redirect_uris:
            if redirect_uri.scheme != "https" and redirect_uri.host not in {"127.0.0.1", "localhost", "[::1]"}:
                raise RegistrationError("invalid_redirect_uri", "Redirect URIs must use HTTPS or a loopback host")
        await self._set_model(self._key("client", client_info.client_id), client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        resource = params.resource or self.settings.resource_url
        if resource != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "Unknown MCP resource")

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        nonce = secrets.token_urlsafe(32)
        pending = PendingLogin(
            client_id=client.client_id,
            params=params.model_copy(update={"resource": resource}),
            keycloak_code_verifier=verifier,
            nonce=nonce,
        )
        await self._set_model(self._key("login", state), pending, self.settings.login_ttl_seconds)

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.keycloak_client_id,
                "redirect_uri": self.settings.keycloak_redirect_uri,
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.settings.authorization_endpoint}?{query}"

    async def _exchange_keycloak_code(self, code: str, verifier: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self.settings.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.keycloak_redirect_uri,
                    "code_verifier": verifier,
                },
                auth=(self.settings.keycloak_client_id, self.settings.keycloak_client_secret),
            )
        response.raise_for_status()
        return response.json()

    def _decode_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.settings.keycloak_client_id,
            issuer=self.settings.keycloak_issuer_url,
            options={"require": ["exp", "iss", "sub", "aud", "nonce"]},
        )
        if not secrets.compare_digest(str(claims["nonce"]), nonce):
            raise jwt.InvalidTokenError("OIDC nonce mismatch")
        return claims

    async def handle_keycloak_callback(self, request: Request) -> Response:
        state = request.query_params.get("state")
        if not state:
            return JSONResponse({"error": "missing_state"}, status_code=400)

        pending = await self._pop_model(self._key("login", state), PendingLogin)
        if not isinstance(pending, PendingLogin):
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)

        if request.query_params.get("iss") not in (None, self.settings.keycloak_issuer_url):
            return JSONResponse({"error": "issuer_mismatch"}, status_code=400)

        if error := request.query_params.get("error"):
            return RedirectResponse(
                construct_redirect_uri(str(pending.params.redirect_uri), error=error, state=pending.params.state),
                status_code=302,
            )

        code = request.query_params.get("code")
        if not code:
            return JSONResponse({"error": "missing_code"}, status_code=400)

        try:
            keycloak_tokens = await self._exchange_keycloak_code(code, pending.keycloak_code_verifier)
            claims = await asyncio.to_thread(
                self._decode_id_token,
                str(keycloak_tokens["id_token"]),
                pending.nonce,
            )
        except (httpx.HTTPError, KeyError, ValueError, jwt.PyJWTError):
            logger.exception("Keycloak authorization-code exchange failed")
            return RedirectResponse(
                construct_redirect_uri(
                    str(pending.params.redirect_uri), error="access_denied", state=pending.params.state
                ),
                status_code=302,
            )

        session_id = secrets.token_urlsafe(32)
        session = UserSession(
            session_id=session_id,
            subject=str(claims["sub"]),
            claims=claims,
            keycloak_tokens=keycloak_tokens,
        )
        await self._set_model(
            self._key("session", session_id), session, self.settings.refresh_token_ttl_seconds
        )

        outer_code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            client_id=pending.client_id,
            scopes=pending.params.scopes or [self.settings.required_scope],
            expires_at=time.time() + self.settings.authorization_code_ttl_seconds,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=session.subject,
        )
        await self._set_model(
            self._key("code", self._digest(outer_code.code)),
            StoredAuthorizationCode(value=outer_code, session_id=session_id),
            self.settings.authorization_code_ttl_seconds,
        )
        return RedirectResponse(
            construct_redirect_uri(
                str(pending.params.redirect_uri), code=outer_code.code, state=pending.params.state
            ),
            status_code=302,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = await self._get_model(
            self._key("code", self._digest(authorization_code)), StoredAuthorizationCode
        )
        if not isinstance(stored, StoredAuthorizationCode) or stored.value.client_id != client.client_id:
            return None
        return stored.value

    async def _save_token(self, key: str, value: BaseModel, session_id: str, ttl: int) -> None:
        await self._set_model(key, value, ttl)
        index = self._key("session_tokens", session_id)
        await self.redis.sadd(index, key)
        await self.redis.expire(index, self.settings.refresh_token_ttl_seconds)

    async def _mint_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        session: UserSession,
    ) -> OAuthToken:
        now = int(time.time())
        access_value = secrets.token_urlsafe(32)
        refresh_value = secrets.token_urlsafe(48)
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + self.settings.access_token_ttl_seconds,
            resource=resource,
            subject=session.subject,
            claims=session.claims,
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + self.settings.refresh_token_ttl_seconds,
            resource=resource,
            subject=session.subject,
        )
        await self._save_token(
            self._key("access", self._digest(access_value)),
            StoredAccessToken(value=access, session_id=session.session_id),
            session.session_id,
            self.settings.access_token_ttl_seconds,
        )
        await self._save_token(
            self._key("refresh", self._digest(refresh_value)),
            StoredRefreshToken(value=refresh, session_id=session.session_id),
            session.session_id,
            self.settings.refresh_token_ttl_seconds,
        )
        return OAuthToken(
            access_token=access_value,
            refresh_token=refresh_value,
            token_type="Bearer",
            expires_in=self.settings.access_token_ttl_seconds,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = await self._pop_model(
            self._key("code", self._digest(authorization_code.code)), StoredAuthorizationCode
        )
        if not isinstance(stored, StoredAuthorizationCode):
            raise TokenError("invalid_grant", "Authorization code was already used or expired")
        session = await self._get_model(self._key("session", stored.session_id), UserSession)
        if not isinstance(session, UserSession):
            raise TokenError("invalid_grant", "User session expired")
        return await self._mint_tokens(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource or self.settings.resource_url,
            session=session,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = await self._get_model(self._key("access", self._digest(token)), StoredAccessToken)
        if not isinstance(stored, StoredAccessToken):
            return None
        if stored.value.expires_at and stored.value.expires_at < int(time.time()):
            return None
        return stored.value

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await self._get_model(
            self._key("refresh", self._digest(refresh_token)), StoredRefreshToken
        )
        if not isinstance(stored, StoredRefreshToken) or stored.value.client_id != client.client_id:
            return None
        return stored.value

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        stored = await self._pop_model(
            self._key("refresh", self._digest(refresh_token.token)), StoredRefreshToken
        )
        if not isinstance(stored, StoredRefreshToken):
            raise TokenError("invalid_grant", "Refresh token was already used or expired")
        session = await self._get_model(self._key("session", stored.session_id), UserSession)
        if not isinstance(session, UserSession):
            raise TokenError("invalid_grant", "User session expired")
        return await self._mint_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=refresh_token.resource or self.settings.resource_url,
            session=session,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        model = StoredAccessToken if kind == "access" else StoredRefreshToken
        stored = await self._get_model(self._key(kind, self._digest(token.token)), model)
        if not isinstance(stored, (StoredAccessToken, StoredRefreshToken)):
            return
        index = self._key("session_tokens", stored.session_id)
        keys = await self.redis.smembers(index)
        if keys:
            await self.redis.delete(*keys)
        await self.redis.delete(index, self._key("session", stored.session_id))
