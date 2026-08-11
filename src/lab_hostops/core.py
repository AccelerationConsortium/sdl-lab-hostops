"""Tool logic behind the MCP surface, backend-agnostic and directly testable.

Every operation gate lives here — whitelist membership, restartable subset,
probe-port allowlist, line clamps — so the MCP layer in ``server.py`` is a
thin serialization wrapper and the gates are unit-tested without a server.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .audit import post_audit
from .config import HostopsConfig

MIN_LOG_LINES = 10
MAX_LOG_LINES = 400
PROBE_TIMEOUT_S = 3.0
PROBE_BODY_MAX_CHARS = 4000

_STARTED_AT = time.monotonic()


class NotWhitelisted(ValueError):
    """The request names a service or port outside the configured whitelist."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _default_status_getter(port: int, path: str = "/status") -> dict[str, Any]:
    import httpx

    url = f"http://127.0.0.1:{port}{path}"
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
        resp = await client.get(url)
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    body: Any
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:PROBE_BODY_MAX_CHARS]
    if isinstance(body, str) and len(body) > PROBE_BODY_MAX_CHARS:
        body = body[:PROBE_BODY_MAX_CHARS]
    return {"url": url, "status_code": resp.status_code, "latency_ms": latency_ms, "body": body}


class HostOps:
    def __init__(
        self,
        cfg: HostopsConfig,
        backend,
        status_getter: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
    ):
        self.cfg = cfg
        self.backend = backend
        self._status_getter = status_getter or _default_status_getter

    # ------------------------------------------------------------------ gates

    def _check_service(self, service: str) -> str:
        if service not in self.cfg.services:
            raise NotWhitelisted(
                f"service {service!r} is not whitelisted; allowed: {sorted(self.cfg.services)}"
            )
        return service

    def _check_restartable(self, service: str) -> str:
        self._check_service(service)
        if service not in self.cfg.restartable:
            raise NotWhitelisted(
                f"service {service!r} is not restartable on this host; "
                f"restartable: {sorted(self.cfg.restartable)}"
            )
        return service

    def _check_port(self, port: int) -> int:
        if port not in self.cfg.probe_ports:
            raise NotWhitelisted(
                f"port {port} is not in the probe whitelist; allowed: {sorted(self.cfg.probe_ports)}"
            )
        return port

    # ------------------------------------------------------------------ tools

    async def host_info(self) -> dict[str, Any]:
        anchor = "C:\\" if platform.system() == "Windows" else "/"
        du = shutil.disk_usage(anchor)
        info: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "backend": self.backend.name,
            "device_time": _utcnow(),
            "disk": {"anchor": anchor, "free_gb": round(du.free / 1e9, 2), "total_gb": round(du.total / 1e9, 2)},
            "uptime_s": None,
            "loadavg": None,
        }
        try:
            info["uptime_s"] = round(float(open("/proc/uptime").read().split()[0]))
        except OSError:
            pass
        if hasattr(os, "getloadavg"):
            try:
                info["loadavg"] = [round(x, 2) for x in os.getloadavg()]
            except OSError:
                pass
        return info

    async def service_status(self, service: str) -> dict[str, Any]:
        return await self.backend.status(self._check_service(service))

    async def tail_service_log(self, service: str, lines: int = 80) -> str:
        clamped = max(MIN_LOG_LINES, min(MAX_LOG_LINES, int(lines)))
        return await self.backend.tail_log(self._check_service(service), clamped)

    async def restart_service(self, service: str) -> dict[str, Any]:
        result = await self.backend.restart(self._check_restartable(service))
        result["audited"] = await post_audit(
            self.cfg.equipment_id,
            event="hostops_action",
            message=f"restart_service {service}: {'ok' if result.get('ok') else 'FAILED'}",
            extra={"action": "restart_service", "service": service, "ok": bool(result.get("ok"))},
        )
        return result

    async def list_serial_ports(self) -> dict[str, Any]:
        try:
            from serial.tools import list_ports  # type: ignore[import-not-found]

            ports = [
                {"device": p.device, "description": p.description, "hwid": p.hwid}
                for p in list_ports.comports()
            ]
            return {"source": "pyserial", "ports": ports}
        except ImportError:
            pass
        if platform.system() == "Windows":
            return {"error": "pyserial not installed; install the [serial] extra for COM enumeration"}
        import glob

        ports = sorted(
            {*glob.glob("/dev/ttyUSB*"), *glob.glob("/dev/ttyACM*"), *glob.glob("/dev/serial/by-id/*")}
        )
        return {"source": "dev-glob", "ports": [{"device": p} for p in ports]}

    async def probe_local_status(self, port: int) -> dict[str, Any]:
        checked = self._check_port(int(port))
        path = (self.cfg.probe_paths or {}).get(checked, "/status")
        try:
            return {"ok": True, **await self._status_getter(checked, path)}
        except Exception as exc:  # transport failure is a *finding*, not a crash
            return {"ok": False, "url": f"http://127.0.0.1:{checked}{path}", "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- status

    def status_envelope(self) -> dict[str, Any]:
        """Minimal STATUS_SPEC-shaped envelope (v1.2 read-only clause) for the
        http transport's unauthenticated GET /status, so the dashboard can
        monitor hostops instances like any other service tile."""
        hostname = socket.gethostname()
        return {
            "protocol_version": "1.2",
            "equipment_id": self.cfg.equipment_id,
            "equipment_name": f"Host Ops ({hostname})",
            "equipment_kind": "other",
            "host": hostname,
            "equipment_status": "ready",
            "activity": "idle",
            "device_time": _utcnow(),
            "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
            "allowed_actions": [],
            "details": {
                "backend": self.backend.name,
                "services_whitelist": sorted(self.cfg.services),
                "restartable": sorted(self.cfg.restartable),
                "probe_ports": sorted(self.cfg.probe_ports),
            },
        }
