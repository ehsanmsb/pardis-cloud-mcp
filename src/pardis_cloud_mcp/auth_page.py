from __future__ import annotations

from html import escape
from importlib.resources import files
from secrets import token_urlsafe
from string import Template
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse


def is_loopback_callback(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _page(
    *,
    title: str,
    eyebrow: str,
    mark: str,
    heading: str,
    description: str,
    page_content: str = "",
    callback_url: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    nonce = token_urlsafe(16)
    handoff = ""
    csp = (
        "default-src 'none'; "
        f"style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    if callback_url:
        parsed = urlsplit(callback_url)
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        csp += f"; frame-src {parsed.scheme}://{host}{port}"
        handoff = (
            f'<iframe class="agent-handoff" src="{escape(callback_url, quote=True)}" '
            'title="Agent authorization handoff" aria-hidden="true"></iframe>'
        )

    assets = files("pardis_cloud_mcp").joinpath("assets")
    page = Template(assets.joinpath("site.html").read_text(encoding="utf-8"))
    content = page.substitute(
        title=escape(title),
        nonce=nonce,
        logo_svg=assets.joinpath("pardis-mcp-logo.svg").read_text(encoding="utf-8"),
        eyebrow=escape(eyebrow),
        mark_class="number" if mark == "404" else "",
        mark=escape(mark),
        heading=escape(heading),
        description=escape(description),
        page_content=page_content,
        handoff=handoff,
    )
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": csp,
        },
    )


def auth_complete_page(callback_url: str) -> HTMLResponse:
    return _page(
        title="Sign-in complete",
        eyebrow="Identity verified",
        mark="✓",
        heading="Sign-in complete.",
        description="Your agent is receiving the connection. Return to it to continue.",
        callback_url=callback_url,
    )


def home_page(resource_url: str) -> HTMLResponse:
    endpoint = escape(resource_url)
    return _page(
        title="Home",
        eyebrow="Pardis Cloud MCP",
        mark="↗",
        heading="Cloud operations, in your agent.",
        description="Connect an MCP-compatible agent to the endpoint below. Sign-in starts in your agent and continues through Keycloak.",
        page_content=(
            f'<div class="endpoint"><span>MCP ENDPOINT</span><code>{endpoint}</code></div>'
            '<a href="/health">Check service status ↗</a>'
        ),
    )


def not_found_page() -> HTMLResponse:
    return _page(
        title="Page not found",
        eyebrow="Error 404",
        mark="404",
        heading="Lost in the cloud?",
        description="That page does not exist. Head back to the Pardis Cloud MCP homepage.",
        page_content='<a href="/">Back to homepage ↗</a>',
        status_code=404,
    )
