"""Optional host-health checks that give ``GET /status`` something to say.

Without this module the status envelope is a liveness ping: it answers
``ready`` whenever the process is up, whatever shape the machine is in. That
is fine for a device PC whose real signals come from the services in front of
it, and useless for a data server where the interesting failures are a full
pool, a missed backup or a wedged container.

Every check is **opt-in per host** through ``[checks]`` in ``config.toml``: a
host that configures none of them behaves exactly as before. Each check owns
its own failure — a missing binary, a permission error or a timeout marks that
one component ``unknown`` and never breaks the envelope (STATUS_SPEC §2.1:
``unknown`` means "cannot tell", which is not the same as healthy). Results are
cached for ``ttl_seconds`` so a 30 s poll never runs ``zpool``/``nvidia-smi``
more than it must, and the whole set is bounded so a stuck command cannot eat
the client's poll timeout.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A component that could not be determined. Never "ok": see the module note.
UNKNOWN = "unknown"

# Worst-wins ordering for severities, lowest first.
_SEVERITY_ORDER = ["ok", "warn", UNKNOWN, "crit"]


@dataclass(frozen=True)
class ChecksConfig:
    """``[checks]`` — every field defaults to "do not run this check"."""

    disks: tuple[str, ...] = ()
    disk_warn_pct: float = 90.0
    disk_crit_pct: float = 95.0
    # Percentages are the wrong instrument for a small filesystem: gaia's 9 GB
    # /var sat at 69% — nowhere near a 90% warn — with 2.5 GB free, which one
    # container image can swallow. An absolute floor catches what the ratio
    # cannot, and on a large disk it simply never fires first.
    disk_min_free_warn_gb: float = 2.0
    disk_min_free_crit_gb: float = 1.0
    zfs_pool: str | None = None
    zfs_warn_pct: float = 80.0
    zfs_crit_pct: float = 90.0
    backup_log: str | None = None
    backup_max_age_hours: float = 26.0
    # Substrings that mark a backup as incomplete even when the last line says
    # OK (gaia: a `dot6:` line saying `postgres: not published`).
    backup_warn_patterns: tuple[str, ...] = ()
    user_units: tuple[str, ...] = ()
    # systemctl --user needs a session bus; a *system* service has none unless
    # it is told where to look. Empty disables the user-unit check entirely.
    user_runtime_dir: str | None = None
    docker_containers: tuple[str, ...] = ()
    gpu: bool = False
    ttl_seconds: float = 20.0
    timeout_seconds: float = 3.0

    @property
    def any_enabled(self) -> bool:
        return bool(
            self.disks
            or self.zfs_pool
            or self.backup_log
            or self.user_units
            or self.docker_containers
            or self.gpu
        )

    @classmethod
    def from_toml(cls, section: dict[str, Any]) -> "ChecksConfig":
        def _floats(key: str, default: float) -> float:
            return float(section.get(key, default))

        return cls(
            disks=tuple(str(x) for x in section.get("disks", [])),
            disk_warn_pct=_floats("disk_warn_pct", 90.0),
            disk_crit_pct=_floats("disk_crit_pct", 95.0),
            disk_min_free_warn_gb=_floats("disk_min_free_warn_gb", 2.0),
            disk_min_free_crit_gb=_floats("disk_min_free_crit_gb", 1.0),
            zfs_pool=str(section["zfs_pool"]) if section.get("zfs_pool") else None,
            zfs_warn_pct=_floats("zfs_warn_pct", 80.0),
            zfs_crit_pct=_floats("zfs_crit_pct", 90.0),
            backup_log=str(section["backup_log"]) if section.get("backup_log") else None,
            backup_max_age_hours=_floats("backup_max_age_hours", 26.0),
            backup_warn_patterns=tuple(str(x) for x in section.get("backup_warn_patterns", [])),
            user_units=tuple(str(x) for x in section.get("user_units", [])),
            user_runtime_dir=(
                str(section["user_runtime_dir"]) if section.get("user_runtime_dir") else None
            ),
            docker_containers=tuple(str(x) for x in section.get("docker_containers", [])),
            gpu=bool(section.get("gpu", False)),
            ttl_seconds=_floats("ttl_seconds", 20.0),
            timeout_seconds=_floats("timeout_seconds", 3.0),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        for mount in self.disks:
            if not mount.startswith("/"):
                errors.append(f"checks.disks entries must be absolute paths: {mount!r}")
        if self.backup_log and not str(self.backup_log).startswith("/"):
            errors.append(f"checks.backup_log must be an absolute path: {self.backup_log!r}")
        if self.user_units and not self.user_runtime_dir:
            errors.append(
                "checks.user_units needs checks.user_runtime_dir (e.g. /run/user/1000): "
                "a system service has no session bus to reach `systemctl --user`"
            )
        if self.disk_min_free_crit_gb > self.disk_min_free_warn_gb:
            errors.append(
                "checks.disk_min_free_crit_gb must be <= checks.disk_min_free_warn_gb"
            )
        if self.ttl_seconds < 0 or self.timeout_seconds <= 0:
            errors.append("checks.ttl_seconds must be >= 0 and checks.timeout_seconds > 0")
        return errors


@dataclass
class Finding:
    """One component plus the metrics it produced."""

    key: str
    connected: bool | None
    state: str
    message: str | None = None
    severity: str = "ok"  # ok | warn | crit | unknown
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    def component(self) -> dict[str, Any]:
        out: dict[str, Any] = {"connected": self.connected, "state": self.state}
        if self.message:
            out["message"] = self.message
        return out


async def _run(*argv: str, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _metric(value: float, unit: str) -> dict[str, Any]:
    return {"value": round(float(value), 2), "unit": unit}


class Checks:
    """Runs the configured checks, with a TTL cache in front of them."""

    def __init__(self, cfg: ChecksConfig):
        self.cfg = cfg
        self._cached: tuple[list[Finding], float] | None = None
        self._lock = asyncio.Lock()

    async def findings(self) -> list[Finding]:
        if not self.cfg.any_enabled:
            return []
        now = time.monotonic()
        cached = self._cached
        if cached and now - cached[1] < self.cfg.ttl_seconds:
            return cached[0]
        async with self._lock:
            cached = self._cached  # another caller may have refreshed while we waited
            if cached and time.monotonic() - cached[1] < self.cfg.ttl_seconds:
                return cached[0]
            found = await self._collect()
            self._cached = (found, time.monotonic())
            return found

    async def _collect(self) -> list[Finding]:
        jobs: list[asyncio.Future] = []
        for mount in self.cfg.disks:
            jobs.append(self._guard(f"disk{mount.replace('/', '_').rstrip('_') or '_root'}", self._disk(mount)))
        if self.cfg.zfs_pool:
            jobs.append(self._guard(f"zfs_{self.cfg.zfs_pool}", self._zfs(self.cfg.zfs_pool)))
        if self.cfg.backup_log:
            jobs.append(self._guard("backup", self._backup(self.cfg.backup_log)))
        for unit in self.cfg.user_units:
            jobs.append(self._guard(f"user_unit_{unit}", self._user_unit(unit)))
        for name in self.cfg.docker_containers:
            jobs.append(self._guard(f"container_{name}", self._container(name)))
        if self.cfg.gpu:
            jobs.append(self._guard("gpu", self._gpu()))
        return list(await asyncio.gather(*jobs))

    async def _guard(self, key: str, coro) -> Finding:
        """One check's failure is that check's finding, never the caller's."""
        try:
            return await coro
        except asyncio.TimeoutError:
            return Finding(key, None, UNKNOWN, f"check timed out after {self.cfg.timeout_seconds}s",
                           severity=UNKNOWN)
        except FileNotFoundError as exc:
            return Finding(key, None, UNKNOWN, f"command not available: {exc.filename}",
                           severity=UNKNOWN)
        except PermissionError as exc:
            return Finding(key, None, UNKNOWN, f"permission denied: {exc}", severity=UNKNOWN)
        except Exception as exc:  # a check bug must not take the envelope down
            return Finding(key, None, UNKNOWN, f"{type(exc).__name__}: {exc}", severity=UNKNOWN)

    # ------------------------------------------------------------- checks

    async def _disk(self, mount: str) -> Finding:
        usage = shutil.disk_usage(mount)
        used_pct = 100.0 * usage.used / usage.total if usage.total else 0.0
        free_gb = usage.free / 1e9
        key = f"disk{mount.replace('/', '_').rstrip('_') or '_root'}"

        # Two independent judgements — a ratio and an absolute floor — and the
        # worse one wins. Either alone has a blind spot: the ratio misses a
        # nearly-full small partition, the floor misses a 4 TB disk at 92%.
        by_ratio = (
            "crit" if used_pct >= self.cfg.disk_crit_pct
            else "warn" if used_pct >= self.cfg.disk_warn_pct
            else "ok"
        )
        by_free = (
            "crit" if free_gb <= self.cfg.disk_min_free_crit_gb
            else "warn" if free_gb <= self.cfg.disk_min_free_warn_gb
            else "ok"
        )
        severity = max(by_ratio, by_free, key=_SEVERITY_ORDER.index)

        if severity == "ok":
            message = None
        elif by_free != "ok" and by_free >= by_ratio:
            message = f"{mount} has only {free_gb:.1f} GB free ({used_pct:.0f}% used)"
        else:
            message = f"{mount} is {used_pct:.0f}% full ({free_gb:.1f} GB free)"

        return Finding(
            key,
            severity == "ok",
            f"{used_pct:.0f}% used, {free_gb:.1f} GB free",
            message,
            severity,
            {
                f"{key}_used_pct": _metric(used_pct, "%"),
                f"{key}_free": _metric(free_gb, "GB"),
            },
        )

    async def _zfs(self, pool: str) -> Finding:
        rc, out = await _run("zpool", "status", "-x", pool, timeout=self.cfg.timeout_seconds)
        healthy = rc == 0 and "is healthy" in out
        capacity: float | None = None
        rc2, out2 = await _run(
            "zpool", "list", "-Hp", "-o", "capacity", pool, timeout=self.cfg.timeout_seconds
        )
        if rc2 == 0:
            digits = re.sub(r"[^0-9.]", "", out2.splitlines()[0] if out2 else "")
            capacity = float(digits) if digits else None
        metrics = {f"zfs_{pool}_capacity_pct": _metric(capacity, "%")} if capacity is not None else {}
        if not healthy:
            # `zpool status -x` prints the fault text; keep the first line only.
            first = out.splitlines()[0] if out else "pool state unavailable"
            return Finding(f"zfs_{pool}", False, "not healthy", first[:200], "crit", metrics)
        severity = (
            "crit" if capacity is not None and capacity >= self.cfg.zfs_crit_pct
            else "warn" if capacity is not None and capacity >= self.cfg.zfs_warn_pct
            else "ok"
        )
        state = "healthy" if capacity is None else f"healthy, {capacity:.0f}% full"
        message = None if severity == "ok" else f"pool {pool} is {capacity:.0f}% full"
        return Finding(f"zfs_{pool}", True, state, message, severity, metrics)

    async def _backup(self, log_path: str) -> Finding:
        path = Path(log_path)
        if not path.is_file():
            return Finding("backup", None, UNKNOWN, f"no backup log at {log_path}", UNKNOWN)
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        metrics = {"backup_age_hours": _metric(age_h, "h")}

        failed = "backup FAILED" in last
        ok = "backup OK" in last
        # Warn patterns are looked for in the most recent run only: the tail
        # after the last "backup OK"/"backup FAILED" boundary is this run's body.
        recent = lines[-40:]
        hits = [p for p in self.cfg.backup_warn_patterns if any(p in ln for ln in recent)]

        if failed:
            return Finding("backup", False, "last run FAILED", last[:200], "crit", metrics)
        if not ok:
            return Finding("backup", None, UNKNOWN,
                           f"last log line is not a result line: {last[:120]!r}", UNKNOWN, metrics)
        if age_h > self.cfg.backup_max_age_hours:
            return Finding("backup", False, f"stale, {age_h:.0f}h old",
                           f"last successful backup was {age_h:.0f}h ago "
                           f"(expected within {self.cfg.backup_max_age_hours:.0f}h)",
                           "crit", metrics)
        if hits:
            return Finding("backup", True, f"OK with gaps, {age_h:.0f}h ago",
                           "incomplete backup: " + "; ".join(hits), "warn", metrics)
        return Finding("backup", True, f"OK, {age_h:.0f}h ago", None, "ok", metrics)

    async def _user_unit(self, unit: str) -> Finding:
        env = {"XDG_RUNTIME_DIR": self.cfg.user_runtime_dir or ""}
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
        rc, out = await _run(
            "systemctl", "--user", "show", unit, "-p", "ActiveState", "--value",
            timeout=self.cfg.timeout_seconds, env=env,
        )
        state = out.strip().splitlines()[-1] if out.strip() else ""
        key = f"user_unit_{unit}"
        if rc != 0 or not state:
            return Finding(key, None, UNKNOWN, f"systemctl --user said: {out[:120] or 'nothing'}",
                           UNKNOWN)
        active = state == "active"
        return Finding(key, active, state, None if active else f"{unit} is {state}",
                       "ok" if active else "crit")

    async def _container(self, name: str) -> Finding:
        fmt = "{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
        rc, out = await _run("docker", "inspect", "-f", fmt, name, timeout=self.cfg.timeout_seconds)
        key = f"container_{name}"
        if rc != 0:
            # Covers both "no such container" and a socket we may not read.
            return Finding(key, None, UNKNOWN, f"docker inspect failed: {out[:120]}", UNKNOWN)
        parts = out.split()
        running = parts[0].lower() == "true" if parts else False
        health = parts[1] if len(parts) > 1 else "none"
        if not running:
            return Finding(key, False, "not running", f"container {name} is not running", "crit")
        if health in ("unhealthy", "starting"):
            severity = "crit" if health == "unhealthy" else "warn"
            return Finding(key, health != "unhealthy", f"running ({health})",
                           f"container {name} health is {health}", severity)
        return Finding(key, True, "running", None, "ok")

    async def _gpu(self) -> Finding:
        rc, out = await _run(
            "nvidia-smi", "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits", timeout=self.cfg.timeout_seconds,
        )
        if rc != 0 or not out:
            return Finding("gpu", None, UNKNOWN, f"nvidia-smi failed: {out[:120]}", UNKNOWN)
        used_s, _, total_s = out.splitlines()[0].partition(",")
        used, total = float(used_s.strip()), float(total_s.strip())
        return Finding(
            "gpu", True, f"{used / 1024:.1f} of {total / 1024:.1f} GiB used", None, "ok",
            {
                "gpu_memory_used": _metric(used / 1024, "GiB"),
                "gpu_memory_total": _metric(total / 1024, "GiB"),
            },
        )


def rollup(findings: list[Finding]) -> tuple[str, str | None]:
    """Fold findings into (equipment_status, message).

    ``unknown`` never improves the picture and never fakes a fault: it leaves
    the host ``degraded`` with the reason named, because "cannot tell" is a
    state an operator should see rather than a green tile.
    """
    if not findings:
        return "ready", None
    crit = [f for f in findings if f.severity == "crit"]
    warn = [f for f in findings if f.severity == "warn"]
    unknown = [f for f in findings if f.severity == UNKNOWN]
    if crit:
        status = "error"
    elif warn or unknown:
        status = "degraded"
    else:
        status = "ready"
    problems = crit + warn
    if problems:
        message = "; ".join(f.message or f"{f.key}: {f.state}" for f in problems)
    elif unknown:
        message = "cannot check: " + ", ".join(f.key for f in unknown)
    else:
        message = " · ".join(f"{f.key} {f.state}" for f in findings)
    return status, message[:400]
