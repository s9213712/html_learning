# Playwright Realtime Proxy Payload QA

- Date: 2026-06-03 18:15 Asia/Taipei
- Branch: `04.BLOCKCHAIN_RC1`
- Scope: Follow-up frontend QA after cloning and rebasing the latest remote dev launcher update.

## Finding

`scripts/testing/playwright_deep_site_check.py` rejected the current video playback payload when prepared HLS was still pending and playback correctly fell back to realtime proxy.

The probe still accepted only legacy `direct` / `realtime` modes and required `stream_url`. Current playback responses use `mode: realtime_proxy` plus `realtime_proxy_url` and `realtime_proxy.url`.

## Fix

- Accept `realtime_proxy` as a valid direct-or-realtime fallback mode.
- Resolve playable fallback URLs from `stream_url`, `realtime_proxy_url`, or nested `realtime_proxy.url`.
- Add a script contract test so the probe keeps recognizing the current realtime proxy payload shape.

## Verification

- `python3 -m py_compile scripts/testing/playwright_deep_site_check.py tests/scripts/testing/test_playwright_acceptance_pipeline.py`
- `pytest -q tests/scripts/testing/test_playwright_acceptance_pipeline.py tests/frontend/video/test_frontend_videos.py tests/video/streaming/test_video_streaming.py::test_stream_playback_payload_exposes_realtime_proxy_when_enabled tests/video/streaming/test_video_streaming.py::test_stream_playback_payload_discovers_realtime_proxy_audio_before_hls_ready tests/video/streaming/test_video_streaming.py::test_shared_standard_video_playback_uses_shared_hls_and_stream_urls`
- `python3 scripts/testing/playwright_deep_site_check.py --runtime-root /tmp/hackme_web_main_after_realtime_proxy_playwright`

Result: deep Playwright passed with `ok: true`; video upload/share passed with `mode=realtime_proxy` and `shared_mode=realtime_proxy`, and desktop/mobile module tab coverage passed.
