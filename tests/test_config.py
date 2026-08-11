import pytest

from lab_hostops.config import HostopsConfig, load_config

VALID = """
[hostops]
equipment_id = "hostops_test"
transport = "http"
host = "0.0.0.0"
port = 8060

[services]
whitelist = ["a", "b"]
restartable = ["a"]

[probe]
ports = [8000, 8010]
"""


def test_load_valid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(VALID)
    cfg = load_config(p)
    assert cfg.equipment_id == "hostops_test"
    assert cfg.services == ("a", "b")
    assert cfg.restartable == ("a",)
    assert cfg.probe_ports == (8000, 8010)
    assert not cfg.loopback_only


def test_restartable_must_be_subset_of_whitelist(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(VALID.replace('restartable = ["a"]', 'restartable = ["c"]'))
    with pytest.raises(ValueError, match="restartable"):
        load_config(p)


def test_missing_equipment_id_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(VALID.replace('equipment_id = "hostops_test"', ""))
    with pytest.raises(ValueError, match="equipment_id"):
        load_config(p)


def test_bad_transport_rejected():
    with pytest.raises(ValueError, match="transport"):
        HostopsConfig(equipment_id="x", transport="tcp").validate()


def test_loopback_detection():
    assert HostopsConfig(equipment_id="x", host="127.0.0.1").loopback_only
    assert not HostopsConfig(equipment_id="x", host="0.0.0.0").loopback_only


def test_example_config_parses():
    from pathlib import Path

    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.restartable and set(cfg.restartable) <= set(cfg.services)
