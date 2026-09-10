from __future__ import annotations

from hextech.infrastructure.vision.mouse_transition import MouseTransitionObserver
from hextech.modules.vision.layout import (
    LayoutTransform,
    apply_transform,
    pick_card_panels,
    pick_slot_interaction_boxes,
)


def test_mouse_down_between_vision_frames_maps_to_exactly_one_slot() -> None:
    button = {"down": False}
    clock = {"now": 10.0}
    client_rect = (100, 200, 1100, 800)
    frame_size = (1000, 600)
    second = apply_transform(pick_card_panels(frame_size)[1], frame_size, LayoutTransform())
    cursor = {
        "value": (
            client_rect[0] + (second[0] + second[2]) // 2,
            client_rect[1] + (second[1] + second[3]) // 2,
        )
    }
    observer = MouseTransitionObserver(
        button_probe=lambda: button["down"],
        cursor_probe=lambda: cursor["value"],
        clock=lambda: clock["now"],
    )
    observer.update_context(
        window_hwnd=321,
        game_instance_id="game-1",
        selection_epoch=4,
        eligible=True,
    )

    observer.poll_once()
    button["down"] = True
    clock["now"] = 10.05
    observer.poll_once()
    button["down"] = False
    observer.poll_once()
    # 下一次 Vision 帧晚 260ms 才到；短点击已经结束，事件仍可精确消费。
    clock["now"] = 10.31
    event = observer.consume_slot_event(
        client_rect=client_rect,
        frame_size=frame_size,
        source={"layout_transform": {}},
        window_hwnd=321,
        game_instance_id="game-1",
        selection_epoch=4,
    )

    assert event == {
        "mouse_event_sequence": 1,
        "mouse_event_observed_at": 10.05,
        "transition_source": "async_mouse_down",
        "transition_kind": "card",
        "transition_slot": 1,
    }
    assert observer.consume_slot_event(
        client_rect=client_rect,
        frame_size=frame_size,
        source={"layout_transform": {}},
        window_hwnd=321,
        game_instance_id="game-1",
        selection_epoch=4,
    ) is None


def test_reroll_buttons_below_cards_map_to_each_slot_on_real_16_10_layout() -> None:
    frame_size = (2560, 1600)
    client_rect = (0, 0, *frame_size)
    action_boxes = pick_slot_interaction_boxes(frame_size)

    for expected_slot, (_panel, reroll) in enumerate(action_boxes):
        button = {"down": False}
        reroll_box = apply_transform(reroll, frame_size, LayoutTransform())
        cursor = ((reroll_box[0] + reroll_box[2]) // 2, (reroll_box[1] + reroll_box[3]) // 2)
        observer = MouseTransitionObserver(
            button_probe=lambda button=button: button["down"],
            cursor_probe=lambda cursor=cursor: cursor,
            clock=lambda: 10.0,
        )
        observer.update_context(
            window_hwnd=321,
            game_instance_id="game-reroll",
            selection_epoch=7,
            eligible=True,
        )
        button["down"] = True
        observer.poll_once()

        event = observer.consume_slot_event(
            client_rect=client_rect,
            frame_size=frame_size,
            source={"layout_transform": {}},
            window_hwnd=321,
            game_instance_id="game-reroll",
            selection_epoch=7,
        )

        assert event is not None
        assert event["transition_slot"] == expected_slot
        assert event["transition_source"] == "async_mouse_down"
        assert event["transition_kind"] == "reroll"
        assert observer.status()["consumed_count"] == 1


def test_mouse_transition_status_counts_outside_and_stale_events() -> None:
    button = {"down": False}
    clock = {"now": 10.0}
    observer = MouseTransitionObserver(
        button_probe=lambda: button["down"],
        cursor_probe=lambda: (5, 5),
        clock=lambda: clock["now"],
    )
    observer.update_context(
        window_hwnd=321,
        game_instance_id="game-status",
        selection_epoch=2,
        eligible=True,
    )
    button["down"] = True
    observer.poll_once()
    assert observer.consume_slot_event(
        client_rect=(0, 0, 1000, 600),
        frame_size=(1000, 600),
        source={"layout_transform": {}},
        window_hwnd=321,
        game_instance_id="game-status",
        selection_epoch=2,
    ) is None
    button["down"] = False
    observer.poll_once()
    button["down"] = True
    observer.poll_once()
    clock["now"] = 11.0
    assert observer.consume_slot_event(
        client_rect=(0, 0, 1000, 600),
        frame_size=(1000, 600),
        source={"layout_transform": {}},
        window_hwnd=321,
        game_instance_id="game-status",
        selection_epoch=2,
    ) is None

    status = observer.status()
    assert status["edge_count"] == 2
    assert status["outside_slot_count"] == 1
    assert status["stale_drop_count"] == 1


def test_mouse_event_is_discarded_across_game_or_epoch_boundary() -> None:
    button = {"down": False}
    observer = MouseTransitionObserver(
        button_probe=lambda: button["down"],
        cursor_probe=lambda: (500, 500),
        clock=lambda: 1.0,
    )
    observer.update_context(window_hwnd=1, game_instance_id="old", selection_epoch=2, eligible=True)
    button["down"] = True
    observer.poll_once()

    assert observer.consume_slot_event(
        client_rect=(0, 0, 1000, 600),
        frame_size=(1000, 600),
        source={"layout_transform": {}},
        window_hwnd=1,
        game_instance_id="new",
        selection_epoch=2,
    ) is None
