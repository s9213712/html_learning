# 2026-06-05 ComfyUI GGUF/HF Semantic QA

## Findings

- **High - GGUF generation is not currently runnable in this environment.**
  ComfyUI has the GGUF custom nodes, but `/mnt/d/share/ComfyUI/models` contains no `.gguf` files, and the cached `sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF` download only contains an `.incomplete` blob. The official GGUF workflow correctly rejects before queueing with missing `clip_g.safetensors`, `clip_l.safetensors`, `sdxl_vae.safetensors`, and `WAI-NSFW-Illustrious-v140-Q8_0.gguf`.
  Evidence: `/tmp/hackme_web_gguf_template_frontend_qa_final_20260605/report.json`.

- **Medium - Regular ComfyUI generation does not reliably satisfy `2girls`.**
  A live frontend generation against the Windows ComfyUI backend completed with the prompt `2girls, two adult women, fully clothed, serafuku school uniforms, lying side by side on a bed`. The output is a valid image and matches serafuku/lying better than the negative terms, but only one visible person was generated.
  Evidence image: `/mnt/d/share/ComfyUI/output/hackme_web_00001_.png`.

- **Medium - HF/Diffusers tiny frontend probe fails before image generation.**
  The frontend sent the HF/Diffusers payload, but the backend failed loading `hf-internal-testing/tiny-stable-diffusion-pipe` because the cached UNet directory lacks `diffusion_pytorch_model.safetensors`. No image was generated, so no semantic judgement was possible from this HF run.
  Evidence screenshot: `/tmp/hackme_web_hf_frontend_20260605/hf_frontend.png`.

## Verified Passes

- Repaired the HF/Diffusers GGUF profile controls in the frontend. All required DOM controls are present and visible in HF/Diffusers mode.
- Verified all four enabled official GGUF profiles appear in the frontend dropdown:
  `calcuis_illustrious_sdxl`, `diving_illustrious_flat_anime_sdxl`, `wai_illustrious_v110_sdxl`, `sothmik_wai_illustrious_v140_sdxl`.
- Verified frontend payloads for all four profiles include non-empty `diffusers_gguf_file`, `diffusers_gguf_base_repo`, `diffusers_gguf_profile`, and `diffusers_gguf_variant`.
- Verified backend billing quote accepts all four official GGUF profile payloads with HTTP 200 for the root account.
- Verified GGUF profile generation only starts work after the user submits the selected profile. In remote ComfyUI mode, missing remote GGUF/companion files now report directly instead of downloading locally.
- Verified local ComfyUI mode can auto-download and import missing official GGUF companion files into `models/unet`, `models/text_encoders`, and `models/vae` when `COMFYUI_BASE_DIR` is configured.
- Verified the live `:5000` runtime has Hugging Face token configured without exposing the token in logs or responses.
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
