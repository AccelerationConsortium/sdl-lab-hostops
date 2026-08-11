import pytest

from lab_hostops.config import HostopsConfig
from lab_hostops.core import MAX_LOG_LINES, MIN_LOG_LINES, HostOps, NotWhitelisted


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls = []

    async def status(self, service):
        self.calls.append(("status", service))
        return {"backend": self.name, "service": service, "active_state": "active"}

    async def tail_log(self, service, lines):
        self.calls.append(("tail", service, lines))
        return f"{lines} lines of {service}"

    async def restart(self, service):
        self.calls.append(("restart", service))
        return {"ok": True, "backend": self.name, "service": service, "output": "done"}


def make_ops(**overrides):
    cfg = HostopsConfig(
        equipment_id="hostops_test",
        services=("svc_a", "svc_b"),
        restartable=("svc_a",),
        probe_ports=(8001,),
        **overrides,
    )
    backend = FakeBackend()

    async def fake_getter(port):
        return {"url": f"http://127.0.0.1:{port}/status", "status_code": 200, "latency_ms": 1.0, "body": {}}

    return HostOps(cfg, backend, status_getter=fake_getter), backend


async def test_service_status_whitelist_gate():
    ops, backend = make_ops()
    assert (await ops.service_status("svc_b"))["service"] == "svc_b"
    with pytest.raises(NotWhitelisted):
        await ops.service_status("ac-organic-lab-api")
    assert ("status", "svc_b") in backend.calls


async def test_restart_gate_is_stricter_than_whitelist():
    ops, backend = make_ops()
    result = await ops.restart_service("svc_a")
    assert result["ok"] is True
    assert result["audited"] is False  # HOSTOPS_INGEST_URL unset in tests
    with pytest.raises(NotWhitelisted):
        await ops.restart_service("svc_b")  # whitelisted but not restartable
    assert ("restart", "svc_b") not in backend.calls


async def test_tail_lines_clamped():
    ops, backend = make_ops()
    await ops.tail_service_log("svc_a", lines=5)
    await ops.tail_service_log("svc_a", lines=99999)
    assert ("tail", "svc_a", MIN_LOG_LINES) in backend.calls
    assert ("tail", "svc_a", MAX_LOG_LINES) in backend.calls


async def test_probe_port_gate():
    ops, _ = make_ops()
    assert (await ops.probe_local_status(8001))["ok"] is True
    with pytest.raises(NotWhitelisted):
        await ops.probe_local_status(8010)


async def test_status_envelope_shape():
    ops, _ = make_ops()
    env = ops.status_envelope()
    assert env["protocol_version"] == "1.2"
    assert env["equipment_status"] == "ready"
    assert env["activity"] == "idle"
    assert env["allowed_actions"] == []
    assert env["details"]["restartable"] == ["svc_a"]
