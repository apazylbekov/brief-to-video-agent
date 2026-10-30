---
name: brief-to-video
description: Turn a one-line video request into a matched creative brief, a 3-shot list, generated and QA-checked clips, and an HTML report, using the brief-to-video MCP tools. Use when asked to make a video, ad or set of clips from a short request.
argument-hint: <one-line video request>
---

# Brief to video

Request: **$ARGUMENTS**

Work through these steps with the `brief-to-video` MCP tools. Don't stop to ask questions:
make sensible choices and say what you chose. Keep commentary to the one-liners asked for.

## 1. Pick the brief

Call `search_briefs` with the request. Pick the best match and say why in one line, e.g.
`Brief: almaty-01 (Aport & Ash Roastery): the only coffee brief, and it's in Almaty.`

## 2. Write the shot list

Write 3 shots that land the brief's `key_message` and show its `must_include` items, in
camera language. For each shot:

- `index` 1, 2, 3 and `duration_s` in whole seconds, 3-8, with **20 s in total at most**.
- `subject`, `setting`, `camera_move` (slow dolly in, orbit, crane up, handheld follow,
  static...), `lens` (24mm wide, 50mm, 100mm macro...), `lighting`, `mood`.
- `prompt`: one paragraph of at most 600 characters that a video model can follow: vertical
  9:16 framing, the subject and its action, the camera move, lens, light and mood.
- `model`: `kling-3.0-standard` (draft, cheaper) unless the shot carries the key message or
  needs fine detail, then `kling-3.0-pro` (quality). `model_reason`: one line on why.
- Leave out `image` unless the request names a file in `assets/`.

Show the shot list as a compact table (shot, duration, camera move, model, reason).

## 3. Save it

Call `save_shot_list(brief_id, shots)`. If it returns an error, it lists every problem: fix
them all and call it again. Note the `run_id` and the estimated total cost.

## 4. Generate and review each shot, in order

For shot 1, then 2, then 3:

1. `generate_clip(run_id, shot)`. If it's refused by the spend cap, don't retry: skip the
   remaining shots and say so at the end.
2. `review_clip(run_id, shot)`, then open the three frame images it returns with Read.
3. Judge them with the **Clip QA rubric in CLAUDE.md** (mock clips: `pass`, `mock
   placeholder`) and call `record_review(run_id, shot, verdict, reasons)` with one line of
   reasons.
4. If its `regeneration` is `regenerated`, a new clip was made: repeat steps 2-3 for it once.
   That second verdict is final; `record_review` won't regenerate again.

## 5. Build the report

Call `build_report(run_id)`.

## 6. Finish

End with this short table, then three one-line facts, and nothing after them:

| Shot | Model | Verdict | Attempts | Cost |
|---|---|---|---|---|

- Brief: the brief id and brand
- Total: Higgsfield cost and shots passed
- Report: the `report_path` from `build_report`
