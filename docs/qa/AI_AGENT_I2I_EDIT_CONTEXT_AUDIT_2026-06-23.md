# AI Agent Image-to-Image Edit Context Audit - 2026-06-23

Target: live `https://127.0.0.1:5000`

Scope:
- Natural-language AI Agent routing for ComfyUI image-to-image edits.
- Recent generated image reuse through message `images[].image_ref`.
- `img2img` style change, `outpaint`, and `inpaint` missing-mask boundary.
- Frontend write-tool dispatch was intercepted; AI Agent planner and UI flow were real, ComfyUI execution was not.

Final evidence:
- JSON: `/tmp/hackme_ai_agent_i2i_edit_context_20260623_live_5/report.json`
- Screenshot: `/tmp/hackme_ai_agent_i2i_edit_context_20260623_live_5/ai_agent_i2i_edit_context.png`
- Probe: `scripts/testing/ai_agent_i2i_edit_context_probe.py`

Final result: PASS

Checks:
- `style_uses_comfyui_generate`: PASS
- `style_uses_img2img`: PASS
- `style_uses_recent_source_ref`: PASS
- `style_keeps_denoise`: PASS
- `outpaint_uses_comfyui_generate`: PASS
- `outpaint_mode_and_source`: PASS
- `outpaint_edges`: PASS
- `inpaint_missing_mask_does_not_write`: PASS
- `inpaint_missing_mask_clarifies`: PASS
- `no_browser_errors`: PASS

Issues found and fixed:
- AI Agent write-tool schema did not expose ComfyUI edit fields such as `generation_mode`, `source_image_ref`, `mask_image_ref`, `denoise_strength`, and `outpaint_*`.
- Frontend planner context did not expose recent generated image refs, so "edit the previous image" could silently lose the source image.
- Frontend normalization could drop valid image-edit arguments.
- LLM sometimes returned a correct `img2img` plan with an empty `prompt`; frontend then stopped with a generic "need to know what to draw" message. The frontend now falls back to the user's natural-language edit request as prompt when an image-edit mode and source image are present.
- Test fake preview/job routes originally produced expected 404 noise; the probe now stubs those endpoints so browser errors represent real frontend issues.

Notes:
- The live AI Agent backend was `openai_compatible` via `http://127.0.0.1:11434/v1`.
- Final observed planner latencies were roughly 7-10 seconds per case in this run.
- The unavailable retired vision model still appears in `/models`, but current allowed models exclude it from frontend selection.
