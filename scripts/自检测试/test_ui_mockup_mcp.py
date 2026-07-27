"""验证前端样图 MCP 的安全边界、渲染结果和工具声明。

除文本模型兼容用例写入 pytest 临时目录外，测试只在内存中创建图片；缺少
MCP/Playwright 依赖时由 pytest 明确跳过，完整验证使用运行脚本的 uv 环境执行。
"""

from __future__ import annotations

import asyncio
import base64
from importlib import util
from io import BytesIO
from pathlib import Path
import sys

import pytest


pytest.importorskip("mcp")
pytest.importorskip("playwright")
pytest.importorskip("PIL")

from mcp.types import ImageContent, TextContent  # noqa: E402
from PIL import Image  # noqa: E402


SCRIPT_PATH = Path(__file__).parents[1] / "前端样图" / "ui_mockup_mcp.py"


def _load_module():
    spec = util.spec_from_file_location("ui_mockup_mcp", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def renderer():
    return _load_module()


def _variant(renderer, variant_id: str, title: str, html: str):
    return renderer.VariantSpec(id=variant_id, title=title, html=html)


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<script>alert(1)</script>", "script"),
        ("<button onclick='alert(1)'>x</button>", "事件属性"),
        ("<img src='https://example.com/a.png'>", "外部或本地资源"),
        ("<img src='file:///C:/secret.png'>", "外部或本地资源"),
        ("<style>@import 'https://example.com/a.css';</style>", "CSS"),
    ],
)
def test_rejects_active_or_external_content(renderer, html: str, message: str) -> None:
    variants = [_variant(renderer, "A", "安全校验", html)]
    with pytest.raises(ValueError, match=message):
        renderer.validate_request(variants, 1_200, 800, "single")


def test_rejects_invalid_variant_shape(renderer) -> None:
    variants = [
        _variant(renderer, "A", "方向一", "<main>A</main>"),
        _variant(renderer, "A", "方向二", "<main>B</main>"),
    ]
    with pytest.raises(ValueError, match="id 不得重复"):
        renderer.validate_request(variants, 1_200, 800, "contact_sheet")
    with pytest.raises(ValueError, match="single 模式"):
        renderer.validate_request(variants, 1_200, 800, "single")


def test_renders_labeled_contact_sheet_in_memory(renderer) -> None:
    variants = [
        _variant(
            renderer,
            "A",
            "明亮卡片",
            "<style>body{margin:0;background:#eef2ff}main{margin:60px;padding:40px;"
            "background:white}</style><main><h1>A</h1></main>",
        ),
        _variant(
            renderer,
            "B",
            "深色侧栏",
            "<style>body{margin:0;background:#020617;color:white}</style>"
            "<main><h1>B</h1></main>",
        ),
        _variant(
            renderer,
            "C",
            "高密度表格",
            "<style>body{font:20px sans-serif}</style><table><tr><th>C</th></tr></table>",
        ),
    ]
    jpeg = asyncio.run(renderer.render_image(variants, 1_200, 800, "contact_sheet"))

    assert jpeg.startswith(b"\xff\xd8\xff")
    with Image.open(BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert image.width <= renderer.MAX_OUTPUT_SIZE[0]
        assert image.height <= renderer.MAX_OUTPUT_SIZE[1]


def test_tool_returns_text_and_image_content(renderer) -> None:
    variants = [
        _variant(renderer, "A", "方向一", "<main><h1>A</h1></main>"),
        _variant(renderer, "B", "方向二", "<main><h1>B</h1></main>"),
    ]
    content = asyncio.run(
        renderer.render_ui_mockups(
            variants=variants,
            viewport_width=1_000,
            viewport_height=700,
            output_mode="contact_sheet",
        )
    )

    assert len(content) == 2
    assert isinstance(content[0], TextContent)
    assert "没有写入项目文件" in content[0].text
    assert isinstance(content[1], ImageContent)
    assert content[1].mimeType == "image/jpeg"
    assert base64.b64decode(content[1].data).startswith(b"\xff\xd8\xff")


def test_text_model_tool_returns_only_markdown_temp_path(
    renderer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "ui-mockup-renderer"
    monkeypatch.setattr(renderer, "TEMP_OUTPUT_DIR", output_dir)
    variants = [
        _variant(renderer, "A", "方向一", "<main><h1>A</h1></main>"),
        _variant(renderer, "B", "方向二", "<main><h1>B</h1></main>"),
    ]

    content = asyncio.run(
        renderer.render_ui_mockups_for_text_model(
            variants=variants,
            viewport_width=1_000,
            viewport_height=700,
            output_mode="contact_sheet",
        )
    )

    assert len(content) == 1
    assert isinstance(content[0], TextContent)
    assert "![UI mockup contact_sheet]" in content[0].text
    assert "不要再次读取图片" in content[0].text
    output_files = list(output_dir.glob("*.jpg"))
    assert len(output_files) == 1
    assert output_files[0].read_bytes().startswith(b"\xff\xd8\xff")


def test_mcp_tool_is_declared_read_only(renderer) -> None:
    tools = asyncio.run(renderer.mcp.list_tools())
    inline_tool = next(item for item in tools if item.name == "render_ui_mockups")
    text_tool = next(
        item for item in tools if item.name == "render_ui_mockups_for_text_model"
    )

    assert inline_tool.annotations is not None
    assert inline_tool.annotations.readOnlyHint is True
    assert inline_tool.annotations.destructiveHint is False
    assert inline_tool.annotations.openWorldHint is False
    assert text_tool.annotations is not None
    assert text_tool.annotations.readOnlyHint is True
    assert text_tool.annotations.destructiveHint is False
    assert text_tool.annotations.idempotentHint is False
    assert text_tool.annotations.openWorldHint is False
