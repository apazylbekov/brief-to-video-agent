"""Higgsfield video API: request bodies and cost estimates."""

from decimal import ROUND_UP, Decimal

import httpx
from higgsfield_client.auth import get_credential_key
from higgsfield_client.exceptions import CredentialsMissedError

from app import config

# Estimates don't depend on the image (checked 2026-10-29), so image-to-video shots are priced
# with this stand-in before the real file is uploaded at generation time.
ESTIMATE_IMAGE_URL = "https://example.com/first-frame.jpg"


class HiggsfieldError(Exception):
    """A Higgsfield call failed. The message is written for the agent to read."""


def video_request(
    model: config.VideoModel, prompt: str, duration_s: int, image_url: str | None
) -> tuple[str, dict[str, object]]:
    """Model path and JSON body for a generation. The estimate endpoint takes the same body."""
    body: dict[str, object] = {"prompt": prompt, "duration": duration_s, "sound": config.SOUND}
    if image_url is None:
        return model.text_to_video, body | {"aspect_ratio": config.ASPECT_RATIO}
    # Image-to-video has no aspect_ratio: the clip takes the image's shape.
    return model.image_to_video, body | {"image_url": image_url}


def estimate_usd(
    model: config.VideoModel, prompt: str, duration_s: int, has_image: bool
) -> Decimal:
    """Price of one clip: Higgsfield's estimate in live mode, the fixed table in mock mode."""
    if config.video_mode() == "mock":
        # Higgsfield rounds up to a tenth of a cent: 3 s at $0.0714/s ($0.2142) is quoted $0.215.
        return (model.usd_per_second * duration_s).quantize(Decimal("0.001"), ROUND_UP)

    try:
        key = get_credential_key()
    except CredentialsMissedError as exc:
        raise HiggsfieldError(f"VIDEO_MODE=live needs Higgsfield credentials. {exc}") from exc
    image_url = ESTIMATE_IMAGE_URL if has_image else None
    path, body = video_request(model, prompt, duration_s, image_url)
    try:
        with _http() as client:
            response = client.post(
                f"/estimate/{path}", json=body, headers={"Authorization": f"Key {key}"}
            )
    except httpx.HTTPError as exc:
        raise HiggsfieldError(f"Could not reach Higgsfield to estimate {path}: {exc}") from exc
    if response.status_code != 200:
        raise HiggsfieldError(
            f"Higgsfield estimate for {path} failed with HTTP {response.status_code}: "
            f"{_detail(response)}"
        )
    try:
        # {"usd": "0.357", "credits": ..., "discount": ...}: a string, already net of discount.
        return Decimal(response.json()["usd"])
    except (ValueError, KeyError, ArithmeticError) as exc:
        raise HiggsfieldError(
            f"Unexpected estimate response for {path}: {response.text[:200]}"
        ) from exc


def _http() -> httpx.Client:
    return httpx.Client(base_url=config.HIGGSFIELD_URL, timeout=30)


def _detail(response: httpx.Response) -> str:
    # Errors look like {"detail": "duration: 2 is less than the minimum of 3"}.
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200]
