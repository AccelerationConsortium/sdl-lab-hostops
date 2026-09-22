import asyncio
import time

import pytest

from lab_hostops.checks import Checks, ChecksConfig, Finding, rollup
from lab_hostops.config import HostopsConfig, load_config
from lab_hostops.core import HostOps


class FakeBackend:
    name = "fake"

    async def status(self, service):
        return {"service": service}

    async def tail_log(self, service, lines):
        return ""

    async def restart(self, service):
        return {"ok": True}


# ---------------------------------------------------------------- config


def test_disabled_by_default():
    cfg = ChecksConfig()
    assert not cfg.any_enabled
    assert cfg.validate() == []


def test_user_units_require_a_runtime_dir():
    errors = ChecksConfig(user_units=("la-agente-chat",)).validate()
    assert any("user_runtime_dir" in e for e in errors)
    assert ChecksConfig(user_units=("x",), user_runtime_dir="/run/user/1000").validate() == []


def test_relative_paths_refused():
    assert any("absolute" in e for e in ChecksConfig(disks=("var",)).validate())
    assert any("absolute" in e for e in ChecksConfig(backup_log="backup.log").validate())


def test_load_config_reads_checks_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        """
[hostops]
equipment_id = "hostops_gaia"

[checks]
disks = ["/", "/var"]
zfs_pool = "tank"
backup_log = "/home/sdl2/storage/internal/backups/backup.log"
backup_warn_patterns = ["postgres: not published"]
gpu = true
"""
    )
    cfg = load_config(p)
    assert cfg.checks.disks == ("/", "/var")
    assert cfg.checks.zfs_pool == "tank"
    assert cfg.checks.gpu is True
    assert cfg.checks.any_enabled


# ---------------------------------------------------------------- rollup


def test_rollup_ready_when_all_ok():
    status, message = rollup([Finding("disk_root", True, "42% used")])
    assert status == "ready"
    assert "disk_root" in message


def test_crit_beats_warn():
    status, message = rollup(
        [
            Finding("backup", True, "OK with gaps", "incomplete backup: postgres", "warn"),
            Finding("zfs_tank", False, "not healthy", "pool DEGRADED", "crit"),
        ]
    )
    assert status == "error"
    assert "pool DEGRADED" in message


def test_unknown_degrades_and_never_reads_as_healthy():
    status, message = rollup([Finding("gpu", None, "unknown", "nvidia-smi failed", "unknown")])
    assert status == "degraded"
    assert "cannot check" in message and "gpu" in message


# ---------------------------------------------------------------- checks


def test_disk_check_reports_percent_and_free(tmp_path):
    found = asyncio.run(Checks(ChecksConfig(disks=(str(tmp_path),)))._disk(str(tmp_path)))
    assert found.severity in ("ok", "warn", "crit")
    pct = [m for m in found.metrics if m.endswith("_used_pct")][0]
    assert found.metrics[pct]["unit"] == "%"
    assert 0 <= found.metrics[pct]["value"] <= 100


def test_disk_warns_on_absolute_free_floor_even_when_percent_looks_fine(tmp_path):
    """gaia's /var: 69% used — nowhere near 90% — with 2.5 GB free."""
    checks = Checks(
        ChecksConfig(
            disks=(str(tmp_path),),
            disk_warn_pct=90.0,
            disk_crit_pct=95.0,
            # force the floor to bite whatever the test filesystem looks like
            disk_min_free_warn_gb=10_000.0,
            disk_min_free_crit_gb=0.001,
        )
    )
    found = asyncio.run(checks._disk(str(tmp_path)))
    assert found.severity == "warn"
    assert "GB free" in (found.message or "")
    assert rollup([found])[0] == "degraded"


def test_disk_crit_on_absolute_floor(tmp_path):
    checks = Checks(
        ChecksConfig(
            disks=(str(tmp_path),),
            disk_min_free_warn_gb=20_000.0,
            disk_min_free_crit_gb=10_000.0,
        )
    )
    found = asyncio.run(checks._disk(str(tmp_path)))
    assert found.severity == "crit"
    assert rollup([found])[0] == "error"


def test_disk_floor_defaults_do_not_fire_on_a_roomy_disk(tmp_path):
    found = asyncio.run(Checks(ChecksConfig(disks=(str(tmp_path),)))._disk(str(tmp_path)))
    import shutil as _sh

    free_gb = _sh.disk_usage(str(tmp_path)).free / 1e9
    assert found.severity == ("ok" if free_gb > 2.0 else found.severity)


def test_crit_floor_above_warn_floor_is_refused():
    errors = ChecksConfig(disk_min_free_warn_gb=1.0, disk_min_free_crit_gb=5.0).validate()
    assert any("disk_min_free_crit_gb" in e for e in errors)


def test_disk_warns_past_threshold(tmp_path):
    checks = Checks(ChecksConfig(disks=(str(tmp_path),), disk_warn_pct=0.0, disk_crit_pct=101.0))
    assert asyncio.run(checks._disk(str(tmp_path))).severity == "warn"


def _backup_checks(**kw):
    return Checks(ChecksConfig(backup_log="/unused", backup_warn_patterns=("postgres: not published",), **kw))


def test_backup_ok(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("dot6: ok\nbackup OK\n")
    found = asyncio.run(_backup_checks()._backup(str(log)))
    assert found.severity == "ok" and found.connected is True
    assert found.metrics["backup_age_hours"]["unit"] == "h"


def test_backup_failed_is_critical(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("backup FAILED: bitacora, postgres\n")
    found = asyncio.run(_backup_checks()._backup(str(log)))
    assert found.severity == "crit" and found.connected is False
    assert "FAILED" in (found.message or "")


def test_backup_ok_but_postgres_not_published_warns(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("dot6: postgres: not published\nbackup OK\n")
    found = asyncio.run(_backup_checks()._backup(str(log)))
    assert found.severity == "warn"
    assert "postgres: not published" in (found.message or "")
    # the whole point: a green tile over an incomplete backup would be a lie
    assert rollup([found])[0] == "degraded"


def test_backup_stale_is_critical(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("backup OK\n")
    old = time.time() - 60 * 3600
    import os

    os.utime(log, (old, old))
    found = asyncio.run(_backup_checks(backup_max_age_hours=26.0)._backup(str(log)))
    assert found.severity == "crit" and "60h" in (found.message or "")


def test_missing_backup_log_is_unknown_not_ok(tmp_path):
    found = asyncio.run(_backup_checks()._backup(str(tmp_path / "nope.log")))
    assert found.severity == "unknown" and found.connected is None


def test_guard_converts_a_broken_check_into_unknown():
    async def boom():
        raise RuntimeError("kaboom")

    found = asyncio.run(Checks(ChecksConfig())._guard("thing", boom()))
    assert found.severity == "unknown" and "kaboom" in (found.message or "")


def test_missing_binary_is_unknown():
    checks = Checks(ChecksConfig(zfs_pool="nosuchpool", timeout_seconds=2.0))
    found = asyncio.run(checks._guard("zfs", checks._zfs("nosuchpool")))
    # Either zpool is absent (unknown) or the pool does not exist (crit) —
    # both are honest, neither is "ready".
    assert found.severity in ("unknown", "crit")


def test_findings_are_cached_within_ttl(tmp_path):
    checks = Checks(ChecksConfig(disks=(str(tmp_path),), ttl_seconds=60.0))
    first = asyncio.run(checks.findings())
    second = asyncio.run(checks.findings())
    assert first is second


# ---------------------------------------------------------------- envelope


def _ops(checks: ChecksConfig) -> HostOps:
    return HostOps(HostopsConfig(equipment_id="hostops_test", checks=checks), FakeBackend())


def test_envelope_unchanged_when_no_checks_configured():
    payload = asyncio.run(_ops(ChecksConfig()).status_payload())
    assert payload["equipment_status"] == "ready"
    assert "components" not in payload and "metrics" not in payload


def test_envelope_gains_components_and_metrics(tmp_path):
    payload = asyncio.run(_ops(ChecksConfig(disks=(str(tmp_path),))).status_payload())
    assert payload["protocol_version"] == "1.2"
    assert payload["components"] and payload["metrics"]
    assert payload["equipment_status"] in ("ready", "degraded", "error")


def test_envelope_goes_degraded_on_an_unknown_check(tmp_path):
    payload = asyncio.run(
        _ops(ChecksConfig(backup_log=str(tmp_path / "missing.log"))).status_payload()
    )
    assert payload["equipment_status"] == "degraded"
    assert "backup" in payload["components"]
