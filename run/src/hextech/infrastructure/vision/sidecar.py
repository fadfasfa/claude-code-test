"""Vision sidecar 组合面与运行入口。"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Mapping


# 这是历史 ``sidecar`` 单文件入口的兼容 facade。职责模块使用显式导入，外部
# 调用方仍能在这里读取原有的检测和诊断符号，不需要再把所有实现重新聚回一处。
_FACADE_MODULE_NAMES = (
    "hextech.infrastructure.vision.sidecar_common",
    "hextech.infrastructure.vision.sidecar_scene_geometry",
    "hextech.infrastructure.vision.sidecar_fingerprints",
    "hextech.infrastructure.vision.sidecar_matching",
    "hextech.infrastructure.vision.sidecar_batch",
    "hextech.infrastructure.vision.sidecar_detection",
    "hextech.infrastructure.vision.sidecar_event_loop",
    "hextech.infrastructure.vision.sidecar_capture",
    "hextech.infrastructure.vision.sidecar_diagnostics",
)
_TEMPLATE_RUNTIME_COMPAT_NAMES = {
    "_hash_runtime_resource_stats",
    "_runtime_environment_signature",
    "_hint_cache_signature",
    "_template_entry_to_manifest",
    "_template_entry_to_cache",
    "_template_entry_from_cache",
    "_template_entry_from_manifest",
    "_template_indices",
    "_templates_by_index",
    "_matrix_from_cache",
    "_cache_manifest_bytes",
    "_read_cache_manifest",
    "_resource_signature_matches",
    "_rank_matrices_from_cache",
    "_read_template_runtime_cache",
    "_write_template_runtime_cache",
    "_cleanup_legacy_template_runtime_cache",
}


def __getattr__(name: str) -> Any:
    """按需转发兼容符号，避免实现模块之间继续使用 ``import *``。"""

    if name in _TEMPLATE_RUNTIME_COMPAT_NAMES:
        runtime = import_module("hextech.infrastructure.vision.template_runtime")
        return getattr(runtime, name)
    for module_name in _FACADE_MODULE_NAMES:
        module = import_module(module_name)
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    names = set(globals()) | _TEMPLATE_RUNTIME_COMPAT_NAMES
    for module_name in _FACADE_MODULE_NAMES:
        module = import_module(module_name)
        names.update(getattr(module, "__all__", (name for name in vars(module) if not name.startswith("__"))))
    return sorted(names)


def _forward_template_runtime(name: str):
    def _call(*args, **kwargs):
        from hextech.infrastructure.vision import template_runtime as _template_runtime

        return getattr(_template_runtime, name)(*args, **kwargs)

    return _call


def _forward_runner(name: str):
    def _call(*args, **kwargs):
        from hextech.infrastructure.vision import runner as _runner

        return getattr(_runner, name)(*args, **kwargs)

    return _call


load_default_template_index = _forward_template_runtime("load_default_template_index")
rank_template_matrices = _forward_template_runtime("rank_template_matrices")
build_template_index = _forward_template_runtime("build_template_index")


def load_or_build_default_template_runtime(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
    resource_signature: Mapping[str, Any] | None = None,
    status_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> Any:
    from hextech.infrastructure.vision import template_runtime as _template_runtime

    return _template_runtime.load_or_build_default_template_runtime(
        base_dir=base_dir,
        hint_cache=hint_cache,
        cache_file=cache_file,
        resource_signature=resource_signature,
        status_callback=status_callback,
    )


template_runtime_resource_signature = _forward_template_runtime("template_runtime_resource_signature")
template_runtime_hint_signature = _forward_template_runtime("template_runtime_hint_signature")
_hash_runtime_resource_stats = _forward_template_runtime("_hash_runtime_resource_stats")
_write_sidecar_status = _forward_runner("_write_sidecar_status")
_sanitize_bootstrap_error_message = _forward_runner("_sanitize_bootstrap_error_message")
_write_sidecar_bootstrap_from_env = _forward_runner("_write_sidecar_bootstrap_from_env")
_write_sidecar_ready_from_env = _forward_runner("_write_sidecar_ready_from_env")
_sidecar_exit_requested = _forward_runner("_sidecar_exit_requested")
run_once = _forward_runner("run_once")
run_loop = _forward_runner("run_loop")
build_parser = _forward_runner("build_parser")
main = _forward_runner("main")


# ``__getattr__`` 仍为旧的点属性调用提供窄兼容层；星号导入则只获得这份经过
# 审核的 facade，避免把 PIL、numpy 或各职责模块的内部实现重新泄漏成公共 API。
__all__ = (
    "_hash_runtime_resource_stats",
    "_sanitize_bootstrap_error_message",
    "_sidecar_exit_requested",
    "_write_sidecar_bootstrap_from_env",
    "_write_sidecar_ready_from_env",
    "_write_sidecar_status",
    "build_parser",
    "build_template_index",
    "load_default_template_index",
    "load_or_build_default_template_runtime",
    "main",
    "rank_template_matrices",
    "run_loop",
    "run_once",
    "template_runtime_hint_signature",
    "template_runtime_resource_signature",
)


if __name__ == "__main__":
    raise SystemExit(main())
