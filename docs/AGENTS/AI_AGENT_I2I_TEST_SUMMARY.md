# AI Agent i2i Test Summary

Date: 2026-06-28

Scope: frontend natural-language AI Agent tests for ComfyUI Qwen Image Edit 2509 person i2i editing. Full image artifacts and conversation logs are intentionally kept in local runtime reports under `docs/AGENTS/reports/` and are not committed.

## Current Findings

- Source preservation bug fixed: existing-source i2i audit no longer silently resizes portrait/landscape sources to `1024x1024`. The 1080x1920 source is preserved as `source_1080x1920.png`.
- Report rendering bug fixed: audit reports can render dynamic source filenames/resolutions instead of hard-coding `source_1024x1024.png`.
- Prompt flow bug fixed: unmasked img2img denoise hints no longer say "only redraw inside the mask", which contradicted no-mask Qwen edit cases.
- v5 body/lace/proportions run: failed. The source had been compressed to `1024x1024`, causing severe aspect contamination and blurred extension bands.
- v6 body/lace/proportions run: failed but validated the source fix. Input and final output were 1080x1920, but the model kept the original kimono and did not achieve the requested white lace dress/body proportion changes.
- v7 body/lace/proportions run: partial improvement, still failed. White lace clothing and body changes were achieved, but the arm pose changed unexpectedly and the dress drifted toward high-slit qipao/cheongsam styling.

## Test Parameters

Shared source:

- Source case: human-accepted festival street full-body person i2i source.
- Source path, local artifact only: `docs/AGENTS/reports/2026-06-28_ai_agent_person_i2i_festival_street_full_body_kimono_single_ponytail_geta_1080x1920_v1/results/festival_street_full_body_kimono_single_ponytail_geta_1080x1920_result.png`
- Source requirements: full-body anime girl, same identity preservation target, dark blue single ponytail, festival hair accessories, night street background, feet visible.
- Full artifacts: kept locally under ignored `docs/AGENTS/reports/`.

Shared ComfyUI / model settings:

- Backend: remote ComfyUI API at `http://127.0.0.1:8189` during the test.
- Frontend test path: root user, AI Agent chat, natural-language command, write tool `write_comfyui_generate`.
- Workflow: `origin_qwen_image_edit_2509`.
- Diffusion model: `qwen_image_edit_2509_fp8_e4m3fn.safetensors`.
- CLIP: `qwen_2.5_vl_7b_fp8_scaled.safetensors`.
- VAE: `qwen_image_vae.safetensors`.
- LoRA: `QWEN/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors`, model strength `1.0`.
- Sampler: `euler`, scheduler `simple`.
- Seed: `1001672099958606`.
- Batch size: `1`.
- Steps: `4`.
- CFG: `1`.
- Output target: `1080x1920`.

| Run | Job / Prompt | Source Handling | Denoise | Prompt Strategy | Result |
| --- | --- | --- | --- | --- | --- |
| v5 | job `ca562be2a37a17331f9b1ecd`, prompt `5ee3818a-bf25-4e3e-ab94-f7c51eeb3138` | Incorrectly normalized source to `1024x1024`; requested output `1080x1920` | `0.55` | Natural-language CJK prompt, backend extracted English Qwen edit instruction | FAIL. Aspect contamination caused blurred top/bottom extension bands; result stayed close to kimono source. |
| v6 | job `06f533888828586f7cdfa13e`, prompt `6cf7276d-cfa5-44ac-82a3-9a86415bda98`, workflow run `118` | Preserved ComfyUI input as `1080x1920`; final postprocessed output `1080x1920` | `0.55` | Same semantic target after source-size fix | FAIL. Size handling was fixed, but the model remained conservative: kimono preserved, body/lace goals mostly missed. |
| v7 | job `b2dd7daf25a7b807e68646f7`, prompt `897af3d3-ea16-4105-9777-c43bdfe3a6f5`, workflow run `119` | Preserved ComfyUI input as `1080x1920`; final output `1080x1920` | `0.85` | Strengthened English edit prompt: replace kimono with fully lined opaque white lace maxi dress, taller silhouette, slim waist, moderately larger bust, longer legs, preserve face/hair/accessories/background | PARTIAL / FAIL. Clothing and body changed, but arm pose drifted from clasped hands to lowered arms; dress drifted toward high-slit qipao/cheongsam, violating preservation and negative intent. |

Scoring rule:

- Hard fail overrides prompt score: severe anatomy collapse, six/missing fingers, limb/object penetration, required body parts cropped out, blank/black/gray artifact frames, unreadable subject, or different main subject.
- If there is no hard fail, score by achieved prompt items. v7 improved target edits but still failed because pose preservation and dress constraints were not met.

## Next Test Direction

- Retry clothing/body edit at a middle denoise range, around `0.70-0.75`.
- Add explicit pose preservation: keep both hands clasped in front of the chest and do not change arm pose.
- Strengthen negative prompt handling for qipao, cheongsam, high slit, exposed leg, transparent dress, and pose drift.
- If the combined task remains unstable, split into two staged tests: clothing replacement first, then body proportions.
