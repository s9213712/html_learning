# HF I2I / History Semantic QA

Date: 2026-06-06 08:28 Asia/Taipei

## Findings

No product regressions found in the tested HF/Diffusers and ComfyUI history paths.

## Fix Verified

- Dev restart history persistence: `test_for_develop.sh` now writes the effective `--runtime-root "$RUNTIME_ROOT"` into the generated restart shortcut. This prevents a dev restart from silently switching to a new temp runtime DB and making ComfyUI history look lost.
- ComfyUI history rerun persistence: rerun completion now has test coverage asserting that a new history row is written and appears before the original row when `/api/comfyui/history` is fetched again.
- Profile empty bio wording: no `尚未填寫個人簡介` strings remain in tested frontend paths; the fallback is `這個人很懶什麼都沒寫`.

## HF / Diffusers Coverage

- dhead preflight:
  - Model: `dhead/wai-nsfw-illustrious-sdxl-v140-sdxl`
  - Cache: `\\wsl.localhost\Ubuntu-22.04\home\s92137\.cache\huggingface\hub\models--dhead--wai-nsfw-illustrious-sdxl-v140-sdxl`
  - Result: pass, `model_index.json` repo layout detected, cache size `13,877,379,334` bytes.
  - Artifact: `D:\tmp\hackme_hf_semantic_20260606\dhead_preflight\hf_diffusers_standalone_report.json`
- Heartsync preflight:
  - Model: `Heartsync/NSFW-Uncensored`
  - Cache: `\\wsl.localhost\Ubuntu-22.04\home\s92137\.cache\huggingface\hub\models--Heartsync--NSFW-Uncensored`
  - Result: pass, `model_index.json` repo layout detected, cache size `13,877,382,497` bytes.
  - Artifact: `D:\tmp\hackme_hf_semantic_20260606\heartsync_preflight\hf_diffusers_standalone_report.json`
- Windows CUDA Diffusers runtime:
  - Installed `diffusers 0.38.0` and `accelerate 1.13.0` into `D:\ComfyUI_windows_portable\python_embeded`.
  - Pip also upgraded Windows portable Python `safetensors` from `0.7.0` to `0.8.0rc1`.
  - Verified `torch 2.11.0+cu130`, CUDA available, GPU `NVIDIA GeForce RTX 3050 Laptop GPU`.

## Semantic I2I Result

- Model used for actual I2I generation: `stabilityai/sd-turbo`.
- Reason: official Diffusers docs show SD-Turbo supports image-to-image through `AutoPipelineForImage2Image`; it is practical on the available 4GB RTX 3050 where the larger dhead/Heartsync SDXL-class repos are not practical for direct Diffusers generation.
- Prompt: `a bright studio photo of one red apple sitting on a blue plate, white background, crisp realistic food photography`
- Source image: `D:\tmp\hackme_hf_semantic_20260606\sd_turbo_i2i\source_apple_plate.png`
- Output image: `D:\tmp\hackme_hf_semantic_20260606\sd_turbo_i2i\sd_turbo_i2i_output.png`
- Runtime: CUDA, total `134.455s`, generation `19.546s`, peak reserved VRAM `3406.0 MB`.
- Semantic judgment: pass. The output preserves the requested red apple, green leaf/stem, blue plate, white background, and realistic food-photo styling. No extra people/text/watermark were visible.

## Limits / Notes

- Direct dhead txt2img from the WSL HF cache failed under Windows Python because the WSL HF snapshot symlinks were not visible as real files through UNC. The dereferenced copy to `D:\tmp\hackme_hf_semantic_20260606\models\dhead` worked as a local directory, but direct Diffusers loading became unresponsive on this 4GB VRAM machine and was aborted. This is treated as an environment/resource limitation, not a confirmed app regression.
- The project backend intentionally has low-VRAM safeguards for Diffusers; on this hardware, full SDXL-class Diffusers generation should be expected to need CPU/offload behavior or a smaller model.

## Commands

- `python3 -m pytest -q /home/s92137/hackme_web/tests/comfyui/test_diffusers_client.py` -> `39 passed`
- `python3 -m pytest -q /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py` -> `16 passed`
- `python3 -m pytest -q /home/s92137/hackme_web/tests/comfyui/generation/test_comfyui_generation.py -k "diffusers_mode_lists_repo_and_generates_without_comfyui_nodes"` -> `1 passed`
- `python3 -m pytest -q /home/s92137/hackme_web/tests/scripts/testing/test_develop_runtime_reset.py /home/s92137/hackme_web/tests/comfyui/generation/test_comfyui_history_delete_and_prompt.py /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_history_ui.py` -> `11 passed`
- `bash -n /home/s92137/hackme_web/test_for_develop.sh` -> pass

## References

- Hugging Face Diffusers image-to-image docs: https://huggingface.co/docs/diffusers/en/using-diffusers/img2img
- Hugging Face Diffusers SDXL/SD-Turbo guide: https://huggingface.co/docs/diffusers/using-diffusers/sdxl_turbo
