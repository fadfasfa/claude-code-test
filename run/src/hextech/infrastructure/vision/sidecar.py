"""Vision sidecar 组合面与运行入口。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.infrastructure.vision import template_runtime as _template_runtime_module
from hextech.infrastructure.vision.sidecar_common import *
from hextech.infrastructure.vision.sidecar_scene_geometry import *
from hextech.infrastructure.vision.sidecar_fingerprints import *
from hextech.infrastructure.vision.sidecar_matching import *
from hextech.infrastructure.vision.sidecar_detection import *
from hextech.infrastructure.vision.sidecar_event_loop import *
from hextech.infrastructure.vision.sidecar_capture import *
from hextech.infrastructure.vision.sidecar_diagnostics import *
_hash_runtime_resource_stats = _template_runtime_module._hash_runtime_resource_stats
_runtime_environment_signature = _template_runtime_module._runtime_environment_signature
_hint_cache_signature = _template_runtime_module._hint_cache_signature
_template_entry_to_manifest = _template_runtime_module._template_entry_to_manifest
_template_entry_to_cache = _template_runtime_module._template_entry_to_cache
_template_entry_from_cache = _template_runtime_module._template_entry_from_cache
_template_entry_from_manifest = _template_runtime_module._template_entry_from_manifest
_template_indices = _template_runtime_module._template_indices
_templates_by_index = _template_runtime_module._templates_by_index
_matrix_from_cache = _template_runtime_module._matrix_from_cache
_cache_manifest_bytes = _template_runtime_module._cache_manifest_bytes
_read_cache_manifest = _template_runtime_module._read_cache_manifest
_resource_signature_matches = _template_runtime_module._resource_signature_matches
_rank_matrices_from_cache = _template_runtime_module._rank_matrices_from_cache
_read_template_runtime_cache = _template_runtime_module._read_template_runtime_cache
_write_template_runtime_cache = _template_runtime_module._write_template_runtime_cache
_cleanup_legacy_template_runtime_cache = _template_runtime_module._cleanup_legacy_template_runtime_cache


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


def load_or_build_default_template_runtime(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
    resource_signature: Mapping[str, Any] | None = None,
    status_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TemplateRuntime:
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


if __name__ == "__main__":
    raise SystemExit(main())
