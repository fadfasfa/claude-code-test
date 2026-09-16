from hextech.modules.acquisition.load_guard import BackgroundLoadGuard


def event(index, duration, *, state="candidate"):
    return {"source": {"game_instance_id": "game", "scene_state": state},
            "timing": {"observation_kind": "recognition", "capture_status": "captured",
                       "captured_at": index, "capture_started_at": index,
                       "recognition_completed_at": index + duration / 1000}}


def test_slow_samples_pause_background_even_if_not_active_and_healthy_samples_resume():
    guard = BackgroundLoadGuard()
    for index in range(1, 6):
        guard.observe(event(index, 250))
    assert guard.paused
    for index in range(6, 16):
        guard.observe(event(index, 100))
    assert not guard.paused


def test_same_frame_does_not_accumulate_load_and_new_game_resets():
    guard = BackgroundLoadGuard()
    for _ in range(10):
        guard.observe(event(1, 250))
    assert not guard.paused
    for i in range(2, 6):
        guard.observe(event(i, 250))
    assert guard.paused
    next_game = event(1, 100)
    next_game["source"]["game_instance_id"] = "new-game"
    assert not guard.observe(next_game)
