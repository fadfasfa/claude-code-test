"""App是唯一Overlay owner；数据任务管理器不会启动第二套watchdog。"""
from types import SimpleNamespace
from hextech.interfaces.desktop.service_manager import ServiceManager
from hextech.bootstrap.runtime_owner import AppRuntimeOwner


def test_data_manager_has_no_overlay_lifecycle():
    manager=ServiceManager(start_data_service_func=lambda:None)
    assert not hasattr(manager,"start_game_overlay")
    assert not hasattr(manager,"ensure_game_overlay_healthy")


def test_context_degradation_does_not_restart_overlay(tmp_path):
    calls=[]
    runtime=SimpleNamespace(snapshot=lambda:{"status":"running","desired_enabled":True,"context_status":"degraded"},
        set_enabled=lambda enabled:calls.append(enabled),shutdown=lambda reason:None)
    owner=AppRuntimeOwner(overlay_runtime=runtime,event_log_path=tmp_path/"events.jsonl",monitor=False,
        retention_worker=SimpleNamespace(request=lambda:None))
    try:
        owner.tick()
        assert calls==[]
    finally:
        owner.stop(timeout=2)
