# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.13,<2",
#   "pillow>=11.3,<13",
#   "playwright>=1.59,<2",
# ]
# ///
"""把自包含 HTML/CSS 方案渲染成可在 Plan Mode 中直接审阅的样图。

本模块作为本地 stdio MCP server 使用。它只接收调用方传入的 HTML，关闭脚本并
阻断网络后在临时浏览器上下文中截图；不读取项目文件，也不把 HTML 落盘。默认
直接返回图片；文本模型兼容工具只把 JPEG 写入专用系统临时目录。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import Annotations, ImageContent, TextContent, ToolAnnotations
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field


MAX_VARIANTS = 3
MAX_HTML_BYTES = 120_000
MIN_VIEWPORT_WIDTH = 800
MAX_VIEWPORT_WIDTH = 1_920
MIN_VIEWPORT_HEIGHT = 600
MAX_VIEWPORT_HEIGHT = 1_400
MAX_OUTPUT_SIZE = (2_048, 1_536)
TEMP_OUTPUT_DIR = Path(tempfile.gettempdir()) / "ui-mockup-renderer"
SYSTEM_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)

_FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "base"}
_URL_ATTRIBUTES = {
    "action",
    "formaction",
    "href",
    "poster",
    "src",
    "xlink:href",
}
_DANGEROUS_SCHEMES = ("http:", "https:", "file:", "javascript:", "vbscript:", "//")
_CSS_EXTERNAL_RE = re.compile(
    r"(?:@import\s+['\"]?\s*|url\(\s*['\"]?\s*)(?:https?:|file:|//)",
    re.IGNORECASE,
)


class VariantSpec(BaseModel):
    """单个候选方向；HTML 必须包含全部样式和视觉内容。"""

    id: Literal["A", "B", "C"] = Field(description="候选编号，使用 A、B 或 C")
    title: str = Field(min_length=1, max_length=60, description="候选方向的短标题")
    html: str = Field(min_length=1, description="无脚本、无外链的自包含 HTML/CSS")


@dataclass(frozen=True)
class RenderedVariant:
    spec: VariantSpec
    png: bytes


class _SafetyParser(HTMLParser):
    """拒绝会执行代码或访问外部/本地资源的 HTML 结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _FORBIDDEN_TAGS:
            raise ValueError(f"不允许使用 <{normalized_tag}> 标签")

        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = (raw_value or "").strip()
            lowered = value.lower()
            if name.startswith("on"):
                raise ValueError(f"不允许使用事件属性 {raw_name}")
            if name == "style" and _CSS_EXTERNAL_RE.search(value):
                raise ValueError("内联 style 不允许引用外部或本地资源")
            if name not in _URL_ATTRIBUTES or not value:
                continue
            if lowered.startswith(_DANGEROUS_SCHEMES):
                raise ValueError(f"属性 {raw_name} 不允许引用外部或本地资源")
            if lowered.startswith("data:") and not lowered.startswith("data:image/"):
                raise ValueError(f"属性 {raw_name} 只允许 data:image 资源")

    handle_startendtag = handle_starttag


def validate_request(
    variants: list[VariantSpec],
    viewport_width: int,
    viewport_height: int,
    output_mode: Literal["contact_sheet", "single"],
) -> None:
    """在启动浏览器前完成全部确定性输入校验。"""

    if not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError("variants 必须包含 1–3 个候选方案")
    if output_mode == "contact_sheet" and len(variants) < 2:
        raise ValueError("contact_sheet 模式至少需要 2 个候选方案")
    if output_mode == "single" and len(variants) != 1:
        raise ValueError("single 模式只能包含 1 个候选方案")
    if not MIN_VIEWPORT_WIDTH <= viewport_width <= MAX_VIEWPORT_WIDTH:
        raise ValueError(
            f"viewport_width 必须在 {MIN_VIEWPORT_WIDTH}–{MAX_VIEWPORT_WIDTH} 之间"
        )
    if not MIN_VIEWPORT_HEIGHT <= viewport_height <= MAX_VIEWPORT_HEIGHT:
        raise ValueError(
            f"viewport_height 必须在 {MIN_VIEWPORT_HEIGHT}–{MAX_VIEWPORT_HEIGHT} 之间"
        )

    ids = [variant.id for variant in variants]
    if len(ids) != len(set(ids)):
        raise ValueError("候选方案 id 不得重复")

    for variant in variants:
        title = variant.title.strip()
        if not title:
            raise ValueError(f"候选方案 {variant.id} 的 title 不得为空")
        encoded_size = len(variant.html.encode("utf-8"))
        if encoded_size > MAX_HTML_BYTES:
            raise ValueError(
                f"候选方案 {variant.id} 的 HTML 为 {encoded_size} bytes，"
                f"超过 {MAX_HTML_BYTES} bytes 上限"
            )
        if _CSS_EXTERNAL_RE.search(variant.html):
            raise ValueError(f"候选方案 {variant.id} 的 CSS 不允许引用外部或本地资源")
        parser = _SafetyParser()
        try:
            parser.feed(variant.html)
            parser.close()
        except ValueError as exc:
            raise ValueError(f"候选方案 {variant.id}: {exc}") from exc


async def _render_variants(
    variants: list[VariantSpec], viewport_width: int, viewport_height: int
) -> list[RenderedVariant]:
    rendered: list[RenderedVariant] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for variant in variants:
                context = await browser.new_context(
                    viewport={"width": viewport_width, "height": viewport_height},
                    java_script_enabled=False,
                    service_workers="block",
                )
                try:
                    page = await context.new_page()
                    await page.route("**/*", lambda route: route.abort())
                    await page.set_content(variant.html, wait_until="domcontentloaded")
                    await page.emulate_media(reduced_motion="reduce")
                    png = await page.screenshot(
                        type="png",
                        full_page=False,
                        animations="disabled",
                        caret="hide",
                    )
                    rendered.append(RenderedVariant(spec=variant, png=png))
                finally:
                    await context.close()
        finally:
            await browser.close()
    return rendered


def _default_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先使用已知系统 CJK 字体，避免中文候选标题显示为方框。"""

    for font_path in SYSTEM_FONT_CANDIDATES:
        if font_path.is_file():
            return ImageFont.truetype(font_path, size=size)

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow 旧版本兼容；锁定版本通常不会进入此分支。
        return ImageFont.load_default()


def _encode_jpeg(image: Image.Image) -> bytes:
    image.thumbnail(MAX_OUTPUT_SIZE, Image.Resampling.LANCZOS)
    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=88,
        optimize=True,
        progressive=True,
    )
    return output.getvalue()


def _compose_contact_sheet(rendered: list[RenderedVariant]) -> bytes:
    columns = 2
    rows = math.ceil(len(rendered) / columns)
    margin = 24
    gap = 24
    cell_width = 960
    preview_height = 600
    header_height = 58
    cell_height = header_height + preview_height
    sheet_width = margin * 2 + columns * cell_width + gap
    sheet_height = margin * 2 + rows * cell_height + max(0, rows - 1) * gap
    sheet = Image.new("RGB", (sheet_width, sheet_height), "#111827")
    draw = ImageDraw.Draw(sheet)
    title_font = _default_font(26)

    for index, item in enumerate(rendered):
        row, column = divmod(index, columns)
        if len(rendered) % 2 == 1 and index == len(rendered) - 1:
            x = (sheet_width - cell_width) // 2
        else:
            x = margin + column * (cell_width + gap)
        y = margin + row * (cell_height + gap)
        draw.rounded_rectangle(
            (x, y, x + cell_width, y + cell_height),
            radius=16,
            fill="#f8fafc",
        )
        draw.text(
            (x + 20, y + 15),
            f"{item.spec.id} · {item.spec.title.strip()}",
            font=title_font,
            fill="#0f172a",
        )
        with Image.open(BytesIO(item.png)) as screenshot:
            preview = ImageOps.contain(
                screenshot.convert("RGB"),
                (cell_width, preview_height),
                method=Image.Resampling.LANCZOS,
            )
            preview_x = x + (cell_width - preview.width) // 2
            preview_y = y + header_height + (preview_height - preview.height) // 2
            sheet.paste(preview, (preview_x, preview_y))

    return _encode_jpeg(sheet)


def _compose_single(rendered: RenderedVariant) -> bytes:
    with Image.open(BytesIO(rendered.png)) as screenshot:
        return _encode_jpeg(screenshot.convert("RGB"))


async def render_image(
    variants: list[VariantSpec],
    viewport_width: int,
    viewport_height: int,
    output_mode: Literal["contact_sheet", "single"],
) -> bytes:
    validate_request(variants, viewport_width, viewport_height, output_mode)
    rendered = await _render_variants(variants, viewport_width, viewport_height)
    if output_mode == "single":
        return _compose_single(rendered[0])
    return _compose_contact_sheet(rendered)


def _write_temp_preview(jpeg: bytes, output_mode: str) -> Path:
    """把样图写入专用 OS 临时目录，绝不使用当前工作目录。"""

    TEMP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f"ui-mockup-{output_mode}-",
        suffix=".jpg",
        dir=TEMP_OUTPUT_DIR,
        delete=False,
    ) as output:
        output.write(jpeg)
        return Path(output.name).resolve()


mcp = FastMCP(
    "ui-mockup-renderer",
    instructions=(
        "把 1–3 个无脚本、无外链的自包含 HTML/CSS UI 方案渲染成样图。"
        "默认工具直接返回图片；文本模型兼容工具只写专用系统临时目录并返回路径。"
        "两个工具都不读取或写入项目文件，也不访问网络。"
    ),
)


@mcp.tool(
    name="render_ui_mockups",
    title="Render UI mockup variants",
    description=(
        "Render 2–3 self-contained HTML/CSS UI directions into one labeled contact sheet, "
        "or render one selected direction at full size. Use for preview-only visual decisions; "
        "the HTML must contain no scripts, event handlers, external URLs, or local file URLs."
    ),
    annotations=ToolAnnotations(
        title="Render UI mockup variants",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def render_ui_mockups(
    variants: list[VariantSpec],
    viewport_width: int = 1_440,
    viewport_height: int = 960,
    output_mode: Literal["contact_sheet", "single"] = "contact_sheet",
) -> list[TextContent | ImageContent]:
    """返回可直接显示在对话中的联系表或单方案大图。"""

    jpeg = await render_image(variants, viewport_width, viewport_height, output_mode)
    labels = ", ".join(f"{variant.id}={variant.title.strip()}" for variant in variants)
    summary = (
        f"已渲染 {len(variants)} 个候选方案（{labels}）；"
        f"viewport={viewport_width}x{viewport_height}，mode={output_mode}。"
        "图片仅作为本次规划预览，没有写入项目文件。"
    )
    return [
        TextContent(type="text", text=summary),
        ImageContent(
            type="image",
            data=base64.b64encode(jpeg).decode("ascii"),
            mimeType="image/jpeg",
            annotations=Annotations(audience=["user"]),
        ),
    ]


@mcp.tool(
    name="render_ui_mockups_for_text_model",
    title="Render UI mockups for a text-only model",
    description=(
        "Render 2–3 self-contained HTML/CSS UI directions, or one selected direction, "
        "to a JPEG in the dedicated OS temporary directory and return only a Markdown "
        "local-image path. Use when the provider cannot accept image tool results."
    ),
    annotations=ToolAnnotations(
        title="Render UI mockups for a text-only model",
        # 该工具只新增一个工具自管的临时输出载体，不读取、覆盖或修改用户/项目数据。
        # Claude Code Plan Mode 以此风险语义判断工具是否可用于规划预览。
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
async def render_ui_mockups_for_text_model(
    variants: list[VariantSpec],
    viewport_width: int = 1_440,
    viewport_height: int = 960,
    output_mode: Literal["contact_sheet", "single"] = "contact_sheet",
) -> list[TextContent]:
    """为文本模型返回纯文本路径，避免把 ImageContent 再传给模型。"""

    jpeg = await render_image(variants, viewport_width, viewport_height, output_mode)
    output_path = _write_temp_preview(jpeg, output_mode)
    labels = ", ".join(f"{variant.id}={variant.title.strip()}" for variant in variants)
    markdown_path = output_path.as_posix()
    summary = (
        f"已渲染 {len(variants)} 个候选方案（{labels}）；"
        f"viewport={viewport_width}x{viewport_height}，mode={output_mode}。\n"
        f"样图：![UI mockup {output_mode}](<{markdown_path}>)\n"
        f"本地文件：{output_path}\n"
        "图片仅写入专用系统临时目录，没有写入项目；请直接把上述路径展示给用户，"
        "不要再次读取图片。"
    )
    return [TextContent(type="text", text=summary)]


async def _self_test() -> dict[str, int | str]:
    variants = [
        VariantSpec(
            id="A",
            title="明亮卡片",
            html=(
                "<style>body{margin:0;background:#eef2ff;font:24px sans-serif;}"
                ".card{margin:72px;padding:48px;background:white;border-radius:24px}</style>"
                "<main class='card'><h1>Dashboard A</h1><p>清晰、明亮、留白充足。</p></main>"
            ),
        ),
        VariantSpec(
            id="B",
            title="深色侧栏",
            html=(
                "<style>body{margin:0;background:#020617;color:#e2e8f0;font:24px sans-serif;}"
                ".layout{display:grid;grid-template-columns:280px 1fr;height:100vh}"
                "aside{padding:40px;background:#0f172a}main{padding:64px}</style>"
                "<div class='layout'><aside>Navigation</aside><main><h1>Dashboard B</h1>"
                "<p>高密度、强层级。</p></main></div>"
            ),
        ),
    ]
    jpeg = await render_image(variants, 1_200, 800, "contact_sheet")
    with Image.open(BytesIO(jpeg)) as image:
        width, height = image.size
        image_format = image.format or "unknown"
    return {
        "status": "ok",
        "format": image_format,
        "bytes": len(jpeg),
        "width": width,
        "height": height,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="执行一次无落盘的双方案渲染自检并输出 JSON",
    )
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(asyncio.run(_self_test()), ensure_ascii=False))
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
