"""MCP server wiring: FastMCP tools over stdio or streamable-http.

Transport model:

- **stdio** (default) — spawned per-connection by the client, e.g. Hermes on
  the same machine, or over SSH for a daemonless "lite" deployment on a Pi.
- **http** — a long-lived service (NSSM / systemd). A non-loopback bind
  REQUIRES ``HOSTOPS_TOKEN``; the server refuses to start without it. The
  unauthenticated exception is ``GET /status`` (read-only STATUS_SPEC
  envelope, no secrets) so the dashboard can poll the instance like any
  other service tile.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from .backends import detect_backend
from .config import HostopsConfig, load_config
from .core import HostOps, NotWhitelisted

logger = logging.getLogger("lab_hostops")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _make_server(name: str):
    """MCP SDK compat: 1.x ships FastMCP, 2.x renamed it MCPServer.
    Both keep the same .tool() / .run() / .streamable_http_app() surface."""
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP(name)
    except ModuleNotFoundError:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x

        return MCPServer(name)


def build_server(cfg: HostopsConfig, ops: HostOps):
    mcp = _make_server("lab-hostops")

    async def _guard(coro) -> str:
        try:
            return _dumps(await coro)
        except NotWhitelisted as exc:
            return _dumps({"error": str(exc)})

    @mcp.tool()
    async def host_info() -> str:
        """Host vitals: hostname, OS, uptime, disk free, load average, and
        which service backend (systemd/nssm) this instance drives."""
        return await _guard(ops.host_info())

    @mcp.tool()
    async def service_status(service: str) -> str:
        """State of one whitelisted lab service (systemd unit or NSSM/Windows
        service): active/sub state, enabled, start time, restart count."""
        return await _guard(ops.service_status(service))

    @mcp.tool()
    async def tail_service_log(service: str, lines: int = 80) -> str:
        """Last N lines (10..400) of a whitelisted service's log — journald on
        Linux, the NSSM .out/.err log files on Windows."""
        try:
            return await ops.tail_service_log(service, lines)
        except NotWhitelisted as exc:
            return _dumps({"error": str(exc)})

    @mcp.tool()
    async def restart_service(service: str) -> str:
        """Restart one service from the (smaller) restartable whitelist. On
        Windows this runs `nssm restart` followed by the mandatory
        `sc continue` quirk. The outcome is reported truthfully and audited
        to the dashboard's event-ingest path when configured."""
        return await _guard(ops.restart_service(service))

    @mcp.tool()
    async def list_serial_ports() -> str:
        """Enumerate serial adapters (COM ports / /dev/tty*). Use when a
        device sits in requires_init after a reboot to check whether its
        USB-serial adapter re-enumerated, and on which port."""
        return await _guard(ops.list_serial_ports())

    @mcp.tool()
    async def probe_local_status(port: int) -> str:
        """GET http://127.0.0.1:<port>/status for a whitelisted local port —
        the device service's own STATUS_SPEC envelope, bypassing the network
        path. Read-only; /status is side-effect-free by contract."""
        return await _guard(ops.probe_local_status(port))

    return mcp


def _run_http(cfg: HostopsConfig, ops: HostOps, mcp) -> None:
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    token = os.environ.get("HOSTOPS_TOKEN")
    if not token and not cfg.loopback_only:
        sys.exit(
            "lab-hostops: refusing to bind "
            f"{cfg.host}:{cfg.port} without HOSTOPS_TOKEN set. "
            "Set the token, or bind 127.0.0.1."
        )

    if token:
        # The bearer token is the gate: rebinding attacker JS cannot attach it,
        # and Host headers on a Tailnet bind are unpredictable — so drop the
        # SDK's Host-header check (mcp 2.x only; 1.x lacks the parameter).
        try:
            from mcp.server.transport_security import TransportSecuritySettings

            app = mcp.streamable_http_app(
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
            )
        except (ImportError, TypeError):
            app = mcp.streamable_http_app()
    else:
        # Loopback without a token: keep the SDK's DNS-rebinding protection.
        app = mcp.streamable_http_app()

    async def status_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse(ops.status_envelope())

    app.add_route("/status", status_endpoint, methods=["GET"])

    if token:
        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.url.path == "/status":
                    return await call_next(request)
                if request.headers.get("authorization") != f"Bearer {token}":
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description="lab-hostops MCP server")
    parser.add_argument("--config", default=None, help="Path to config.toml (default: $HOSTOPS_CONFIG or ./config.toml)")
    parser.add_argument("--transport", choices=["stdio", "http"], default=None, help="Override the config's transport")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    cfg = load_config(args.config)
    ops = HostOps(cfg, detect_backend(cfg))
    mcp = build_server(cfg, ops)

    transport = args.transport or cfg.transport
    if transport == "stdio":
        # stdout is reserved for MCP JSON-RPC framing; logging already went to stderr.
        mcp.run()
    else:
        _run_http(cfg, ops, mcp)


if __name__ == "__main__":
    main()
