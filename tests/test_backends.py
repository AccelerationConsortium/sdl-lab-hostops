from pathlib import Path

from lab_hostops.backends import NssmBackend, SystemdBackend, parse_kv_block, tail_file


def test_systemd_unit_normalization():
    assert SystemdBackend.unit("plateloc") == "plateloc.service"
    assert SystemdBackend.unit("ac-organic-lab-api.service") == "ac-organic-lab-api.service"
    assert SystemdBackend.unit("some.timer") == "some.timer"


def test_parse_kv_block():
    props = parse_kv_block("ActiveState=active\nSubState=running\nNRestarts=2\nnot a kv line")
    assert props == {"ActiveState": "active", "SubState": "running", "NRestarts": "2"}


def test_tail_file(tmp_path):
    p = tmp_path / "svc.out.log"
    p.write_text("".join(f"line{i}\n" for i in range(100)))
    out = tail_file(p, 3)
    assert out.splitlines() == ["line97", "line98", "line99"]
    assert "no such log file" in tail_file(tmp_path / "missing.log", 3)


async def test_nssm_tail_reads_both_log_files(tmp_path):
    (tmp_path / "plateloc.out.log").write_text("stdout here\n")
    (tmp_path / "plateloc.err.log").write_text("stderr here\n")
    backend = NssmBackend(str(tmp_path))
    out = await backend.tail_log("plateloc", 10)
    assert "stdout here" in out and "stderr here" in out
    assert "plateloc.out.log" in out and "plateloc.err.log" in out


def test_nssm_default_log_dir():
    assert NssmBackend(None).log_dir == Path("C:/SDL_Logs")
