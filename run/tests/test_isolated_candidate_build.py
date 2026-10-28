from types import SimpleNamespace
import pytest
from tooling.build import package, runtime_shutdown


def test_isolated_candidate_requires_explicit_roots_and_forbids_deploy():
    for args in (["--isolated-candidate"], ["--isolated-candidate", "--deploy"]):
        with pytest.raises(SystemExit):
            package.parse_build_args(args)


@pytest.mark.parametrize("name", [runtime_shutdown.APP_NAME, "League of Legends.exe"])
def test_isolated_preflight_never_stops_any_process(monkeypatch, name):
    process = SimpleNamespace(pid=17, info={"name": name})
    monkeypatch.setattr(runtime_shutdown.psutil, "process_iter", lambda _: [process])
    with pytest.raises(RuntimeError, match="isolated_candidate_process_conflict"):
        runtime_shutdown.require_idle_for_isolated_candidate()


def test_isolated_smoke_does_not_call_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(package, "require_idle_for_isolated_candidate", lambda: None)
    monkeypatch.setattr(package, "shutdown_for_package", lambda *_: pytest.fail("formal shutdown forbidden"))
    commands = []
    monkeypatch.setattr(package.subprocess, "run", lambda command, **kw: commands.append(command))
    package.run_packaged_smoke(tmp_path, smoke_root=tmp_path / "smoke", isolated_candidate=True)
    assert "--smoke-root" in commands[0]


def test_isolated_launcher_binds_runtime_and_guide(tmp_path):
    launcher = package.write_portable_launcher(tmp_path, isolated_candidate=True)
    assert 'set "HEXTECH_VAR_DIR=%~dp0var"' in launcher.read_text()
    guide = package.write_first_run_guide(tmp_path, isolated_candidate=True).read_text(encoding="utf-8")
    assert "由包内构建清单固定" in guide
    assert "不回退到正式运行目录" in guide
