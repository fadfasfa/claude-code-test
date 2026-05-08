from __future__ import annotations

"""bundle manifest 稳定验证入口。

这个脚本从仓库根目录直接运行，内部临时把 `run/` 加入 `sys.path`，
避免手写 `run.tools.*` 导入触发 package 初始化副作用。
"""

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = REPO_ROOT / "run"
if str(RUN_ROOT) not in sys.path:
    sys.path.insert(0, str(RUN_ROOT))

from tools.bundle_manifest import build_bundle_manifest  # noqa: E402


def main() -> int:
    manifest = build_bundle_manifest(RUN_ROOT)
    summary = {
        key: len(value) if isinstance(value, list) else value
        for key, value in manifest.items()
    }
    print(summary)

    has_key = "hextech_snapshot_files" in manifest
    print("has_hextech_snapshot_files", has_key)
    if not has_key:
        return 1

    files = manifest["hextech_snapshot_files"]
    print("hextech_snapshot_files_count", len(files))
    print("hextech_snapshot_files_sample", files[:5])
    has_synergy_key = "synergy_data_file" in manifest
    print("has_synergy_data_file", has_synergy_key)
    if not has_synergy_key:
        return 1
    print("synergy_data_file", manifest["synergy_data_file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
