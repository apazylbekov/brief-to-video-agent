"""Generate one clip from the terminal, without the agent.

    uv run python -m app.smoke "a barista pouring latte art, snowy peaks outside"

Saves a one-shot list (5 s, the draft model) and calls generate_clip on it. Follows VIDEO_MODE
like everything else: mock (the default) writes a free placeholder; VIDEO_MODE=live spends real
money on Higgsfield, within MAX_SPEND_USD.
"""

import argparse
import sys

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from app import config, shot_list
from app.server import generate_clip


def main(argv: list[str] | None = None) -> None:
    # No load_dotenv() here: importing app.config already loaded .env via python-dotenv.
    parser = argparse.ArgumentParser(
        prog="python -m app.smoke",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="+", help="what the clip should show (quotes optional)")
    prompt = " ".join(parser.parse_args(argv).prompt)
    model = config.DRAFT_MODEL
    try:
        shot = shot_list.Shot(
            index=1,
            duration_s=5,
            subject="smoke test",
            setting="smoke test",
            camera_move="as in the prompt",
            lens="as in the prompt",
            lighting="as in the prompt",
            mood="as in the prompt",
            prompt=prompt,
            model=model.id,
            model_reason="Smoke test: one 5 s clip on the cheapest model.",
        )
    except ValidationError as exc:
        parser.error(f"invalid prompt: {exc.errors()[0]['msg']}")

    # One shot, so this skips save_shot_list's 3-4 shot rule, which is for agent-made lists.
    run_id, _ = shot_list.new_run("smoke", [shot])
    # flush: in live mode the next line can take minutes, even when output is piped.
    mode = config.video_mode()
    print(f"VIDEO_MODE={mode}: generating one 5 s clip with {model.id} ...", flush=True)
    try:
        clip = generate_clip(run_id, 1)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"run_id:   {clip.run_id}")
    print(f"clip:     {clip.clip_path}")
    print(f"model:    {clip.model}")
    print(f"latency:  {clip.latency_s:.1f} s")
    total = f"total logged ${clip.spent_usd:.3f} of ${clip.max_spend_usd:.2f}"
    print(f"cost:     ${clip.cost_usd:.3f} ({total})")


if __name__ == "__main__":
    main()
