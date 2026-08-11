"""Configuration loading and validation.

One TOML file per host (see ``config.example.toml``). The two whitelists are
the entire security model of the tool surface: a service not listed cannot be
inspected at all, and only the ``restartable`` subset can be restarted.
Machine-local configs are gitignored (``*.local.toml``); secrets
(``HOSTOPS_TOKEN``) live in the environment, never in the TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class HostopsConfig:
    equipment_id: str
    transport: str = "stdio"  # "stdio" | "http"
    host: str = "127.0.0.1"
    port: int = 8060
    log_dir: str | None = None  # NSSM backend: directory of <svc>.{out,err}.log
    use_sudo: bool = False  # systemd backend: prefix restart with `sudo -n`
    services: tuple[str, ...] = ()
    restartable: tuple[str, ...] = ()
    probe_ports: tuple[int, ...] = ()

    @property
    def loopback_only(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    def validate(self) -> None:
        errors: list[str] = []
        if not self.equipment_id:
            errors.append("hostops.equipment_id is required")
        if self.transport not in ("stdio", "http"):
            errors.append(f"hostops.transport must be 'stdio' or 'http', got {self.transport!r}")
        if not (0 < self.port < 65536):
            errors.append(f"hostops.port out of range: {self.port}")
        stray = sorted(set(self.restartable) - set(self.services))
        if stray:
            errors.append(f"services.restartable entries missing from services.whitelist: {stray}")
        bad_ports = sorted(p for p in self.probe_ports if not (0 < p < 65536))
        if bad_ports:
            errors.append(f"probe.ports out of range: {bad_ports}")
        if errors:
            raise ValueError("invalid hostops config: " + "; ".join(errors))


def load_config(path: str | Path | None = None) -> HostopsConfig:
    resolved = Path(path or os.environ.get("HOSTOPS_CONFIG", "config.toml"))
    data = tomllib.loads(resolved.read_text(encoding="utf-8"))
    h = data.get("hostops", {})
    s = data.get("services", {})
    p = data.get("probe", {})
    cfg = HostopsConfig(
        equipment_id=str(h.get("equipment_id", "")),
        transport=str(h.get("transport", "stdio")),
        host=str(h.get("host", "127.0.0.1")),
        port=int(h.get("port", 8060)),
        log_dir=str(h["log_dir"]) if "log_dir" in h else None,
        use_sudo=bool(h.get("use_sudo", False)),
        services=tuple(str(x) for x in s.get("whitelist", [])),
        restartable=tuple(str(x) for x in s.get("restartable", [])),
        probe_ports=tuple(int(x) for x in p.get("ports", [])),
    )
    cfg.validate()
    return cfg
