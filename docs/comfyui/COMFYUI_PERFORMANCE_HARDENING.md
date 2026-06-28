# ComfyUI Performance Hardening

ComfyUI is treated as an external, slow backend. Model loading, generation and
interrupts must not keep the main Flask request waiting for the full operation.

## Current Runtime Rules

- `POST /api/comfyui/generate` always creates a background generation job and
  returns immediately with `async=true` and `job.job_id`.
- The browser polls `GET /api/comfyui/jobs/<job_id>` for progress and results.
- The default generation worker timeout is bounded by
  `COMFYUI_GENERATION_TIMEOUT_SECONDS` (`0` / unlimited by default).
- Backend HTTP calls use `COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS` (`8` seconds
  by default) so a stalled ComfyUI process cannot leave worker calls hanging for
  the old long default.
- Status checks use `COMFYUI_STATUS_TIMEOUT_SECONDS` (`2` seconds by default).
- Interrupt calls use `COMFYUI_INTERRUPT_TIMEOUT_SECONDS` (`2` seconds by
  default). Normal users still avoid global interrupt when another user's job is
  active on the same backend.
- If a queued/running job stops reporting progress longer than
  `COMFYUI_JOB_STALE_SECONDS` (`1500` seconds by default), the job status payload
  marks progress as `backend_unresponsive` so the UI can show that ComfyUI may
  be loading a large model or under disk/VRAM pressure.
- A queued/running job is only marked failed after
  `COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS` (`7200` seconds by default) with
  no new progress, which avoids treating low-VRAM Qwen Image Edit steps as a
  crash.
- `GET /api/comfyui/status` and `GET /api/comfyui/models` include
  `storage_warnings` when the configured ComfyUI project/model path is under
  `/mnt/*`.
- Job Center progress writes are throttled by
  `COMFYUI_JOB_PROGRESS_DB_THROTTLE_SECONDS` (`2` seconds by default) to reduce
  DB churn while progress is unchanged.
- Platform-level Job Center progress now also goes through
  `services/core/progress_backend.py`: latest `running` progress is coalesced in
  the progress backend and periodically checkpointed to SQLite, while terminal
  states still write DB immediately.

## Deployment Guidance

On small VRAM hosts, prefer smaller/default-safe checkpoints. Frequently used
models should live on WSL/Linux native storage instead of `/mnt/d` or other
Windows-mounted paths. Loading large checkpoint files through `/mnt/d` can add
heavy disk and CPU pressure and make unrelated web requests visibly slower.

Recommended model layout:

```text
/home/<user>/comfyui-models/checkpoints
/home/<user>/comfyui-models/loras
/home/<user>/comfyui-models/vae
```

Then point ComfyUI at those paths with native symlinks or its extra model paths
configuration, and keep `COMFYUI_BASE_DIR` on a native Linux filesystem when
possible.

## Operator Checks

Before enabling public generation on a small host:

- Run `scripts/comfyui/local_connection_smoke.py` against the configured port.
- Run `scripts/comfyui/feature_probe.py` and confirm status/interrupt return
  quickly under load.
- Watch `nvidia-smi`, system RAM and request latency while loading the default
  checkpoint.
- If `backend_unresponsive` appears often, lower default model size, move models
  off `/mnt/d`, or increase worker timeout only after confirming the main server
  remains responsive.
