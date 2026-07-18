"""synergy 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    FetchedResource,
    Path,
    RUN_DIR,
    SynergyEntry,
    SynergyExtractor,
    SynergyWriter,
    TemporaryDirectory,
    _augment_map,
    _collision_core_info,
    _core_info,
    _normalize_synergy_items,
    _synergy_item_to_display_string,
    build_champion_lookup,
    icon_resolver,
    json,
    patch,
)

pytestmark = pytest.mark.dev_gate

def test_apexlol_hextech_map_size_limit() -> None:
    class OversizeResponse:
        headers = {"Content-Length": str(icon_resolver.MAX_APEXLOL_HEXTECH_MAP_BYTES + 1)}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b""

        def close(self) -> None:
            return None

    original_cache = icon_resolver._APEXLOL_MAP_CACHE
    try:
        with TemporaryDirectory() as tmp_dir:
            cached = {"cached": "slug"}
            icon_resolver._APEXLOL_MAP_CACHE = ("cached-path", 1.0, cached)
            with patch("hextech.modules.acquisition.common.icons.requests.get", return_value=OversizeResponse()):
                result = icon_resolver.load_apexlol_hextech_map(config_dir=tmp_dir, force_refresh=True)
            assert result == cached
            assert not (Path(tmp_dir) / "Augment_Apexlol_Map.json").exists()
    finally:
        icon_resolver._APEXLOL_MAP_CACHE = original_cache

def test_synergy_structured_payloads() -> None:
    payload = {
        "55": {
            "synergy_items": [
                {
                    "augment_names": ["利刃华尔兹"],
                    "tier": "黄金",
                    "rating": "A",
                    "tag": "强力联动",
                    "author": "ApexLoL",
                    "content": "卡特琳娜 R 可以触发这条联动。",
                }
            ]
        }
    }
    extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )

    result = extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/snapshot/data.json",
            text=json.dumps(payload, ensure_ascii=False),
            source="test",
        )
    ])

    assert "katarina" in result
    assert result["katarina"][0].augment_names == ["利刃华尔兹"]
    assert result["katarina"][0].tier == "黄金"

    entry = SynergyEntry(
        champion_slug="katarina",
        augment_names=["利刃华尔兹"],
        tier="黄金",
        rating="A",
        tag="强力联动",
        author="ApexLoL",
        is_original=True,
        content="卡特琳娜 R 可以触发这条联动。",
        upvotes=3,
        downvotes=1,
    )

    writer_payload = SynergyWriter(_core_info()).build_payload({"katarina": [entry]})
    assert writer_payload["55"]["synergy_items"][0]["augment_names"] == ["利刃华尔兹"]
    assert "利刃华尔兹 | 黄金 | 评分 A" in writer_payload["55"]["synergies"][0]

    legacy = "利刃华尔兹 | 黄金 | 评分 A | 强力联动 | A | B站晴转小雨Yy_ | 原创 | 卡特琳娜 R 可以触发这条联动。"
    items = _normalize_synergy_items([], [legacy])
    assert items[0]["augment_names"] == ["利刃华尔兹"]
    assert items[0]["rating"] == "A"
    assert items[0]["is_original"]
    assert _synergy_item_to_display_string(items[0]).split(" | ")[4:6] == ["0", "0"]

    html_extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )
    html = """
    <html><body>
    <div>利刃华尔兹</div>
    <div>黄金</div>
    <div>D 级</div>
    <div>陷阱</div>
    <div>0</div>
    <div>0</div>
    <div>作者</div>
    <div>ApexLoL</div>
    <p>卡特琳娜 R 在这个组合里会卡手。</p>
    </body></html>
    """

    parsed = html_extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/champions/Katarina",
            text=html,
            source="test",
        )
    ])

    assert parsed["katarina"][0].rating == "D"
    assert parsed["katarina"][0].tag == "陷阱"

def test_synergy_alias_collision_guard() -> None:
    def entry(slug: str, content: str) -> SynergyEntry:
        return SynergyEntry(
            champion_slug=slug,
            augment_names=[content],
            tier="黄金",
            rating="A",
            tag="强力联动",
            author="ApexLoL",
            is_original=True,
            content=content,
            upvotes=0,
            downvotes=0,
        )

    writer = SynergyWriter(_collision_core_info())
    payload = writer.build_payload(
        {
            "vi": [entry("vi", "蔚专属联动")],
            "viego": [entry("viego", "佛耶戈专属联动")],
            "viktor": [entry("viktor", "维克托专属联动")],
        }
    )

    assert payload["254"]["synergy_items"][0]["content"] == "蔚专属联动"
    assert payload["234"]["synergy_items"][0]["content"] == "佛耶戈专属联动"
    assert payload["112"]["synergy_items"][0]["content"] == "维克托专属联动"

    payload_without_viktor = writer.build_payload({"vi": [entry("vi", "蔚专属联动")]})
    assert payload_without_viktor["112"]["synergy_items"] == []

def test_synergy_playwright_calibrator_contract() -> None:
    tool_path = RUN_DIR / "tooling" / "diagnostics" / "synergy.py"
    text = tool_path.read_text(encoding="utf-8")
    assert "sync_playwright" in text
    assert "只访问本地 Hextech Web/API" in text
    assert "apexlol.info" not in text.lower()
    assert "build_synergy_data_path" in text
    assert "api_quarantined" in text
    assert "if duplicate_with else []" in text
