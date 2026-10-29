"""Shot lists: the Shot and ShotList models, and saving a validated list as a new run."""

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import ErrorDetails

from app import config, higgsfield, retrieval

MAX_TOTAL_S = 20
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _model_choices() -> str:
    return " or ".join(f"'{m.id}' ({m.tier})" for m in config.VIDEO_MODELS.values())


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    index: int = Field(description="Position in the list, counting from 1")
    duration_s: int = Field(ge=3, le=8, description="Clip length in whole seconds")
    subject: str = Field(min_length=1, description="Who or what the shot shows")
    setting: str = Field(min_length=1, description="Where it happens")
    camera_move: str = Field(min_length=1, description="e.g. slow dolly in, static, orbit")
    lens: str = Field(min_length=1, description="e.g. 24mm wide, 50mm, 100mm macro")
    lighting: str = Field(min_length=1)
    mood: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=600, description="Text sent to the video model")
    model: str = Field(description=f"Video model: {_model_choices()}")
    model_reason: str = Field(min_length=1, max_length=200, description="One line: why this model")
    image: str | None = Field(
        default=None, description="Optional first frame: an image file in assets/"
    )

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in config.VIDEO_MODELS:
            raise ValueError(f"unknown model {value!r}; use {_model_choices()}")
        return value

    @field_validator("model_reason")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("must be a single line")
        return value

    @field_validator("image")
    @classmethod
    def _image_in_assets(cls, value: str | None) -> str | None:
        if not value:
            return None  # agents often send "" for "no image"
        assets = config.ASSETS_DIR.resolve()
        path = (assets / value).resolve()
        if not path.is_relative_to(assets):
            raise ValueError(f"{value!r} is outside assets/; give a file name inside assets/")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(f"{value!r} is not an image; use one of {', '.join(IMAGE_SUFFIXES)}")
        if not path.is_file():
            raise ValueError(f"{value!r} is not in assets/; available images: {_images()}")
        return value


def _images() -> list[str]:
    assets = config.ASSETS_DIR
    if not assets.is_dir():
        return []
    found = (p for p in assets.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    return sorted(str(p.relative_to(assets)) for p in found)[:20]


class ShotList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: str
    shots: list[Shot] = Field(min_length=3, max_length=4)

    @field_validator("brief_id")
    @classmethod
    def _known_brief(cls, value: str) -> str:
        if value not in {b.id for b in retrieval.load_briefs()}:
            raise ValueError(f"unknown brief_id {value!r}; use an id returned by search_briefs")
        return value

    @field_validator("shots")
    @classmethod
    def _order_and_total(cls, shots: list[Shot]) -> list[Shot]:
        # Runs only once every shot is valid on its own.
        problems = []
        indexes = [s.index for s in shots]
        if indexes != list(range(1, len(shots) + 1)):
            problems.append(f"indexes must be 1 to {len(shots)} in order, got {indexes}")
        total = sum(s.duration_s for s in shots)
        if total > MAX_TOTAL_S:
            problems.append(
                f"total duration is {total}s, over the {MAX_TOTAL_S}s limit; "
                "shorten shots or drop one"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return shots


class ShotListError(ValueError):
    """The shot list is invalid. The message lists every problem for the agent to fix."""


class ShotEstimate(BaseModel):
    index: int
    model: str
    duration_s: int
    usd: float


class SavedShotList(BaseModel):
    run_id: str
    shots_path: str
    pricing: Literal["higgsfield", "mock"] = Field(
        description="'higgsfield': the live estimate endpoint; 'mock': the fixed price table"
    )
    total_usd: float
    total_duration_s: int
    shots: list[ShotEstimate]


def save(brief_id: str, shots: object) -> SavedShotList:
    """Validate and price a shot list, then save it as runs/<run_id>/shots.json."""
    try:
        shot_list = ShotList.model_validate({"brief_id": brief_id, "shots": shots})
    except ValidationError as exc:
        raise ShotListError(_describe(exc)) from exc

    # Price before writing anything, so a failed estimate leaves no half-made run behind.
    prices = [
        higgsfield.estimate_usd(
            config.VIDEO_MODELS[s.model], s.prompt, s.duration_s, s.image is not None
        )
        for s in shot_list.shots
    ]
    run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"  # UTC, sortable
    path = config.RUNS_DIR / run_id / "shots.json"
    path.parent.mkdir(parents=True)
    path.write_text(shot_list.model_dump_json(indent=2) + "\n")

    return SavedShotList(
        run_id=run_id,
        shots_path=str(path),
        pricing="mock" if config.video_mode() == "mock" else "higgsfield",
        total_usd=float(sum(prices, Decimal(0))),
        total_duration_s=sum(s.duration_s for s in shot_list.shots),
        shots=[
            ShotEstimate(index=s.index, model=s.model, duration_s=s.duration_s, usd=float(usd))
            for s, usd in zip(shot_list.shots, prices, strict=True)
        ],
    )


# --- Error messages for the agent ---------------------------------------------------------


def _describe(exc: ValidationError) -> str:
    lines = [f"- {_where(e['loc'])}: {_message(e)}" for e in exc.errors()]
    return (
        f"Shot list rejected, nothing saved. Fix all {len(lines)} problem(s) and call "
        "save_shot_list again (shots[0] is the first shot):\n" + "\n".join(lines)
    )


def _where(loc: tuple[int | str, ...]) -> str:
    # ("shots", 1, "model") -> "shots[1].model"
    path = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc)
    return path.lstrip(".") or "shot list"


def _message(error: ErrorDetails) -> str:
    if error["type"] == "extra_forbidden":
        fields = Shot.model_fields if len(error["loc"]) == 3 else ShotList.model_fields
        return f"unknown field; allowed fields are {', '.join(fields)}"
    message = error["msg"].removeprefix("Value error, ")
    value = error["input"]
    # Custom messages already quote the value; for the rest, show what was sent.
    if error["type"] != "value_error" and isinstance(value, str | int | float):
        if isinstance(value, str) and len(value) > 40:
            message += f" (got {len(value)} characters)"
        else:
            message += f" (got {value!r})"
    return message
