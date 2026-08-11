"""Service-manager backends: systemd (Linux / Raspberry Pi) and NSSM (Windows).

Both expose the same three async operations. Callers (``core.HostOps``) have
already enforced the whitelist; backends only translate to the platform's
commands and report results truthfully — a failed restart returns the error
output, it is never swallowed.
"""

from __future__ import annotations

import asyncio
import platform
from collections import deque
from pathlib import Path

from .config import HostopsConfig


class BackendError(RuntimeError):
    """A backend command could not be executed at all (binary missing, timeout)."""


async def _run(*argv: str, timeout: float = 30.0) -> tuple[int, str]:
    """Run a command, return (returncode, combined stdout+stderr text)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise BackendError(f"{argv[0]!r} is not available on this host") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise BackendError(f"{' '.join(argv)!r} timed out after {timeout}s") from exc
    return proc.returncode or 0, out.decode(errors="replace").strip()


def tail_file(path: Path, lines: int) -> str:
    """Last N lines of a text file; explicit marker when it doesn't exist."""
    if not path.exists():
        return f"(no such log file: {path})"
    with path.open(encoding="utf-8", errors="replace") as fh:
        return "".join(deque(fh, maxlen=lines)).rstrip("\n")


def parse_kv_block(text: str) -> dict[str, str]:
    """Parse `Key=Value` lines (systemctl show output)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


class SystemdBackend:
    name = "systemd"

    def __init__(self, use_sudo: bool = False):
        self.use_sudo = use_sudo

    @staticmethod
    def unit(service: str) -> str:
        return service if "." in service else f"{service}.service"

    async def status(self, service: str) -> dict:
        unit = self.unit(service)
        _, out = await _run(
            "systemctl", "show", unit, "--no-pager",
            "--property=ActiveState,SubState,UnitFileState,ExecMainStartTimestamp,NRestarts",
        )
        props = parse_kv_block(out)
        return {
            "backend": self.name,
            "service": unit,
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "enabled": props.get("UnitFileState"),
            "started_at": props.get("ExecMainStartTimestamp") or None,
            "restarts": props.get("NRestarts"),
        }

    async def tail_log(self, service: str, lines: int) -> str:
        unit = self.unit(service)
        rc, out = await _run(
            "journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso",
        )
        if rc != 0:
            return f"(journalctl exited {rc}: {out})"
        return out

    async def restart(self, service: str) -> dict:
        unit = self.unit(service)
        argv = ["systemctl", "restart", unit]
        if self.use_sudo:
            # `-n` = never prompt; requires a sudoers drop-in for exactly this
            # command (see README). An interactive-only sudo fails loudly here
            # instead of hanging.
            argv = ["sudo", "-n", *argv]
        rc, out = await _run(*argv, timeout=90.0)
        return {"ok": rc == 0, "backend": self.name, "service": unit, "output": out or "(no output)"}


class NssmBackend:
    name = "nssm"

    def __init__(self, log_dir: str | None):
        self.log_dir = Path(log_dir) if log_dir else Path("C:/SDL_Logs")

    async def status(self, service: str) -> dict:
        rc, sc_out = await _run("sc", "query", service)
        state = None
        for line in sc_out.splitlines():
            if "STATE" in line:
                state = line.split(":", 1)[-1].strip()
                break
        return {
            "backend": self.name,
            "service": service,
            "state": state if rc == 0 else None,
            "raw": sc_out if rc != 0 else None,
        }

    async def tail_log(self, service: str, lines: int) -> str:
        # NSSM AppStdout/AppStderr convention from DEVICE_PC_SETUP §3.
        parts = []
        for suffix in ("out", "err"):
            path = self.log_dir / f"{service}.{suffix}.log"
            parts.append(f"===== {path.name} =====\n{tail_file(path, lines)}")
        return "\n".join(parts)

    async def restart(self, service: str) -> dict:
        rc, out = await _run("nssm", "restart", service, timeout=90.0)
        # NSSM quirk (DEVICE_PC_SETUP §7): services come back SERVICE_PAUSED;
        # `sc continue` is the resume verb, run unconditionally and best-effort.
        try:
            await _run("sc", "continue", service, timeout=30.0)
        except BackendError:
            pass
        return {"ok": rc == 0, "backend": self.name, "service": service, "output": out or "(no output)"}


def detect_backend(cfg: HostopsConfig):
    if platform.system() == "Windows":
        return NssmBackend(cfg.log_dir)
    return SystemdBackend(use_sudo=cfg.use_sudo)
