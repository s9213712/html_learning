# Standalone Generation Probe Dependencies

This file documents the runtime dependencies for the three standalone probes:
regular ComfyUI, HF Diffusers, and GGUF.

## Runtime Paths

- Probe home: set `PROBE_ROOT` to a large local disk path.
- WSL isolated Python deps: set `PROBE_DEPS` to a writable dependency target.
- Hugging Face cache root: set `HF_PROBE_CACHE_ROOT`; Hugging Face Hub will use its `hub` child directory.
- Windows ComfyUI: `D:/ComfyUI/ComfyUI_windows_portable/ComfyUI`
- Windows ComfyUI UNet/GGUF models: `D:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/unet`

## Token Rule

Do not store Hugging Face tokens in JSON config, reports, README files, or shell
commands. Use `HF_TOKEN`, `--hf-token-file`, or `--hf-token-stdin` at runtime.
Reports must only record `hf_token_supplied: true/false`.

## Python Packages

The app runtime and the standalone probe runtime are different install targets.
For the web app's HF / Diffusers backend, use `requirements-hf.txt` through
`test_for_develop.sh` or the deployment environment. For copied standalone
probes, use `generation_probe_requirements.txt` with `--target`/`PYTHONPATH` as
shown below. Do not assume a successful probe dependency install changes the
web app's virtualenv.

WSL path for HF Diffusers and GGUF inspect/download:

```bash
$WSL_PYTHON -m pip install \
  --target "$PROBE_DEPS" \
  -r "$PROBE_ROOT/generation_probe_requirements.txt"
```

Use the environment when running WSL probes:

```bash
PYTHONPATH="$PROBE_DEPS" HF_PROBE_CACHE_ROOT="$HF_PROBE_CACHE_ROOT" \
  "$WSL_PYTHON" SCRIPT.py ...
```

Windows portable Python currently needs these modules for the ComfyUI API paths:

```bash
D:/ComfyUI/ComfyUI_windows_portable/python_embeded/python.exe -m pip install \
  huggingface-hub gguf psutil pynvml nvidia-ml-py
```

## Model Dependencies

GGUF dependencies are profile based. Do not treat GGUF as a universal
`repo + file` field. Each official profile must map the UNet GGUF, companion
text encoders, VAE, loader class, cache root, install folders, and verified
sampler defaults before it is exposed to customers.

Regular ComfyUI checkpoint:

- `SDXL\illustrious(IL)\janxd系列\JANKUTrainedChenkinNoobai_v777.safetensors`

HF Diffusers repo:

- `dhead/wai-nsfw-illustrious-sdxl-v140-sdxl`
- Variant: `fp16`

GGUF repo/file:

- Profile: `wai_illustrious_v110_q8`
- Repo: `kekusprod/WAI-NSFW-illustrious-SDXL-v110-GGUF`
- File: `WAI-NSFW-illustrious-SDXL-v110-Q8_0.gguf`
- Installed target: `D:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/unet/WAI-NSFW-illustrious-SDXL-v110-Q8_0.gguf`

GGUF ComfyUI workflow must follow the model card. The selected WAI
Illustrious GGUF is not compatible with arbitrary SDXL CLIP/VAE files; its
model card points to companion assets from `calcuis/illustrious`:

- Required CLIP-L: `illustrious_clip_l_fp8_e4m3fn.safetensors`
- Required CLIP-G: `illustrious_clip_g_fp8_e4m3fn.safetensors`
- Required VAE: `illustrious_v110_vae_fp8_e4m3fn.safetensors`

Install CLIP files into `models/text_encoders` and VAE files into `models/vae`,
then use `DualCLIPLoaderGGUF` plus `VAELoader`. Generic SDXL `clip_l`,
`clip_g`, or `sdxl_vae` can produce technically successful but unusable images.

## Standard Commands

Regular ComfyUI:

```bash
D:/ComfyUI/ComfyUI_windows_portable/python_embeded/python.exe \
  D:/tmp/hackme_comfyui_remote_probe/standalone_regular_comfyui_txt2img.py \
  --config D:/tmp/hackme_comfyui_remote_probe/generation_probe_config.example.json \
  --comfyui-url http://127.0.0.1:8188 \
  --out-dir D:/tmp/hackme_comfyui_remote_probe/regular_1920x1080_steps24_win
```

HF Diffusers:

```bash
PYTHONPATH="$PROBE_DEPS" HF_PROBE_CACHE_ROOT="$HF_PROBE_CACHE_ROOT" \
  "$WSL_PYTHON" \
  "$PROBE_ROOT/standalone_hf_diffusers_txt2img.py" \
  --config "$PROBE_ROOT/generation_probe_config.example.json" \
  --hf-token-file /tmp/hackme_hf_token_runtime \
  --out-dir "$PROBE_ROOT/hf_1920x1080_steps24_token"
```

GGUF ComfyUI-GGUF:

```bash
D:/ComfyUI/ComfyUI_windows_portable/python_embeded/python.exe \
  D:/tmp/hackme_comfyui_remote_probe/standalone_gguf_txt2img.py \
  --config D:/tmp/hackme_comfyui_remote_probe/generation_probe_config.example.json \
  --gguf-profile wai_illustrious_v110_q8 \
  --backend comfyui \
  --comfyui-url http://127.0.0.1:8188 \
  --install-to-comfyui-unet-dir D:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/unet \
  --install-to-comfyui-text-encoder-dir D:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/text_encoders \
  --install-to-comfyui-vae-dir D:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models/vae \
  --gguf-file WAI-NSFW-illustrious-SDXL-v110-Q8_0.gguf \
  --local-files-only \
  --out-dir D:/tmp/hackme_comfyui_remote_probe/gguf_1920x1080_steps24_win_illustrious_aux
```
