"""MSS 局部捕获保持 client 坐标并拒绝不完整像素证据。"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass
class _Shot:
    size: tuple[int, int]
    bgra: bytes


class _FakeMss:
    def __init__(
        self,
        *,
        wrong_size: bool = False,
        bgra: tuple[int, int, int, int] = (255, 255, 255, 255),
    ) -> None:
        self.closed = False
        self.grabs: list[dict[str, int]] = []
        self.wrong_size = wrong_size
        self.bgra = bytes(bgra)

    def grab(self, monitor: dict[str, int]) -> _Shot:
        self.grabs.append(dict(monitor))
        width = int(monitor["width"])
        height = int(monitor["height"])
        size = (1, 1) if self.wrong_size else (width, height)
        return _Shot(size=size, bgra=self.bgra * (size[0] * size[1]))

    def close(self) -> None:
        self.closed = True


def test_required_capture_bounds_cover_all_detection_consumers_and_transform_envelope() -> None:
    from hextech.infrastructure.vision.capture_geometry import required_capture_bounds

    width, height = 2560, 1440
    left, top, right, bottom = required_capture_bounds((width, height))

    # 卡面允许的 -4.5%/+4.5% 平移和 1.10 倍缩放比旧固定 19%-81% 更宽，
    # 同时必须覆盖约 76.5%-85.5% 的选择按钮扫描区。
    assert left <= round(width * 0.144)
    assert top <= round(height * 0.091)
    assert right >= round(width * 0.865)
    assert bottom >= round(height * 0.855)
    assert (left, top, right, bottom) == required_capture_bounds((width, height), "2560x1440")


def test_mss_roi_capture_preserves_client_size_origin_and_negative_screen_coordinates() -> None:
    from hextech.infrastructure.vision import sidecar_capture
    from hextech.infrastructure.vision.capture_geometry import required_capture_bounds

    fake = _FakeMss(bgra=(30, 20, 10, 255))
    backend = sidecar_capture._MssCaptureBackend(factory=lambda: fake)
    rect = (-2560, -200, 0, 1240)
    expected_origin = required_capture_bounds((2560, 1440))

    frame = sidecar_capture._capture_lol_game_rect(rect, backend=backend)

    assert frame is not None
    assert frame.mode == "RGB"
    assert frame.size == (2560, 1440)
    assert frame.info["hextech_capture_mode"] == "roi_union"
    assert frame.info["hextech_roi_origin"] == expected_origin[:2]
    assert frame.info["hextech_roi_size"] == (
        expected_origin[2] - expected_origin[0],
        expected_origin[3] - expected_origin[1],
    )
    assert frame.info["hextech_client_size"] == (2560, 1440)
    assert fake.grabs == [
        {
            "left": rect[0] + expected_origin[0],
            "top": rect[1] + expected_origin[1],
            "width": expected_origin[2] - expected_origin[0],
            "height": expected_origin[3] - expected_origin[1],
        }
    ]
    assert frame.getpixel(expected_origin[:2]) == (10, 20, 30)
    assert frame.getpixel((0, 0)) == (0, 0, 0)


def test_capture_backend_reuses_one_mss_session_and_close_is_idempotent() -> None:
    from hextech.infrastructure.vision import sidecar_capture

    sessions: list[_FakeMss] = []

    def factory() -> _FakeMss:
        session = _FakeMss()
        sessions.append(session)
        return session

    backend = sidecar_capture._MssCaptureBackend(factory=factory)
    assert sidecar_capture._capture_lol_game_rect((0, 0, 1920, 1080), backend=backend) is not None
    assert sidecar_capture._capture_lol_game_rect((10, 20, 1930, 1100), backend=backend) is not None
    assert len(sessions) == 1
    assert len(sessions[0].grabs) == 2

    backend.close()
    backend.close()
    assert sessions[0].closed is True

    assert sidecar_capture._capture_lol_game_rect((0, 0, 1920, 1080), backend=backend) is not None
    assert len(sessions) == 2


def test_close_capture_backend_releases_and_clears_default(monkeypatch) -> None:
    from hextech.infrastructure.vision import sidecar_capture

    backend = sidecar_capture._MssCaptureBackend(factory=_FakeMss)
    assert backend.capture_rgb((0, 0, 8, 8)) is not None
    session = backend._session
    monkeypatch.setattr(sidecar_capture, "_DEFAULT_CAPTURE_BACKEND", backend)

    sidecar_capture.close_capture_backend()
    sidecar_capture.close_capture_backend()

    assert session.closed is True
    assert sidecar_capture._DEFAULT_CAPTURE_BACKEND is None


def test_bad_mss_frame_stops_current_tick_without_desktop_or_fullscreen_fallback() -> None:
    from hextech.infrastructure.vision import sidecar_capture

    fake = _FakeMss(wrong_size=True)
    backend = sidecar_capture._MssCaptureBackend(factory=lambda: fake)
    rect = (0, 0, 1920, 1080)

    assert sidecar_capture._capture_lol_game_rect(rect, backend=backend) is None
    assert sidecar_capture._capture_lol_game_rect(rect, backend=backend) is None
    assert len(fake.grabs) == 2
    assert all(call != {"left": 0, "top": 0, "width": 1920, "height": 1080} for call in fake.grabs)


def test_unknown_preset_uses_same_backend_for_full_game_client() -> None:
    from hextech.infrastructure.vision import sidecar_capture

    fake = _FakeMss()
    backend = sidecar_capture._MssCaptureBackend(factory=lambda: fake)

    frame = sidecar_capture._capture_lol_game_rect(
        (-100, 40, 900, 840),
        preset_name="future-layout",
        backend=backend,
    )

    assert frame is not None
    assert frame.info["hextech_capture_mode"] == "client_full"
    assert frame.info["hextech_roi_origin"] == (0, 0)
    assert frame.info["hextech_roi_size"] == (1000, 800)
    assert fake.grabs == [{"left": -100, "top": 40, "width": 1000, "height": 800}]


def test_capture_regions_valid_allows_plain_offline_frame_but_rejects_partial_metadata() -> None:
    from hextech.infrastructure.vision.capture_geometry import capture_regions_valid

    plain = Image.new("RGB", (100, 80))
    assert capture_regions_valid(plain, [(0, 0, 100, 80)])
    assert not capture_regions_valid(plain, [(-1, 0, 20, 20)])

    partial = plain.copy()
    partial.info["hextech_roi_origin"] = (0, 0)
    assert not capture_regions_valid(partial, [(0, 0, 20, 20)])


def test_capture_regions_valid_uses_actual_pixels_not_black_padding() -> None:
    from hextech.infrastructure.vision.capture_geometry import capture_regions_valid

    frame = Image.new("RGB", (100, 80), "black")
    frame.info.update(
        {
            "hextech_capture_mode": "roi_union",
            "hextech_roi_origin": (20, 10),
            "hextech_roi_size": (60, 50),
            "hextech_client_size": (100, 80),
        }
    )

    assert capture_regions_valid(frame, [(20, 10, 80, 60), (25, 15, 40, 30)])
    assert not capture_regions_valid(frame, [(19, 10, 80, 60)])
    assert not capture_regions_valid(frame, [(20, 10, 81, 60)])
    assert not capture_regions_valid(frame, [(30, 30, 30, 40)])
