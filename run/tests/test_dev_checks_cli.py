"""验证 dev_checks 兼容入口只负责选择并委托 pytest 门禁。"""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from tools import dev_checks


def test_manual_modes_live_in_dedicated_module_and_health_wrapper_keeps_patch_surface(
    monkeypatch,
    tmp_path,
) -> None:
    from tools import dev_check_manual

    sentinel_store = object()
    evidence_dir = tmp_path / "evidence"
    calls: list[tuple[object, object]] = []

    monkeypatch.setattr(dev_checks, "runtime_store", sentinel_store)
    monkeypatch.setattr(dev_checks, "DATA_EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(
        dev_check_manual,
        "build_hextech_scrape_health_summary",
        lambda *, runtime_store_module, data_evidence_dir: calls.append(
            (runtime_store_module, data_evidence_dir)
        )
        or {"healthy": True},
    )

    assert dev_checks.build_hextech_scrape_health_summary() == {"healthy": True}
    assert calls == [(sentinel_store, evidence_dir)]
    assert callable(dev_check_manual.check_bundle_manifest)
    assert callable(dev_check_manual.run_manual_web_synergy)


@pytest.mark.parametrize(
    ("argv", "marker", "success_summary"),
    [
        ([], "dev_gate and not deep", "所有开发 fast 自检通过。"),
        (["--overlay-only"], "dev_gate and overlay and not deep", "overlay fast 自检通过。"),
        (["--deep"], "dev_gate", "所有开发深度自检通过。"),
        (["--overlay-only", "--deep"], "dev_gate and overlay", "overlay 深度自检通过。"),
    ],
)
def test_main_delegates_automated_checks_to_pytest(
    monkeypatch,
    capsys,
    argv: list[str],
    marker: str,
    success_summary: str,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_run(command: list[str], *, cwd: str, check: bool) -> CompletedProcess[str]:
        calls.append((command, cwd))
        assert check is False
        return CompletedProcess(command, 0)

    monkeypatch.setattr(dev_checks.subprocess, "run", fake_run)

    assert dev_checks.main(argv) == 0
    assert calls == [
        ([dev_checks.sys.executable, "-m", "pytest", "-m", marker], str(dev_checks.RUN_DIR)),
    ]
    assert capsys.readouterr().out.strip() == success_summary


def test_main_returns_pytest_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(
        dev_checks.subprocess,
        "run",
        lambda command, *, cwd, check: CompletedProcess(command, 5),
    )
    assert dev_checks.main([]) == 5


def test_bundle_manifest_mode_does_not_invoke_pytest(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(dev_checks, "check_bundle_manifest", lambda *, verbose: called.append(verbose))
    monkeypatch.setattr(
        dev_checks.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("bundle manifest mode must not invoke pytest"),
    )

    assert dev_checks.main(["--bundle-manifest"]) == 0
    assert called == [True]


def test_manual_web_synergy_mode_does_not_invoke_pytest(monkeypatch) -> None:
    monkeypatch.setattr(dev_checks, "run_manual_web_synergy", lambda _args: {"passed": True})
    monkeypatch.setattr(
        dev_checks.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("manual web mode must not invoke pytest"),
    )

    assert dev_checks.main(["--manual-web-synergy"]) == 0
