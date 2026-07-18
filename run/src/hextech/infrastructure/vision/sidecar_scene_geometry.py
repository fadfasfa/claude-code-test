"""Vision sidecar scene_geometry 职责模块。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import *

def resolve_roi_preset(width: int, height: int, *, preset: str = "auto") -> RoiPreset:
    """按捕获尺寸选择 ROI 预设；16:10 2K 优先落到 2560x1600。"""

    requested = str(preset or "auto").strip().lower()
    if requested != "auto":
        normalized = requested.replace("*", "x")
        if normalized in ROI_PRESETS:
            return ROI_PRESETS[normalized]
        raise ValueError(f"未知 overlay ROI preset: {preset}")

    if width <= 0 or height <= 0:
        return ROI_PRESETS["2560x1600"]
    aspect = width / max(1, height)
    if abs(aspect - 1.6) <= 0.035:
        return ROI_PRESETS["2560x1600"]

    target = (int(width), int(height))
    if target in {preset.base_size for preset in ROI_PRESETS.values()}:
        for roi_preset in ROI_PRESETS.values():
            if roi_preset.base_size == target:
                return roi_preset
    return ROI_PRESETS["2560x1440"] if width >= 2300 else ROI_PRESETS["1920x1080"]


def _clip_box(box: tuple[int, int, int, int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = box
    return (
        max(0, min(width - 1, int(left))),
        max(0, min(height - 1, int(top))),
        max(1, min(width, int(right))),
        max(1, min(height, int(bottom))),
    )


def _is_selection_button_pixel(pixel: tuple[int, int, int] | tuple[int, int, int, int]) -> bool:
    """识别海克斯选择界面底部偏蓝按钮像素；只用于本地截图门控。"""

    red, green, blue = int(pixel[0]), int(pixel[1]), int(pixel[2])
    return (
        blue >= 100
        and green >= 70
        and red <= 110
        and (blue - red) >= 45
        and (green - red) >= 15
        and blue >= green - 20
    )


def _selection_button_blue_ratio(image: Image.Image) -> tuple[int, float]:
    rgb = image.convert("RGB")
    pixels = list(rgb.getdata())
    if not pixels:
        return 0, 0.0
    count = sum(1 for pixel in pixels if _is_selection_button_pixel(pixel))
    return count, count / len(pixels)


def selection_button_present(image: Image.Image) -> bool:
    """固定按钮 ROI 内仍需每轮确认按钮存在，避免无关页面误显示。"""

    count, ratio = _selection_button_blue_ratio(image)
    return count >= BUTTON_MIN_BLUE_PIXELS and ratio >= BUTTON_MIN_BLUE_RATIO


def _selection_button_box_geometry_valid(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> bool:
    """统一校验扫描结果和缓存按钮框，拒绝载入进度条等过低横条。"""

    width, height = image_size
    left, top, right, bottom = box
    box_width = max(0, right - left)
    box_height = max(0, bottom - top)
    if min(width, height, box_width, box_height) <= 0:
        return False
    center_x_ratio = ((left + right) / 2.0) / width
    center_y_ratio = ((top + bottom) / 2.0) / height
    return (
        0.05 <= box_width / width <= 0.32
        and 0.01 <= box_height / height <= 0.12
        and BUTTON_CENTER_MIN_RATIO <= center_x_ratio <= BUTTON_CENTER_MAX_RATIO
        and BUTTON_CENTER_MIN_Y_RATIO <= center_y_ratio <= BUTTON_CENTER_MAX_Y_RATIO
    )


def _selection_button_box_valid(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    return _selection_button_box_geometry_valid(box, image.size) and selection_button_present(image.crop(box))


def _selection_button_source_fields(
    *,
    present: bool,
    window_active: bool | None = None,
    button_box: tuple[int, int, int, int] | None = None,
    blue_ratio: float = 0.0,
) -> dict[str, Any]:
    """把按钮检测结果稳定写入事件 source，供 host 判断选择窗口生命周期。"""

    return {
        "selection_button_present": bool(present),
        "selection_window_active": bool(present if window_active is None else window_active),
        "button_blue_ratio": round(max(0.0, float(blue_ratio)), 6),
        "button_box": list(button_box) if button_box is not None else [],
    }


def detect_selection_button_box(frame: Image.Image) -> tuple[int, int, int, int] | None:
    """首次校准时在宽搜索区内定位蓝色按钮，后续只复用固定 ROI。

    级联策略：
    1. 下采样扫描蓝色像素 → 提取水平游程
    2. 游程按 Y 邻近度分组为连通域
    3. 连通域按紧实度 + 中心位置校验 → 选出最佳候选按钮框
    """

    image = frame.convert("RGB")
    width, height = image.size
    scan_width = max(1, width // BUTTON_SCAN_DOWNSAMPLE)
    scan_height = max(1, height // BUTTON_SCAN_DOWNSAMPLE)
    resampling_nearest = getattr(Image, "Resampling", Image).NEAREST
    scan_image = image.resize((scan_width, scan_height), resampling_nearest)
    scale_x = width / scan_width
    scale_y = height / scan_height

    left_r, top_r, right_r, bottom_r = BUTTON_SEARCH_REGION
    search_box = _clip_box(
        (
            int(round(left_r * scan_width)),
            int(round(top_r * scan_height)),
            int(round(right_r * scan_width)),
            int(round(bottom_r * scan_height)),
        ),
        scan_image.size,
    )
    left, top, right, bottom = search_box
    min_run_width = max(8, int(round(scan_width * 0.06)))
    pixels = scan_image.load()

    row_runs: list[tuple[int, int, int]] = []
    for y in range(top, bottom):
        run_start: int | None = None
        for x in range(left, right):
            if _is_selection_button_pixel(pixels[x, y]):
                if run_start is None:
                    run_start = x
            elif run_start is not None:
                if x - run_start >= min_run_width:
                    row_runs.append((y, run_start, x - 1))
                run_start = None
        if run_start is not None and right - run_start >= min_run_width:
            row_runs.append((y, run_start, right - 1))

    if not row_runs:
        return None

    groups: list[dict[str, int]] = []
    for y, run_left, run_right in row_runs:
        target: dict[str, int] | None = None
        run_center = (run_left + run_right) // 2
        for group in reversed(groups):
            if y - group["max_y"] > 2:
                continue
            group_center = (group["min_x"] + group["max_x"]) // 2
            overlap = min(run_right, group["max_x"]) - max(run_left, group["min_x"])
            if overlap >= min_run_width // 3 or abs(run_center - group_center) <= int(scan_width * 0.035):
                target = group
                break
        if target is None:
            groups.append(
                {
                    "min_x": run_left,
                    "max_x": run_right,
                    "min_y": y,
                    "max_y": y,
                    "rows": 1,
                    "run_pixels": run_right - run_left + 1,
                }
            )
            continue
        target["min_x"] = min(target["min_x"], run_left)
        target["max_x"] = max(target["max_x"], run_right)
        target["min_y"] = min(target["min_y"], y)
        target["max_y"] = max(target["max_y"], y)
        target["rows"] += 1
        target["run_pixels"] += run_right - run_left + 1

    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for group in groups:
        box_width = group["max_x"] - group["min_x"] + 1
        box_height = group["max_y"] - group["min_y"] + 1
        center_x = (group["min_x"] + group["max_x"]) / 2.0
        center_y = (group["min_y"] + group["max_y"]) / 2.0
        if not (0.06 * scan_width <= box_width <= 0.30 * scan_width):
            continue
        if not (0.012 * scan_height <= box_height <= 0.10 * scan_height):
            continue
        if not (BUTTON_CENTER_MIN_RATIO * scan_width <= center_x <= BUTTON_CENTER_MAX_RATIO * scan_width):
            continue
        if not (BUTTON_CENTER_MIN_Y_RATIO * scan_height <= center_y <= BUTTON_CENTER_MAX_Y_RATIO * scan_height):
            continue
        if group["rows"] < max(3, int(round(scan_height * 0.006))):
            continue
        solidity = group["run_pixels"] / max(1, box_width * box_height)
        if solidity < BUTTON_MIN_SOLIDITY:
            continue
        # 候选越居中、越靠下、连续蓝段面积越大越像底部选择按钮。
        centered_penalty = abs((center_x / scan_width) - 0.5)
        lower_bonus = center_y / scan_height
        area_bonus = min(1.0, group["run_pixels"] / max(1.0, scan_width * scan_height * 0.01))
        score = area_bonus + lower_bonus - centered_penalty
        candidates.append((score, (group["min_x"], group["min_y"], group["max_x"] + 1, group["max_y"] + 1)))

    if not candidates:
        return None

    _, best_box = max(candidates, key=lambda item: item[0])
    pad_x = max(4, int(round(width * 0.008)))
    pad_y = max(3, int(round(height * 0.008)))
    best_left, best_top, best_right, best_bottom = best_box
    original_box = (
        int(round(best_left * scale_x)),
        int(round(best_top * scale_y)),
        int(round(best_right * scale_x)),
        int(round(best_bottom * scale_y)),
    )
    candidate = _clip_box(
        (
            original_box[0] - pad_x,
            original_box[1] - pad_y,
            original_box[2] + pad_x,
            original_box[3] + pad_y,
        ),
        image.size,
    )
    return candidate if _selection_button_box_valid(image, candidate) else None



__all__ = [name for name in globals() if not name.startswith("__")]
