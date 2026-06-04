# ComfyUI Scripts

This folder holds ComfyUI-specific live probes and helper tooling.

- [comfyui_run_in_linux.template.sh](comfyui_run_in_linux.template.sh): Linux local startup template that operators can download from the root ComfyUI settings panel
- [feature_probe.py](feature_probe.py): end-to-end ComfyUI feature probe
- [hf_diffusers_repo_smoke.sh](hf_diffusers_repo_smoke.sh): batch HF Diffusers repo smoke runner; supports `HF_TOKEN`, `--hf-token-file`, and `--hf-token-stdin` without writing tokens to config or reports, and copies each image to repo-slug PNGs for live audit; by default it generates the current 1girl beach prompt and the legacy 2girls prompt
- [standalone_hf_diffusers_txt2img.py](standalone_hf_diffusers_txt2img.py): direct HF Diffusers txt2img probe for another machine; it does not call hackme_web and records cache placement, timings, output image, and resource peaks; supports `--interactive` for TTY-guided setup
- [standalone_gguf_txt2img.py](standalone_gguf_txt2img.py): direct GGUF probe for another machine; GGUF should be selected from an official profile because each model needs its own UNet/CLIP/VAE/loader map; supports `--interactive` for TTY-guided setup
- [standalone_regular_comfyui_txt2img.py](standalone_regular_comfyui_txt2img.py): direct ComfyUI API SDXL checkpoint workflow probe for the regular JAN v777 path; supports `--interactive` for TTY-guided setup
- [standalone_comfyui_i2i_matrix.py](standalone_comfyui_i2i_matrix.py): direct ComfyUI API I2I matrix for img2img, inpaint delete/repair, replacement edit, outpaint, ControlNet copy-composition when available, redraw-upscale via `ImageScale + VAEEncode + KSampler`, two-image blend, IPAdapter style reference, and IPAdapter plus masked inpaint; supports `InpaintModelConditioning`/`DifferentialDiffusion` reprobes, per-side outpaint controls, and `--interactive` for TTY-guided setup
- [generation_probe_config.example.json](generation_probe_config.example.json): shared config for all three standalone generation probes; CLI flags override the config when explicitly supplied
- [generation_probe_dependencies.md](generation_probe_dependencies.md): remote runtime package/model dependency checklist and standard commands
- [generation_probe_requirements.txt](generation_probe_requirements.txt): pip requirements for the isolated WSL dependency target used by the standalone probes

## App Backend Split

In the root app settings, `ComfyUI / GGUF` and `HF` are separate tabs. The
ComfyUI / GGUF tab chooses whether generation uses a remote ComfyUI API or a
local ComfyUI folder/start script. The HF tab edits Hugging Face / Diffusers
repo, token, cache, dtype/device, and in-process risk controls. Switching to
HF does not disable or overwrite the ComfyUI / GGUF backend choice.

If the HF test connection reports missing Python packages, install the app HF
runtime with `requirements-hf.txt` through `test_for_develop.sh`, or install the
standalone probe dependency target with `generation_probe_requirements.txt`.

Example external-machine checks:

Do not put Hugging Face tokens in `generation_probe_config.example.json` or
any copied config. Pass tokens at runtime with `HF_TOKEN`, `--hf-token-file`,
or `--hf-token-stdin`; reports only store `hf_token_supplied: true/false`.
Standalone probes default Hugging Face cache under `HF_PROBE_CACHE_ROOT`,
`HF_HOME`, or the user's standard Hugging Face cache directory. On shared remote
machines, set `HF_PROBE_CACHE_ROOT` and probe output directories to an operator
owned temporary workspace outside the repo. HF Diffusers txt2img probes use
`DiffusionPipeline` by default so repo-provided Diffusers metadata controls the
concrete pipeline class. The repo smoke runner also reads the model card's
Diffusers `from_pretrained(...)` snippet by default and applies loader hints
such as `dtype=torch.bfloat16`, `device_map="cuda"`, `revision`, `variant`, and
`subfolder` unless the operator explicitly overrides those flags.

All standalone scripts keep non-interactive CLI mode as the default. Add
`--interactive` only when running in a real terminal and you want prompts for
common values; CI, remote automation, and copied command lines should keep using
explicit flags or the shared config file.

```bash
export HF_TOKEN=...
python3 standalone_hf_diffusers_txt2img.py \
  --config generation_probe_config.example.json \
  --out-dir /tmp/hackme_hf_diffusers_probe
```

```bash
python3 -m py_compile standalone_hf_diffusers_txt2img.py
printf '%s\n' "$HF_TOKEN" | ./hf_diffusers_repo_smoke.sh \
  --hf-token-stdin \
  --hf-cache-root /tmp/hackme_hf_cache \
  --out-root /tmp/hackme_hf_diffusers_repos \
  Heartsync/NSFW-Uncensored=fp16 \
  John6666/perfect-rsb-mix-illustrious-real-anime-sfw-nsfw-definitive-iota-sdxl \
  cagliostrolab/animagine-xl-4.0
```

When no custom `--prompt` is supplied, the batch runner writes one image with
the current default prompt to `<repo-slug>.png` and one image with the legacy
2girls audit prompt to `<repo-slug>_2girls.png`. Supplying `--prompt` switches
the run to single-prompt mode unless `--prompt-suite dual` is explicitly set.

`John6666/wai-ani-nsfw-ponyxl-v6-sdxl` was tested with both
`AutoPipelineForText2Image + float16` and `DiffusionPipeline + bfloat16` and was
removed from the default smoke set after visual output failed. That HF repo maps
to WAI-ANI-PONYXL v6.0, whose upstream release notes say v7.0 fixed full-image
snowflake/noise coverage.

`circlestone-labs/Anima-Base-v1.0-Diffusers` is not in the default smoke set:
its Hugging Face generated "Use this model" snippet uses `DiffusionPipeline`,
but the repo has `modular_model_index.json` and no `model_index.json`. It needs
ModularPipeline/runtime support for Anima modular components instead of the
current generic DiffusionPipeline path.

```bash
export HF_TOKEN=...
python3 standalone_gguf_txt2img.py \
  --config generation_probe_config.example.json \
  --gguf-profile wai_illustrious_v110_q8 \
  --out-dir /tmp/hackme_gguf_probe
```

`generation_probe_config.example.json` also includes:

- `sothmik_wai_illustrious_v140_q8`: SDXL WAI v14 Q8 through
  `UnetLoaderGGUF + DualCLIPLoaderGGUF`.
- `calcuis_illustrious_q4_0`: Calcuis Illustrious SDXL Q4_0 through
  `UnetLoaderGGUF + DualCLIPLoader`.
- `diving_illustrious_flat_anime_q4_k_m`: Diving Illustrious Flat Anime Q4_K_M
  through `UnetLoaderGGUF + DualCLIPLoader`.

`calcuis/sd3.5-large-gguf` was tested and then removed from the supported probe
set after visual output was judged abnormal. Do not expose SD35 GGUF publicly
unless a new mapping and visual reprobe passes.

```bash
python3 standalone_regular_comfyui_txt2img.py \
  --config generation_probe_config.example.json \
  --out-dir /tmp/hackme_regular_comfyui_probe
```

```bash
python3 standalone_comfyui_i2i_matrix.py \
  --comfyui-url http://127.0.0.1:8188 \
  --out-dir /tmp/hackme_comfyui_i2i_matrix
```

For GGUF diagnosis without generation, run `standalone_gguf_txt2img.py --backend inspect --preflight-only`. Do not expose arbitrary GGUF repos as customer-facing options until a profile has been model-card mapped, predownloaded, and visually verified.
