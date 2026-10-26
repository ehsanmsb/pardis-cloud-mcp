"""One upstream validation per HTTP request, with transient failures kept distinct from logout."""
from contextvars import ContextVar
from dataclasses import dataclass, field

from redis.exceptions import RedisError
from starlette.responses import JSONResponse


class SessionUnavailable(Exception):
    """Fixed safe message; never an upstream response or credential."""


@dataclass
class RequestSessions:
    check_upstream: bool
    active: bool = True
    checked: dict[tuple[int, str], str] = field(default_factory=dict)


request_sessions: ContextVar[RequestSessions | None] = ContextVar("request_sessions", default=None)


class SessionGuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = RequestSessions(check_upstream=scope["path"].rstrip("/") == "/mcp")
        context = request_sessions.set(state)
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await self.app(scope, receive, tracked_send)
        except (SessionUnavailable, RedisError):
            if started:
                raise
            await JSONResponse({"error": "temporarily_unavailable",
                                "error_description": "Authentication service is temporarily unavailable. Try again; sign-in is not required yet."},
                               status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "2"})(scope, receive, send)
        finally:
            state.active = False
            request_sessions.reset(context)
