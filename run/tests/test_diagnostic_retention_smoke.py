"""验证冻结包内部 v1/v2 timeline 留存烟测合同。"""

from __future__ import annotations


def test_diagnostic_retention_smoke_keeps_legacy_and_terminal_v2(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_retention_smoke import (
        run_diagnostic_retention_smoke,
    )

    result = run_diagnostic_retention_smoke(tmp_path)

    assert result["ok"] is True
    assert result["legacy_v1_count"] == 20
    assert result["v2_count"] == 1
    assert all(result["checks"].values())
