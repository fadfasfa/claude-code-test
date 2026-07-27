"""桌面己方/队友角色标识在 champ-select 结束瞬间的短暂保留回归。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime_interaction。
"""

from __future__ import annotations

from types import SimpleNamespace

from hextech.interfaces.desktop.runtime_interaction import _preserve_recent_selection_roles
from hextech.modules.game_context.client import DEFAULT_CONTEXT_TTL_SECONDS


def _groups(*, local_id: str = "", teammates: list[str] | None = None, selected=None, bench=None) -> dict:
    return {
        "local_champion_id": local_id,
        "teammate_champion_ids": teammates or [],
        "selected_champion_ids": selected or [],
        "bench_champion_ids": bench or [],
    }


def _ui() -> SimpleNamespace:
    return SimpleNamespace(_last_known_selection_roles=None)


def test_fresh_local_id_is_cached_and_passed_through(monkeypatch) -> None:
    ui = _ui()
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 100.0)

    result = _preserve_recent_selection_roles(
        ui, _groups(local_id="86", teammates=["1"], selected=["86", "1"], bench=["22"])
    )

    assert result["local_champion_id"] == "86"
    assert ui._last_known_selection_roles == {
        "local_champion_id": "86",
        "teammate_champion_ids": ["1"],
        "at": 100.0,
    }


def test_empty_local_id_restores_cache_when_hero_still_in_pool(monkeypatch) -> None:
    """回归：champ-select 结束(404)自然过渡的瞬间不应丢失"已选"标识。"""

    ui = _ui()
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 100.0)
    _preserve_recent_selection_roles(ui, _groups(local_id="86", teammates=["1"], selected=["86", "1"], bench=["22"]))

    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 102.0)
    result = _preserve_recent_selection_roles(ui, _groups(local_id="", selected=["86", "1"], bench=["22"]))

    assert result["local_champion_id"] == "86"
    assert result["teammate_champion_ids"] == ["1"]


def test_empty_local_id_stays_empty_when_champion_pool_truly_changed(monkeypatch) -> None:
    """回归：缓存英雄不在当前池里时不得误标——真正换局要如实清空。"""

    ui = _ui()
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 100.0)
    _preserve_recent_selection_roles(ui, _groups(local_id="86", selected=["86"], bench=[]))

    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 102.0)
    result = _preserve_recent_selection_roles(ui, _groups(local_id="", selected=["254"], bench=["22"]))

    assert result["local_champion_id"] == ""


def test_empty_local_id_stays_empty_after_ttl_expires(monkeypatch) -> None:
    """回归：超过与 ClientContextProvider 一致的 TTL 后不得继续沿用旧角色。"""

    ui = _ui()
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 100.0)
    _preserve_recent_selection_roles(ui, _groups(local_id="86", selected=["86"], bench=["22"]))

    expired_at = 100.0 + DEFAULT_CONTEXT_TTL_SECONDS + 1.0
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: expired_at)
    result = _preserve_recent_selection_roles(ui, _groups(local_id="", selected=["86"], bench=["22"]))

    assert result["local_champion_id"] == ""


def test_no_cache_and_empty_local_id_is_a_noop(monkeypatch) -> None:
    ui = _ui()
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_interaction.time.time", lambda: 100.0)

    result = _preserve_recent_selection_roles(ui, _groups(local_id="", selected=["86"], bench=[]))

    assert result["local_champion_id"] == ""
