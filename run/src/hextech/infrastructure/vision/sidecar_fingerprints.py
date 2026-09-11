"""Vision sidecar fingerprints 职责模块。"""
from __future__ import annotations

from dataclasses import dataclass

from hextech.infrastructure.vision.sidecar_common import (
    Any,
    BODY_SHARD_STRONG_CONFIDENCE,
    BODY_SHARD_SUFFIX,
    BODY_SHARD_SUFFIX_SIZE,
    BODY_SHARD_SUPPORT_CONFIDENCE,
    BODY_SHARD_VERY_STRONG_CONFIDENCE,
    FINGERPRINT_SIZE,
    INDEX_DATA_DIR,
    Image,
    ImageDraw,
    ImageFilter,
    ImageFont,
    Mapping,
    NAME_FINGERPRINT_SIZE,
    Path,
    SLOT_COUNT,
    Sequence,
    TEXT_DECORATION_MAX_WIDTH,
    TEXT_DECORATION_MIN_HEIGHT_RATIO,
    TemplateEntry,
    _RankMatrices,
    _clean_text,
    _template_runtime_module,
    hashlib,
    json,
    load_augment_manifest_entries,
    load_augment_name_to_icon_map,
    lru_cache,
    normalize_augment_id,
    np,
)

def _resampling_lanczos() -> int:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _fit_mask_to_canvas(mask: Image.Image, size: tuple[int, int]) -> Image.Image | None:
    """按前景 bbox 紧裁并等比放入固定画布，避免透明边或卡面背景主导匹配。"""

    gray = mask.convert("L")
    binary = gray.point(lambda value: 255 if value >= 32 else 0)
    bbox = binary.getbbox()
    if bbox is None:
        return None
    foreground_ratio = float(np.mean(np.asarray(binary, dtype=np.uint8) > 0))
    if foreground_ratio >= 0.98:
        return None
    cropped = gray.crop(bbox)
    target_width, target_height = size
    scale = min(target_width / max(1, cropped.width), target_height / max(1, cropped.height))
    resized = cropped.resize(
        (max(1, int(round(cropped.width * scale))), max(1, int(round(cropped.height * scale)))),
        _resampling_lanczos(),
    )
    canvas = Image.new("L", size, 0)
    canvas.paste(resized, ((target_width - resized.width) // 2, (target_height - resized.height) // 2))
    return canvas


def _alpha_or_luminance_mask(image: Image.Image) -> Image.Image:
    """模板优先取 alpha 作为字形轮廓；无 alpha 时退化为亮度前景。"""

    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
        alpha_bbox = alpha.point(lambda value: 255 if value >= 32 else 0).getbbox()
        if alpha_bbox is not None:
            alpha_ratio = sum(1 for value in alpha.getdata() if value >= 32) / max(1, alpha.width * alpha.height)
            if alpha_ratio < 0.98:
                return alpha
    if image.mode == "P" and "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
        alpha_bbox = alpha.point(lambda value: 255 if value >= 32 else 0).getbbox()
        if alpha_bbox is not None:
            alpha_ratio = sum(1 for value in alpha.getdata() if value >= 32) / max(1, alpha.width * alpha.height)
            if alpha_ratio < 0.98:
                return alpha
    gray = image.convert("L")
    return gray.point(lambda value: 255 if value >= 24 else 0)


def _bright_glyph_mask(image: Image.Image) -> Image.Image:
    """从深色卡面截图中分割棱彩/浅色字形，压掉暗纹理背景。"""

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    high = rgb.max(axis=2).astype(np.int16)
    low = rgb.min(axis=2).astype(np.int16)
    # 真机图标/卡名明显亮于暗卡面；高饱和棱彩和浅奶油字都应保留。
    foreground = (high >= 112) | ((high >= 78) & ((high - low) >= 34))
    return Image.fromarray(foreground.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))


def _adaptive_icon_mask(image: Image.Image, *, template: bool) -> Image.Image:
    """统一模板透明字形与实战金色卡面图标，避免固定亮度把背景一起缩放。"""

    if template:
        return _alpha_or_luminance_mask(image).point(lambda value: 255 if value >= 32 else 0)
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return Image.new("L", image.size, 0)
    high = rgb.max(axis=2).astype(np.int16)
    low = rgb.min(axis=2).astype(np.int16)
    # 按当前 ROI 自适应阈值，同时保留棱彩高色差部分；阈值有上下界，防止全暗 HUD 噪声抬升。
    threshold = int(max(88, min(150, float(np.percentile(high, 84)))))
    mask = (high >= threshold) | ((high >= max(72, threshold - 28)) & ((high - low) >= 34))
    return Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))


@dataclass
class _MaskComponent:
    left: int
    top: int
    right: int
    bottom: int
    area: int
    runs: list[tuple[int, int, int]]

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def _mask_components(mask: Image.Image) -> tuple[np.ndarray, list[_MaskComponent]]:
    """按横向 run 标记 8 邻接组件，避免逐像素 Python flood-fill。"""

    binary = np.asarray(mask.convert("L"), dtype=np.uint8) >= 128
    if binary.ndim != 2 or binary.size == 0:
        return binary, []

    parents: list[int] = []
    runs: list[tuple[int, int, int]] = []
    previous: list[tuple[int, int, int]] = []

    def find(node: int) -> int:
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for y, row in enumerate(binary):
        padded = np.pad(row, (1, 1), constant_values=False)
        transitions = np.flatnonzero(padded[1:] != padded[:-1])
        current: list[tuple[int, int, int]] = []
        for start, end in zip(transitions[::2], transitions[1::2], strict=True):
            node = len(parents)
            parents.append(node)
            run = (y, int(start), int(end))
            runs.append(run)
            for previous_start, previous_end, previous_node in previous:
                # end 为 exclusive；边界相等时两行像素仍为对角相邻。
                if previous_start <= end and previous_end >= start:
                    union(node, previous_node)
            current.append((int(start), int(end), node))
        previous = current

    grouped: dict[int, list[tuple[int, int, int]]] = {}
    for node, run in enumerate(runs):
        grouped.setdefault(find(node), []).append(run)

    components: list[_MaskComponent] = []
    for component_runs in grouped.values():
        left = min(start for _y, start, _end in component_runs)
        right = max(end for _y, _start, end in component_runs)
        top = min(y for y, _start, _end in component_runs)
        bottom = max(y for y, _start, _end in component_runs) + 1
        area = sum(end - start for _y, start, end in component_runs)
        components.append(_MaskComponent(left, top, right, bottom, area, component_runs))
    return binary, components


def _component_image(binary: np.ndarray, components: Sequence[_MaskComponent]) -> Image.Image:
    output = np.zeros(binary.shape, dtype=np.uint8)
    for component in components:
        for y, start, end in component.runs:
            output[y, start:end] = 255
    return Image.fromarray(output)


def _largest_mask_component(mask: Image.Image) -> Image.Image:
    """保留主连通组件，削弱卡框、粒子和小装饰对图标轮廓的影响。"""

    binary, components = _mask_components(mask)
    largest = max(components, key=lambda component: component.area, default=None)
    return _component_image(binary, [largest] if largest is not None else [])


def _icon_fingerprints(image: Image.Image, *, template: bool) -> tuple[tuple[float, ...], ...]:
    """生成完整 glyph、主组件与轮廓三种去重指纹。"""

    full = _adaptive_icon_mask(image, template=template)
    foreground_ratio = float(np.mean(np.asarray(full, dtype=np.uint8) >= 128)) if full.size[0] and full.size[1] else 0.0
    if foreground_ratio <= 0.002 or foreground_ratio >= 0.98:
        return ()
    compact = _fit_mask_to_canvas(full, (72, 72)) or full
    main = _largest_mask_component(compact)
    contour = main.filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= 24 else 0)
    fingerprints: list[tuple[float, ...]] = []
    for mask in (full, main, contour):
        fingerprint = _normalized_fingerprint(_mask_levels(mask, FINGERPRINT_SIZE))
        if fingerprint is not None and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return tuple(fingerprints)


def _icon_mask_digest(image: Image.Image, *, template: bool) -> str:
    """共享图标按规范化主组件 mask 分组，不按源 PNG 文件字节分组。"""

    full = _adaptive_icon_mask(image, template=template)
    compact = _fit_mask_to_canvas(full, (72, 72)) or full
    fitted = _fit_mask_to_canvas(_largest_mask_component(compact), FINGERPRINT_SIZE)
    if fitted is None:
        return ""
    normalized = fitted.point(lambda value: 255 if value >= 64 else 0)
    return hashlib.sha256(bytes(normalized.getdata())).hexdigest()[:20]


def _name_text_mask(image: Image.Image) -> Image.Image:
    """名字区专用 mask：去掉卡框和星光等装饰，只保留标题主字形。"""

    mask = _bright_glyph_mask(image)
    width, height = mask.size
    binary, raw_components = _mask_components(mask)
    components: list[_MaskComponent] = []
    for component in raw_components:
        touches_edge = component.left <= 2 or component.right >= width - 2
        if component.area < 8 or component.height < 4:
            continue
        if component.width <= TEXT_DECORATION_MAX_WIDTH and component.height >= height * TEXT_DECORATION_MIN_HEIGHT_RATIO:
            continue
        if touches_edge and (component.height >= height * 0.45 or component.width <= TEXT_DECORATION_MAX_WIDTH + 1):
            continue
        components.append(component)

    if not components:
        return mask

    # 真机卡名两侧会出现星光粒子。它们能通过固定像素阈值，却明显小于同一行主字形；
    # 相对当前裁剪中的最大字形过滤，既不依赖分辨率，也不会误删正常短名称。
    dominant_height = max(component.height for component in components)
    dominant_area = max(component.area for component in components)
    glyph_components = [
        component
        for component in components
        if component.height >= dominant_height * 0.72 and component.area >= dominant_area * 0.25
    ]
    if not glyph_components:
        glyph_components = components
    return _component_image(binary, glyph_components)


def _mask_levels(mask: Image.Image, size: tuple[int, int]) -> list[int]:
    fitted = _fit_mask_to_canvas(mask, size)
    if fitted is None:
        return []
    edges = fitted.filter(ImageFilter.FIND_EDGES)
    return list(fitted.getdata()) + list(edges.getdata())


def _text_mask_levels(mask: Image.Image, size: tuple[int, int]) -> list[int]:
    """文字主体优先的指纹输入；边缘仅作辅助，避免抗锯齿和装饰噪声反客为主。"""

    fitted = _fit_mask_to_canvas(mask, size)
    if fitted is None:
        return []
    fill = list(fitted.getdata())
    edges = list(fitted.filter(ImageFilter.FIND_EDGES).getdata())
    return fill * 3 + edges


def _icon_levels(image: Image.Image, *, template: bool) -> list[int]:
    mask = _adaptive_icon_mask(image, template=template)
    return _mask_levels(mask, FINGERPRINT_SIZE)


def _text_levels(image: Image.Image) -> list[int]:
    return _text_mask_levels(_name_text_mask(image), NAME_FINGERPRINT_SIZE)


def _grayscale_levels(image: Image.Image) -> list[int]:
    """兼容旧调用名：现在返回图标字形轮廓指纹输入，而非整块灰度。"""

    return _icon_levels(image, template=False)


def _levels_std(levels: Sequence[int]) -> float:
    if not levels:
        return 0.0
    return float(np.asarray(levels, dtype=np.float32).std())


def _normalized_fingerprint(levels: Sequence[int]) -> tuple[float, ...] | None:
    """零均值/单位方差归一化指纹；平坦图像（纯色、暗面板）没有有效指纹。"""

    if not levels:
        return None
    values = np.asarray(levels, dtype=np.float32)
    mean = float(values.mean())
    std = float(values.std())
    if std < 1e-6:
        return None
    return tuple(((values - mean) / std).tolist())


def _fingerprint(image: Image.Image, *, template: bool = False) -> tuple[float, ...] | None:
    return _normalized_fingerprint(_icon_levels(image, template=template))


def _fingerprint_confidence(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """归一化互相关（NCC）映射到 [0,1]；只对形状敏感，不受亮度/色调影响。"""

    if not left or not right or len(left) != len(right):
        return 0.0
    correlation = sum(a * b for a, b in zip(left, right)) / len(left)
    return max(0.0, min(1.0, (correlation + 1.0) / 2.0))


def _load_cjk_font(size: int, *, family: str = "primary") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    primary_paths = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    )
    alt_paths = (
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    legacy_paths = (
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    font_paths = alt_paths if family == "alt" else (legacy_paths if family == "legacy" else primary_paths)
    for font_path in font_paths:
        try:
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_name_mask(name: str, *, family: str = "primary") -> Image.Image | None:
    clean_name = _clean_text(name)
    if not clean_name:
        return None
    canvas = Image.new("L", NAME_FINGERPRINT_SIZE, 0)
    draw = ImageDraw.Draw(canvas)
    font_size = 34
    font = _load_cjk_font(font_size, family=family)
    max_width = int(NAME_FINGERPRINT_SIZE[0] * 0.94)
    while font_size >= 16:
        bbox = draw.textbbox((0, 0), clean_name, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_width:
            break
        font_size -= 2
        font = _load_cjk_font(font_size, family=family)
    bbox = draw.textbbox((0, 0), clean_name, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text(
        ((NAME_FINGERPRINT_SIZE[0] - text_width) // 2 - bbox[0], (NAME_FINGERPRINT_SIZE[1] - text_height) // 2 - bbox[1]),
        clean_name,
        fill=255,
        font=font,
    )
    return canvas.point(lambda value: 255 if value >= 32 else 0).filter(ImageFilter.MaxFilter(3))


def render_name_mask(name: str, *, family: str = "primary") -> Image.Image | None:
    """供离线刷新/评测工具复用的名称模板渲染入口。"""

    return _render_name_mask(name, family=family)


def _name_fingerprint(name: str, *, family: str = "primary") -> tuple[float, ...] | None:
    mask = _render_name_mask(name, family=family)
    if mask is None:
        return None
    return _normalized_fingerprint(_text_mask_levels(mask, NAME_FINGERPRINT_SIZE))


def _cleaned_name_fingerprint(name: str, *, family: str = "primary") -> tuple[float, ...] | None:
    mask = _render_name_mask(name, family=family)
    if mask is None:
        return None
    # 截图路径会先经过 _name_text_mask 清理装饰组件；额外保留同路径模板指纹，
    # 让长名称、数字和标点在全量合成回归里不会被相似中文名压过。
    cleaned = _name_text_mask(Image.merge("RGB", (mask, mask, mask)))
    return _normalized_fingerprint(_text_mask_levels(cleaned, NAME_FINGERPRINT_SIZE))


@lru_cache(maxsize=1)
def _body_shard_suffix_fingerprints() -> tuple[tuple[float, ...], ...]:
    fingerprints: list[tuple[float, ...]] = []
    # 碎片后缀门保留旧 SimHei 指纹，避免主名称通道切换字体后削弱既有硬阻断。
    for family in ("primary", "alt", "legacy"):
        mask = _render_name_mask(BODY_SHARD_SUFFIX, family=family)
        if mask is None:
            continue
        fingerprint = _normalized_fingerprint(_mask_levels(mask, BODY_SHARD_SUFFIX_SIZE))
        if fingerprint is not None and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return tuple(fingerprints)


@lru_cache(maxsize=1)
def _body_shard_suffix_matrix() -> np.ndarray:
    fingerprints = _body_shard_suffix_fingerprints()
    if not fingerprints:
        return np.empty((0, 0), dtype=np.float32)
    return np.asarray(fingerprints, dtype=np.float32)


def _rightmost_two_glyphs(mask: Image.Image) -> Image.Image | None:
    """从清理后的名称 mask 提取最右两个有效字形。

    动画/淡出常把整段文字粘成 48px 高的大连通块，或只剩一个残字；两者都不
    能作为“碎片”后缀证据。阈值相对 ROI 高度计算，不依赖固定分辨率。
    """

    width, height = mask.size
    components: list[tuple[int, int, int, int, int]] = []
    _binary, raw_components = _mask_components(mask)
    for component in raw_components:
        minimum_area = max(16, int(component.height * component.height * 0.12))
        if (
            component.area >= minimum_area
            and int(height * 0.45) <= component.height <= int(height * 0.82)
            and int(component.height * 0.45) <= component.width <= int(component.height * 1.25)
        ):
            components.append(
                (component.left, component.top, component.right, component.bottom, component.area)
            )

    if len(components) < 2:
        return None
    components.sort(key=lambda item: (item[0], item[1]))
    first, second = components[-2:]
    first_height = first[3] - first[1]
    second_height = second[3] - second[1]
    gap = second[0] - first[2]
    if gap > max(8, int(max(first_height, second_height) * 0.35)):
        return None
    if min(first_height, second_height) / max(first_height, second_height) < 0.72:
        return None
    return mask.crop(
        (
            first[0],
            min(first[1], second[1]),
            second[2],
            max(first[3], second[3]),
        )
    )


def _body_shard_name_scores(
    name_crops: Sequence[Image.Image],
    *,
    name_masks: Sequence[Image.Image] | None = None,
) -> tuple[float, ...]:
    """匹配名称右侧“碎片”后缀；只用于选择场景类型判定。"""

    template_matrix = _body_shard_suffix_matrix()
    scores: list[float] = []
    masks = list(name_masks or [])
    for index, crop in enumerate(list(name_crops)[:SLOT_COUNT]):
        mask = masks[index] if index < len(masks) else _name_text_mask(crop)
        suffix = _rightmost_two_glyphs(mask)
        if suffix is None or template_matrix.size == 0:
            scores.append(0.0)
            continue
        fingerprint = _normalized_fingerprint(_mask_levels(suffix, BODY_SHARD_SUFFIX_SIZE))
        if fingerprint is None:
            scores.append(0.0)
            continue
        candidate = np.asarray(fingerprint, dtype=np.float32)
        correlations = template_matrix @ candidate / candidate.shape[0]
        best = float(np.clip((correlations.max() + 1.0) / 2.0, 0.0, 1.0))
        scores.append(round(best, 6))
    while len(scores) < SLOT_COUNT:
        scores.append(0.0)
    return tuple(scores)


def _body_shard_scene_present(scores: Sequence[float]) -> bool:
    ranked = sorted((float(score) for score in list(scores)[:SLOT_COUNT]), reverse=True)
    if len(ranked) < 2:
        return False
    return bool(
        sum(score >= BODY_SHARD_STRONG_CONFIDENCE for score in ranked) >= 2
        or (
            ranked[0] >= BODY_SHARD_VERY_STRONG_CONFIDENCE
            and ranked[1] >= BODY_SHARD_SUPPORT_CONFIDENCE
        )
    )


def _name_crop_has_residue(crop: Image.Image, *, name_mask: Image.Image | None = None) -> bool:
    mask_array = np.asarray(name_mask if name_mask is not None else _name_text_mask(crop), dtype=np.uint8) >= 128
    if mask_array.size == 0:
        return False
    foreground_ratio = float(np.mean(mask_array))
    return 0.005 <= foreground_ratio <= 0.45


def _load_manifest_entries(root: Path, *, use_runtime_resources: bool = True) -> list[Mapping[str, Any]]:
    try:
        if use_runtime_resources:
            payload = load_augment_manifest_entries()
        else:
            payload = load_augment_manifest_entries(root / "resources" / "catalog")
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, Mapping)]


def _load_manifest_entries_by_name(root: Path, *, use_runtime_resources: bool = True) -> dict[str, list[Mapping[str, Any]]]:
    payload = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    result: dict[str, list[Mapping[str, Any]]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("name"))
        if not name:
            continue
        result.setdefault(name, []).append(item)
        result.setdefault(normalize_augment_id(name), []).append(item)
    return result


def _select_manifest_item(
    manifest_by_name: Mapping[str, Sequence[Mapping[str, Any]]],
    name: str,
    relative_icon: str,
) -> Mapping[str, Any]:
    entries = (
        manifest_by_name.get(name)
        or manifest_by_name.get(normalize_augment_id(name))
        or ()
    )
    if not entries:
        return {}

    # CDragon 有少数同中文名但不同玩法/图标的条目。模板图标来自 name-to-icon，
    # 因此元数据也要优先选择同 filename 的 manifest 项，避免图标正确但 tier/id 串到同名旧项。
    requested_filename = Path(str(relative_icon or "").replace("\\", "/")).name.lower()
    for item in entries:
        filename = _clean_text(item.get("filename")).lower()
        local_path = Path(_clean_text(item.get("local_path")).replace("\\", "/")).name.lower()
        if requested_filename and requested_filename in {filename, local_path}:
            return item
    return entries[0]


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    """从内存模板构建匹配索引。

    去重策略：同一 normalize_augment_id 只保留一个身份，同名图标作为指纹变体聚合。
    无图标指纹时仍保留文字模板，避免透明或低对比度官方图标让整个海克斯消失。
    双字体指纹：SimHei（主字体）和 SimSun（备选字体）各生成一份 name_fingerprint，
    匹配时双通道独立评分再综合判定。
    """

    variant_counts: dict[str, int] = {}
    for payload in raw_templates.values():
        if not isinstance(payload, Mapping):
            continue
        name_key = normalize_augment_id(str(payload.get("name") or ""))
        if name_key:
            variant_counts[name_key] = variant_counts.get(name_key, 0) + 1

    index: list[TemplateEntry] = []
    for augment_id, payload in raw_templates.items():
        if not isinstance(payload, Mapping):
            continue
        normalized_id = normalize_augment_id(augment_id, str(payload.get("name") or ""))
        if not normalized_id:
            continue
        name = _clean_text(payload.get("name"), fallback=normalized_id)
        raw_images = payload.get("images")
        images = [item for item in raw_images if isinstance(item, Image.Image)] if isinstance(raw_images, Sequence) else []
        if not images and isinstance(payload.get("image"), Image.Image):
            images = [payload["image"]]
        filenames_value = payload.get("source_icon_filenames")
        filenames = [
            _clean_text(item)
            for item in filenames_value
            if _clean_text(item)
        ] if isinstance(filenames_value, Sequence) and not isinstance(filenames_value, (str, bytes)) else []

        icon_fingerprints_list: list[np.ndarray] = []
        icon_fingerprint_digests: set[bytes] = set()
        text_only_filenames: list[str] = []
        icon_digest = ""
        for image_index, image in enumerate(images):
            image_fingerprints = _icon_fingerprints(image, template=True)
            if image_fingerprints:
                for fingerprint in image_fingerprints:
                    row = np.ascontiguousarray(np.asarray(fingerprint, dtype=np.float16))
                    digest = row.tobytes()
                    if digest not in icon_fingerprint_digests:
                        icon_fingerprint_digests.add(digest)
                        icon_fingerprints_list.append(row)
                if not icon_digest:
                    icon_digest = _icon_mask_digest(image, template=True)
            elif image_index < len(filenames):
                text_only_filenames.append(filenames[image_index])
        icon_fingerprints = tuple(icon_fingerprints_list)
        name_fingerprint = _name_fingerprint(name)
        name_fingerprint_alt = _name_fingerprint(name, family="alt")
        if not icon_fingerprints and name_fingerprint is None and name_fingerprint_alt is None:
            continue
        index.append(
            TemplateEntry(
                augment_id=normalized_id,
                name=name,
                tier=_clean_text(payload.get("tier"), fallback="Unknown"),
                summary=_clean_text(payload.get("summary"), fallback="本地模板识别结果"),
                fingerprint=icon_fingerprints[0] if icon_fingerprints else None,
                icon_fingerprints=icon_fingerprints,
                icon_digest=icon_digest,
                priority=1 if bool(payload.get("priority")) else 0,
                name_fingerprint=name_fingerprint,
                name_fingerprint_alt=name_fingerprint_alt,
                source_icon_filenames=tuple(filenames),
                text_only_icon_filenames=tuple(text_only_filenames),
                name_variant_count=max(1, variant_counts.get(normalize_augment_id(name), 1)),
            )
        )
    return index


def audit_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """审计稳定资源是否为每个名称和图标变体建立了可识别模板。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_data_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "resources" / "catalog"
    entries = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    try:
        name_to_icon = load_augment_name_to_icon_map(version_data_dir)
    except (OSError, json.JSONDecodeError):
        name_to_icon = {}
    expected_identities = {
        normalize_augment_id(item.get("name"))
        for item in entries
        if normalize_augment_id(item.get("name"))
    } | {
        normalize_augment_id(name)
        for name in name_to_icon
        if normalize_augment_id(name)
    }
    expected_variants = {
        (normalize_augment_id(item.get("name")), Path(_clean_text(item.get("filename") or item.get("local_path"))).name.lower())
        for item in entries
        if normalize_augment_id(item.get("name")) and _clean_text(item.get("filename") or item.get("local_path"))
    } | {
        (normalize_augment_id(name), Path(str(icon_path).replace("\\", "/")).name.lower())
        for name, icon_path in name_to_icon.items()
        if normalize_augment_id(name) and _clean_text(icon_path)
    }
    template_index = _template_runtime_module.load_default_template_index(base_dir, hint_cache=hint_cache)
    actual_identities = {normalize_augment_id(entry.name) for entry in template_index}
    actual_variants = {
        (normalize_augment_id(entry.name), filename.lower())
        for entry in template_index
        for filename in entry.source_icon_filenames
    }
    missing_identities = sorted(expected_identities - actual_identities)
    missing_variants = sorted(expected_variants - actual_variants)
    return {
        "manifest_count": len(entries),
        "identity_count": len(expected_identities),
        "variant_count": len(expected_variants),
        "template_count": len(template_index),
        "text_only_template_count": sum(1 for entry in template_index if not entry.icon_variant_count),
        "text_only_variant_count": sum(len(entry.text_only_icon_filenames) for entry in template_index),
        "missing_identity_count": len(missing_identities),
        "missing_variant_count": len(missing_variants),
        "missing_identity_sample": missing_identities[:20],
        "missing_variant_sample": [list(item) for item in missing_variants[:20]],
    }


# 单进程通常只有一份长驻 template_index；缓存按 id 命中，限容防 eval/测试反复建表泄漏。
_RANK_MATRIX_CACHE: "dict[int, _RankMatrices]" = {}
_RANK_MATRIX_CACHE_MAX = 4



__all__ = [name for name in globals() if not name.startswith("__")]
