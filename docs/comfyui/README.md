# ComfyUI Reference Set

Use this folder for ComfyUI operator-only material. The main admin route still
starts from [03_ADMIN_GUIDE.md](../03_ADMIN_GUIDE.md) and [WEB.md](../WEB.md).
Cross-feature external integration boundaries are summarized in
[EXTERNAL_INTEGRATION_PLAYBOOK.md](../EXTERNAL_INTEGRATION_PLAYBOOK.md).

- [COMFYUI_ADMIN.md](COMFYUI_ADMIN.md): root/admin-only ComfyUI and Civitai operations
- [COMFYUI_HISTORY_RERUN_GGUF.md](COMFYUI_HISTORY_RERUN_GGUF.md): history restore/rerun ACL, workflow snapshot restore, GGUF rerun repair, HF token shortcut QA, and the 2026-06-05 live `.18` validation record
- [COMFYUI_PERFORMANCE_HARDENING.md](COMFYUI_PERFORMANCE_HARDENING.md): async generation, bounded backend timeouts, stale job handling, and small-VRAM deployment guidance
- [COMFYUI_WORKFLOW_LAYOUT_BUILDER.md](COMFYUI_WORKFLOW_LAYOUT_BUILDER.md): user guide for custom workflow layouts, import/export, version metadata, and dependency errors
- [COMFYUI_TEMPLATE_IMPORTER_PLAN.md](COMFYUI_TEMPLATE_IMPORTER_PLAN.md): staged design for stricter workflow import, manifest derivation, and run gates
- [I2I_OUTPAINT_VALIDATION.md](I2I_OUTPAINT_VALIDATION.md): production i2i / outpaint visual-review contract, SAM3 semantic composition, and HF/Diffusers validation commands

Deployment note:

- Production-like deployments should keep generation in remote ComfyUI or an
  external ComfyUI process. In-process Diffusers is guarded by
  `HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS=1` and should be treated as a
  deliberate local experiment because it can load large models into the Flask
  process and consume RAM / VRAM / CPU.
- Diffusers mode is not a ComfyUI backend. Its progress text should identify
  Hugging Face download, Diffusers model loading, and Python inference phases
  directly; operators can inspect the sanitized Python log tail in the job
  progress panel when a download or model load appears stalled.
- Outpaint must be judged from an actual delivered image, not a queued/succeeded
  job state. The semantic SAM3 route delivers only its validated final composite;
  its background and RGBA foreground artifacts are internal evidence, not
  alternative customer outputs.
- GGUF customer-facing options must be exposed through official profiles, not
  arbitrary repo/file inputs. Each profile maps the GGUF UNet, companion text
  encoders, VAE, loader class, sampler defaults, cache/install expectations,
  and verification status.
- On small VRAM hosts, prefer smaller checkpoints and Linux-native model
  storage instead of loading frequently used models from slow mounted paths.
