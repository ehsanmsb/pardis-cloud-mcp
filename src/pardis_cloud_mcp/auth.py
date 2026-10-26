from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from jwt import PyJWKClient
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import WatchError
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

from pardis_cloud_mcp.auth_page import auth_complete_page, is_loopback_callback
from pardis_cloud_mcp.session_guard import SessionUnavailable, request_sessions

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
    authorization_version: str = ""
    oidc_binding: dict[str, Any] = Field(default_factory=dict)


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
        self._jwks = PyJWKClient(settings.jwks_url, cache_keys=True, timeout=5)

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
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
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

    def _verified_id_token(self, token: str) -> dict[str, Any]:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.settings.keycloak_client_id,
            issuer=self.settings.keycloak_issuer_url,
            options={"require": ["exp", "iss", "sub", "aud", "iat"]},
        )
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if claims.get("azp", self.settings.keycloak_client_id) != self.settings.keycloak_client_id:
            raise jwt.InvalidTokenError("OIDC authorized party mismatch")
        if len(audiences) > 1 and "azp" not in claims:
            raise jwt.InvalidTokenError("OIDC authorized party is required")
        return claims

    def _decode_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        claims = self._verified_id_token(token)
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
            raise jwt.InvalidTokenError("OIDC nonce mismatch")
        return claims

    def _decode_refreshed_id_token(self, token: str, session: UserSession) -> dict[str, Any]:
        claims = self._verified_id_token(token)
        if claims["sub"] != session.subject:
            raise jwt.InvalidTokenError("OIDC subject changed")
        binding = session.oidc_binding or {name: session.claims[name] for name in ("nonce", "auth_time", "sid") if name in session.claims}
        for name in ("nonce", "auth_time", "sid"):
            if name in claims and name in binding and claims[name] != binding[name]:
                raise jwt.InvalidTokenError("OIDC session binding changed")
        if "nonce" in claims and "nonce" not in binding:
            raise jwt.InvalidTokenError("Unexpected OIDC nonce")
        return claims

    def authorization_fingerprint(self, claims: dict[str, Any]) -> str:
        roles = claims.get("pardis_roles")
        roles = [] if roles is None else roles
        if not isinstance(roles, list) or not all(isinstance(role, str) and role for role in roles):
            raise ValueError("Invalid role claim")
        audience = claims.get("aud")
        data = [claims.get("iss"), claims.get("sub"), sorted(audience if isinstance(audience, list) else [audience]),
                claims.get("azp", self.settings.keycloak_client_id), sorted(set(roles))]
        return self._digest(json.dumps(data, separators=(",", ":")))

    @asynccontextmanager
    async def _session_lease(self, session_id: str):
        key = self._key("session_refresh_lock", session_id)
        owner = secrets.token_urlsafe(32)
        deadline = time.monotonic() + 12
        while not await self.redis.set(key, owner, nx=True, ex=30):
            if time.monotonic() >= deadline:
                raise SessionUnavailable("Session refresh is busy")
            await asyncio.sleep(0.05)
        try:
            yield key, owner
        finally:
            # Compare-and-delete: an expired lease must never unlock its successor.
            for _ in range(3):
                async with self.redis.pipeline() as pipe:
                    try:
                        await pipe.watch(key)
                        if await pipe.get(key) != owner:
                            break
                        pipe.multi()
                        pipe.delete(key)
                        await pipe.execute()
                        break
                    except WatchError:
                        continue

    async def _refresh_oidc_tokens(self, session: UserSession) -> tuple[dict, dict] | None:
        refresh = session.keycloak_tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            return None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.post(self.settings.token_endpoint,
                    data={"grant_type": "refresh_token", "refresh_token": refresh},
                    auth=(self.settings.keycloak_client_id, self.settings.keycloak_client_secret))
        except httpx.HTTPError:
            logger.warning("OIDC refresh connection unavailable")
            raise SessionUnavailable("Identity provider connection failed") from None
        if response.status_code == 400:
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            if error in ("invalid_grant", "invalid_token", "login_required"):
                return None
        if response.status_code != 200:
            logger.warning("OIDC refresh unavailable (HTTP %s)", response.status_code)
            raise SessionUnavailable("Identity provider refresh is unavailable")
        try:
            if len(response.content) > 128 * 1024:
                raise ValueError
            tokens = response.json()
            if not isinstance(tokens, dict) or any(not isinstance(tokens.get(key), str) or not tokens[key]
                                                   for key in ("id_token", "access_token")):
                raise ValueError
            tokens.setdefault("refresh_token", refresh)
            if not isinstance(tokens["refresh_token"], str) or not tokens["refresh_token"]:
                raise ValueError
            claims = await asyncio.to_thread(self._decode_refreshed_id_token, tokens["id_token"], session)
            self.authorization_fingerprint(claims)
            return tokens, claims
        except jwt.PyJWKClientConnectionError:
            raise SessionUnavailable("Identity provider signing keys are unavailable") from None
        except (ValueError, KeyError, TypeError, jwt.PyJWTError):
            return None

    async def _current_session(self, session_id: str, *, force: bool = False) -> UserSession | None:
        key = self._key("session", session_id)
        state = request_sessions.get()
        session = await self._get_model(key, UserSession)
        if not isinstance(session, UserSession):
            return None
        audience = session.claims.get("aud")
        if (session.claims.get("iss") != self.settings.keycloak_issuer_url
                or self.settings.keycloak_client_id not in (audience if isinstance(audience, list) else [audience])
                or session.claims.get("sub") != session.subject):
            await self._revoke_session(session_id)
            return None
        try:
            self.authorization_fingerprint(session.claims)
        except ValueError:
            await self._revoke_session(session_id)
            return None
        context_key = (id(self), session_id)
        if not force and state and state.active and (not state.check_upstream
                                                    or state.checked.get(context_key) == session.authorization_version):
            return session
        async with self._session_lease(session_id) as (lock_key, lock_owner):
            raw = await self.redis.get(key)
            if not raw:
                return None
            session = UserSession.model_validate_json(self._cipher.decrypt(raw.encode()))
            try:
                async with asyncio.timeout(12):
                    refreshed = await self._refresh_oidc_tokens(session)
            except TimeoutError:
                raise SessionUnavailable("Identity provider refresh timed out") from None
            if refreshed is None:
                await self._revoke_session(session_id, expected=raw, lease=(lock_key, lock_owner))
                logger.info("Upstream session rejected; local session revoked")
                return None
            tokens, claims = refreshed
            changed = self.authorization_fingerprint(claims) != self.authorization_fingerprint(session.claims)
            updated = session.model_copy(update={"claims": claims, "keycloak_tokens": tokens,
                "oidc_binding": session.oidc_binding or {name: session.claims[name] for name in ("nonce", "auth_time", "sid") if name in session.claims},
                "authorization_version": secrets.token_hex(16) if changed or not session.authorization_version else session.authorization_version})
            index = self._key("session_tokens", session_id)
            for _ in range(3):
                async with self.redis.pipeline() as pipe:
                    try:
                        await pipe.watch(key, lock_key, index)
                        current_raw = await pipe.get(key)
                        if current_raw != raw:
                            if current_raw is None:
                                return None
                            raise SessionUnavailable("Session changed during refresh")
                        if await pipe.get(lock_key) != lock_owner:
                            raise SessionUnavailable("Session refresh lease expired")
                        ttl = await pipe.pttl(key)
                        if ttl <= 0:
                            return None
                        cloud_keys = ([item for item in await pipe.smembers(index)
                                       if item.startswith(self._key("cloud", ""))] if changed or not session.authorization_version else [])
                        pipe.multi()
                        pipe.set(key, self._cipher.encrypt(updated.model_dump_json().encode()).decode(), px=ttl)
                        if cloud_keys:
                            pipe.delete(*cloud_keys)
                            pipe.srem(index, *cloud_keys)
                        await pipe.execute()
                        break
                    except WatchError:
                        continue
            else:
                raise SessionUnavailable("Session refresh could not be saved")
        if state and state.active:
            state.checked[context_key] = updated.authorization_version
        if changed:
            logger.info("Upstream authorization changed; cloud credential generation invalidated")
        return updated

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
            self.authorization_fingerprint(claims)
        except (httpx.HTTPError, KeyError, ValueError, jwt.PyJWTError):
            logger.warning("Identity provider authorization-code exchange failed")
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
            authorization_version=secrets.token_hex(16),
            oidc_binding={name: claims[name] for name in ("nonce", "auth_time", "sid") if name in claims},
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
        callback_url = construct_redirect_uri(
            str(pending.params.redirect_uri), code=outer_code.code, state=pending.params.state
        )
        if is_loopback_callback(callback_url):
            return auth_complete_page(callback_url)
        return RedirectResponse(callback_url, status_code=302)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = await self._get_model(
            self._key("code", self._digest(authorization_code)), StoredAuthorizationCode
        )
        if not isinstance(stored, StoredAuthorizationCode) or stored.value.client_id != client.client_id:
            return None
        return stored.value

    async def _save_token(self, key: str, value: BaseModel, session_id: str, ttl: int,
                          authorization_version: str) -> bool:
        session_key = self._key("session", session_id)
        index = self._key("session_tokens", session_id)
        for _ in range(3):
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(session_key)
                    raw = await pipe.get(session_key)
                    remaining = await pipe.pttl(session_key)
                    if not raw or remaining <= 0:
                        return False
                    session = UserSession.model_validate_json(self._cipher.decrypt(raw.encode()))
                    if session.authorization_version != authorization_version:
                        return False
                    pipe.multi()
                    pipe.set(key, self._cipher.encrypt(value.model_dump_json().encode()).decode(), px=min(ttl * 1000, remaining))
                    pipe.sadd(index, key)
                    pipe.pexpire(index, remaining)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue
        raise SessionUnavailable("Session token could not be saved")

    async def _mint_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        session: UserSession,
        consume_refresh_key: str | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        session_key = self._key("session", session.session_id)
        remaining = await self.redis.ttl(session_key)
        if remaining <= 0:
            raise TokenError("invalid_grant", "User session expired")
        access_ttl = min(self.settings.access_token_ttl_seconds, remaining)
        refresh_ttl = min(self.settings.refresh_token_ttl_seconds, remaining)
        access_value = secrets.token_urlsafe(32)
        refresh_value = secrets.token_urlsafe(48)
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + access_ttl,
            resource=resource,
            subject=session.subject,
            claims=session.claims,
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + refresh_ttl,
            resource=resource,
            subject=session.subject,
        )
        access_key = self._key("access", self._digest(access_value))
        refresh_key = self._key("refresh", self._digest(refresh_value))
        index = self._key("session_tokens", session.session_id)
        for _ in range(3):
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(session_key, *([consume_refresh_key] if consume_refresh_key else []))
                    raw = await pipe.get(session_key)
                    ttl_ms = await pipe.pttl(session_key)
                    if not raw or ttl_ms <= 0:
                        raise TokenError("invalid_grant", "User session expired")
                    current = UserSession.model_validate_json(self._cipher.decrypt(raw.encode()))
                    if current.subject != session.subject or current.authorization_version != session.authorization_version:
                        raise SessionUnavailable("Session access changed during token issuance")
                    if consume_refresh_key and not await pipe.exists(consume_refresh_key):
                        raise TokenError("invalid_grant", "Refresh token was already used or expired")
                    pipe.multi()
                    if consume_refresh_key:
                        pipe.delete(consume_refresh_key)
                        pipe.srem(index, consume_refresh_key)
                    pipe.set(access_key, self._cipher.encrypt(StoredAccessToken(value=access, session_id=session.session_id).model_dump_json().encode()).decode(),
                             px=min(access_ttl * 1000, ttl_ms))
                    pipe.set(refresh_key, self._cipher.encrypt(StoredRefreshToken(value=refresh, session_id=session.session_id).model_dump_json().encode()).decode(),
                             px=min(refresh_ttl * 1000, ttl_ms))
                    pipe.sadd(index, access_key, refresh_key)
                    pipe.pexpire(index, ttl_ms)
                    await pipe.execute()
                    break
                except WatchError:
                    continue
        else:
            raise SessionUnavailable("Session tokens could not be issued")
        return OAuthToken(
            access_token=access_value,
            refresh_token=refresh_value,
            token_type="Bearer",
            expires_in=access_ttl,
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
        if stored.value.expires_at and stored.value.expires_at <= int(time.time()):
            return None
        if stored.value.resource != self.settings.resource_url or self.settings.required_scope not in stored.value.scopes:
            return None
        session = await self._current_session(stored.session_id)
        if not isinstance(session, UserSession) or session.subject != stored.value.subject:
            return None
        if not await self.redis.exists(self._key("access", self._digest(token))):
            return None
        # Access-token records are snapshots; current session claims are authoritative.
        return stored.value.model_copy(update={"claims": session.claims})

    async def session_for_access_token(self, token: str) -> UserSession | None:
        """Resolve only a live, resource-bound MCP token to its own session."""
        access = await self.load_access_token(token)
        if (
            access is None
            or access.resource != self.settings.resource_url
            or self.settings.required_scope not in access.scopes
        ):
            return None
        stored = await self._get_model(self._key("access", self._digest(token)), StoredAccessToken)
        if not isinstance(stored, StoredAccessToken):
            return None
        session = await self._get_model(self._key("session", stored.session_id), UserSession)
        if not isinstance(session, UserSession) or session.subject != access.subject:
            return None
        return session

    async def session_is_current(self, token: str, expected: UserSession) -> bool:
        current = await self.session_for_access_token(token)
        return (current is not None and current.session_id == expected.session_id
                and current.authorization_version == expected.authorization_version)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await self._get_model(
            self._key("refresh", self._digest(refresh_token)), StoredRefreshToken
        )
        if (not isinstance(stored, StoredRefreshToken) or stored.value.client_id != client.client_id
                or stored.value.expires_at is not None and stored.value.expires_at <= time.time()):
            return None
        return stored.value

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        consume_key = self._key("refresh", self._digest(refresh_token.token))
        stored = await self._get_model(
            consume_key, StoredRefreshToken
        )
        if (not isinstance(stored, StoredRefreshToken) or stored.value.client_id != client.client_id
                or stored.value.expires_at is not None and stored.value.expires_at <= time.time()):
            raise TokenError("invalid_grant", "Refresh token was already used or expired")
        if not set(scopes).issubset(stored.value.scopes):
            raise TokenError("invalid_scope", "Requested scopes exceed the original grant")
        session = await self._current_session(stored.session_id, force=True)
        if not isinstance(session, UserSession):
            raise TokenError("invalid_grant", "User session expired")
        return await self._mint_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=stored.value.resource or self.settings.resource_url,
            session=session,
            consume_refresh_key=consume_key,
        )

    async def _revoke_session(self, session_id: str, *, expected: str | None = None,
                              lease: tuple[str, str] | None = None) -> None:
        # Delete the session first: watched token/cache writes cannot recreate a revoked login.
        key = self._key("session", session_id)
        if expected is None:
            await self.redis.delete(key)
        else:
            for _ in range(3):
                async with self.redis.pipeline() as pipe:
                    try:
                        await pipe.watch(key, *([lease[0]] if lease else []))
                        current_raw = await pipe.get(key)
                        if current_raw != expected:
                            if current_raw is None:
                                return
                            raise SessionUnavailable("Session changed during revocation")
                        if lease and await pipe.get(lease[0]) != lease[1]:
                            raise SessionUnavailable("Session refresh lease expired")
                        pipe.multi()
                        pipe.delete(key)
                        await pipe.execute()
                        break
                    except WatchError:
                        continue
            else:
                raise SessionUnavailable("Session revocation could not be saved")
        index = self._key("session_tokens", session_id)
        keys = await self.redis.smembers(index)
        if keys:
            await self.redis.delete(*keys)
        await self.redis.delete(index)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        model = StoredAccessToken if kind == "access" else StoredRefreshToken
        stored = await self._get_model(self._key(kind, self._digest(token.token)), model)
        if not isinstance(stored, (StoredAccessToken, StoredRefreshToken)):
            return
        await self._revoke_session(stored.session_id)
