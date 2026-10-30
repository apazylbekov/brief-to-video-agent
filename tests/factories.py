"""Valid shot dicts for tests. Change one field to make an invalid one."""


def make_shot(index: int, **changes: object) -> dict[str, object]:
    shot: dict[str, object] = {
        "index": index,
        "duration_s": 5,
        "subject": "barista pouring a flat white",
        "setting": "Almaty café counter, snowy peaks through the window",
        "camera_move": "slow dolly in",
        "lens": "50mm",
        "lighting": "soft morning window light",
        "mood": "warm and unhurried",
        "prompt": "Vertical close-up of a barista pouring latte art into a flat white, snowy "
        "mountains blurred through the café window, soft morning light, slow dolly in.",
        "model": "kling-3.0-standard",
        "model_reason": "Cheap draft to check framing before a quality pass.",
    }
    return shot | changes


def make_shots(count: int = 3, duration_s: int = 5) -> list[dict[str, object]]:
    return [make_shot(i, duration_s=duration_s) for i in range(1, count + 1)]
