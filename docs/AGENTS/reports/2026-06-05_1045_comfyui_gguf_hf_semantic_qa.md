# 2026-06-05 ComfyUI GGUF/HF Semantic QA

## Findings

- **High - GGUF generation is still not currently runnable in this remote ComfyUI environment.**
  Remote ComfyUI now has `models/unet/illustrious-q4_0.gguf` and `models/text_encoders/illustrious_clip_l.safetensors`, but the selected Calcuis Illustrious profile still lacks `clip_g：illustrious_clip_g.safetensors` and `vae：illustrious_vae.safetensors`. The frontend/API route correctly creates a GGUF check job and then reports the missing remote files without starting a local HF download.
  Evidence: `/tmp/gguf_remote_missing_probe.py`, job `7349a5607c9cb5ce7808e75d`.

- **Medium - Regular ComfyUI generation does not reliably satisfy `2girls`.**
  A live frontend generation against the Windows ComfyUI backend completed with the prompt `2girls, two adult women, fully clothed, serafuku school uniforms, lying side by side on a bed`. The output is a valid image and matches serafuku/lying better than the negative terms, but only one visible person was generated.
  Evidence image: `/mnt/d/share/ComfyUI/output/hackme_web_00001_.png`.

- **Medium - HF/Diffusers semantic quality is not validated by the current tiny-model probe.**
  The live frontend HF/Diffusers route now successfully loads `hf-internal-testing/tiny-sdxl-pipe` and returns a 64x64 image, but that tiny test model outputs noise and cannot be used to judge prompt semantics such as `2girls`, `serafuku`, or `lying`. The WSL Python environment has `torch.cuda.is_available() == false`, so running a real semantic HF model in-process would be CPU-only and impractical for this QA pass.
  Evidence image: `/tmp/hackme_web_hf_frontend_20260605_v2/hf_tiny_sdxl_01.png`; report: `/tmp/hackme_web_hf_frontend_20260605_v2/hf_report.json`.

## Verified Passes

- Repaired the HF/Diffusers GGUF profile controls in the frontend. All required DOM controls are present and visible in HF/Diffusers mode.
- Verified all four enabled official GGUF profiles appear in the frontend dropdown:
  `calcuis_illustrious_sdxl`, `diving_illustrious_flat_anime_sdxl`, `wai_illustrious_v110_sdxl`, `sothmik_wai_illustrious_v140_sdxl`.
- Verified frontend payloads for all four profiles include non-empty `diffusers_gguf_file`, `diffusers_gguf_base_repo`, `diffusers_gguf_profile`, and `diffusers_gguf_variant`.
- Verified backend billing quote accepts all four official GGUF profile payloads with HTTP 200 for the root account.
- Verified GGUF profile generation only starts work after the user submits the selected profile. In remote ComfyUI mode, missing remote GGUF/companion files now report directly instead of downloading locally.
- Verified local ComfyUI mode can auto-download and import missing official GGUF companion files into `models/unet`, `models/text_encoders`, and `models/vae` when `COMFYUI_BASE_DIR` is configured.
- Verified the live `:5000` runtime has Hugging Face token configured without exposing the token in logs or responses.
- Verified live desktop frontend ComfyUI generation against `http://127.0.0.1:8188` completed through the UI and returned an image. Visual QA: `serafuku` pass, `lying` pass, not kimono/standing pass, `2girls` fail because only one person is visible.
- Verified live desktop frontend HF/Diffusers generation completed through the UI with `hf-internal-testing/tiny-sdxl-pipe`, `steps=1`, `64x64`, CPU device. It returned one image in 224.63 seconds.
- Verified mobile frontend smoke at 390px viewport: ComfyUI/GGUF controls and HF repo/GGUF profile controls are visible and usable; `scrollWidth=390`, `overflowCount=0`.
- Fixed `scripts/testing/playwright_comfyui_template_default_qa.py` so captured data URLs are written as image bytes instead of attempting to write an integer size.

## Commands

- `python3 -m pytest /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_idle_retry.py` - 17 passed.
- `python3 -m pytest /home/s92137/hackme_web/tests/comfyui/generation/test_comfyui_generation.py -q -k 'gguf and (auto_routes or auto_downloads or remote_missing or rejects_failed_sd35 or profiles_hide or installed_gguf)'` - 6 passed.
- `python3 -m pytest /home/s92137/hackme_web/tests/comfyui/test_diffusers_client.py /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_idle_retry.py -q` - 54 passed.
- `python3 -m py_compile /home/s92137/hackme_web/scripts/testing/playwright_comfyui_template_default_qa.py` - passed.
- `python3 -m py_compile /home/s92137/hackme_web/routes/comfyui.py /home/s92137/hackme_web/services/comfyui/diffusers_client.py` - passed.
- `bash -n /home/s92137/hackme_web/test_for_develop.sh` - passed.
- `python3 /home/s92137/hackme_web/scripts/testing/playwright_comfyui_template_default_qa.py --base-url https://127.0.0.1:5000 --root-password root --comfyui-api-url http://127.0.0.1:8188 --only origin_sdxl_gguf_txt2img --per-template-timeout 180 --out-dir /tmp/hackme_web_gguf_template_frontend_qa_final_20260605` - expected failure due missing GGUF/SDXL dependencies.
- `python3 /tmp/gguf_profile_payload_probe.py` - all four frontend GGUF profiles present and payloads populated.
- `python3 /tmp/gguf_backend_profile_probe.py` - all four backend quote requests returned HTTP 200.
- `python3 /tmp/gguf_remote_missing_probe.py` - remote ComfyUI mode returned missing `clip_g`/`vae` without local download bytes.
- `python3 /tmp/comfy_regular_frontend_semantic_probe_v2.py` - desktop frontend ComfyUI generation completed; report recovered from DB/job history because the first probe waited on a non-window lexical state variable after success.
- `python3 /tmp/hf_frontend_tiny_sdxl_probe_v2.py` - desktop frontend HF/Diffusers tiny SDXL generation completed; one 64x64 image returned.
- `python3 /tmp/comfy_mobile_ui_smoke_20260605.py` - mobile frontend ComfyUI/GGUF/HF UI smoke passed with no horizontal overflow.
