from __future__ import annotations

import contextlib
import os
from typing import AsyncIterator

import typer
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Mount

from mcp_email_server.app import mcp
from mcp_email_server.attachments.routes import attachment_route
from mcp_email_server.attachments.store import attachment_store
from mcp_email_server.config import delete_settings

app = typer.Typer()

LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
WILDCARD_IPV4_BIND_HOST = "0.0.0.0"  # noqa: S104
WILDCARD_BIND_HOSTS = {WILDCARD_IPV4_BIND_HOST, "::", ""}
FALSE_VALUES = {"0", "false", "no", "off"}


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_dns_rebinding_protection_enabled() -> bool:
    value = os.environ.get("MCP_ENABLE_DNS_REBINDING_PROTECTION")
    if value is None:
        return True
    return value.strip().lower() not in FALSE_VALUES


def _normalize_host(host: str) -> str:
    if host == "::1":
        return "[::1]"
    return host


def _build_trusted_host_middleware(bind_host: str, port: int) -> Middleware | None:
    """Return TrustedHostMiddleware configured for the given bind address, or None if disabled.

    Trusted host behaviour:
    - Disabled entirely if MCP_ENABLE_DNS_REBINDING_PROTECTION=false or MCP_ALLOWED_HOSTS=*
    - Wildcard bind (0.0.0.0 / :: / "") → no middleware; operator must use MCP_ALLOWED_HOSTS
      to restrict if desired, since the server is intentionally public-facing.
    - Loopback bind → allow only loopback hostnames.
    - Specific host → allow loopback + that hostname.
    - Explicit MCP_ALLOWED_HOSTS → use those (port suffixes stripped).
    """
    if not _is_dns_rebinding_protection_enabled():
        return None

    explicit = _split_csv(os.environ.get("MCP_ALLOWED_HOSTS"))
    if "*" in explicit:
        return None

    if explicit:
        # TrustedHostMiddleware matches on hostname only — strip port suffixes.
        allowed = list({h.split(":")[0] for h in explicit})
        return Middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    normalized = _normalize_host(bind_host)
    if bind_host in WILDCARD_BIND_HOSTS:
        # Wildcard bind is intentionally public; skip host restriction by default.
        return None
    if normalized in set(LOOPBACK_HOSTS):
        return Middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK_HOSTS)
    return Middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK_HOSTS + [normalized])


def _build_fastmcp_transport_security(bind_host: str, port: int) -> TransportSecuritySettings:
    """Build TransportSecuritySettings to inject into FastMCP's own security layer.

    FastMCP's streamable_http_app() runs its own TransportSecurityMiddleware internally.
    We must configure it explicitly — otherwise FastMCP auto-enables loopback-only
    protection whenever the default host (127.0.0.1) is used at init time.
    """
    if not _is_dns_rebinding_protection_enabled():
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    explicit_hosts = _split_csv(os.environ.get("MCP_ALLOWED_HOSTS"))
    explicit_origins = _split_csv(os.environ.get("MCP_ALLOWED_ORIGINS"))

    if "*" in explicit_hosts or "*" in explicit_origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    if explicit_hosts or explicit_origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=explicit_hosts,
            allowed_origins=explicit_origins,
        )

    # No explicit config: mirror the original default behaviour.
    normalized = _normalize_host(bind_host)
    if bind_host in WILDCARD_BIND_HOSTS:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if normalized in set(LOOPBACK_HOSTS):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{h}:*" for h in LOOPBACK_HOSTS],
            allowed_origins=[f"http://{h}:*" for h in LOOPBACK_HOSTS],
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in LOOPBACK_HOSTS] + [normalized, f"{normalized}:{port}", f"{normalized}:*"],
        allowed_origins=[f"http://{h}:*" for h in LOOPBACK_HOSTS]
        + [f"http://{normalized}", f"http://{normalized}:{port}", f"https://{normalized}", f"https://{normalized}:{port}"],
    )


def _build_starlette_app(transport: str, bind_host: str, port: int) -> Starlette:
    """Build the parent Starlette app that mounts FastMCP plus the attachment route."""

    # Configure FastMCP's internal transport security before building the app.
    mcp.settings.transport_security = _build_fastmcp_transport_security(bind_host, port)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        attachment_store.start_cleanup_task()
        async with mcp.session_manager.run():
            yield
        attachment_store.stop_cleanup_task()

    mcp_app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()

    middleware = []
    trusted_host = _build_trusted_host_middleware(bind_host, port)
    if trusted_host is not None:
        middleware.append(trusted_host)

    return Starlette(
        routes=[
            attachment_route,
            Mount("/", app=mcp_app),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )


@app.command()
def stdio():
    mcp.run(transport="stdio")


@app.command()
def sse(
    host: str = os.environ.get("MCP_HOST", "localhost"),
    port: int = int(os.environ.get("MCP_PORT", 9557)),
):
    starlette_app = _build_starlette_app("sse", host, port)
    uvicorn.run(starlette_app, host=host, port=port)


@app.command()
def streamable_http(
    host: str = os.environ.get("MCP_HOST", "localhost"),
    port: int = int(os.environ.get("MCP_PORT", 9557)),
):
    starlette_app = _build_starlette_app("streamable-http", host, port)
    uvicorn.run(starlette_app, host=host, port=port)


@app.command()
def ui():
    from mcp_email_server.ui import main as ui_main

    ui_main()


@app.command()
def reset():
    delete_settings()
    typer.echo("✅ Config reset")


if __name__ == "__main__":
    app(["stdio"])
