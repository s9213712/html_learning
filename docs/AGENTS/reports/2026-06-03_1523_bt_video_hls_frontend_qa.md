# 2026-06-03 BT Video/HLS Frontend QA

## Findings

- Fixed: the video publish UI option that visually meant "do not prebuild HLS" could still submit both `prepared_hls` and `realtime_proxy`, causing MP4 files to queue HLS unintentionally.
  - Impact: MP4/WebM uploads or BT-downloaded MP4 files could consume Premium HLS worker/storage work even when the user expected Standard realtime playback only.
  - Fix: `direct` now maps to `["realtime_proxy"]`; MKV/AVI/MOV-like containers auto-select `prepared_hls`; E2EE files never auto-select server HLS.
  - Regression coverage: `tests/frontend/video/test_frontend_videos.py`.

No remaining blocker was reproduced in the final frontend pass for the tested MP4/MKV BT files.

## Frontend E2E Coverage

Probe: `/tmp/hackme_video_bt_hls_probe_20260603_full/probe_report.json`

Result: 41 checks passed, 0 failed.

Tested artifacts:

- MP4 BT file: `43124cba04b14b0b8fbefc11ef10e5f0`, `/爭取歐/Awajima-Hyakkei-08.mp4`
- MKV BT file: `84a4b56dbca0489c927890b1d3fbf9f7`, `/爭取歐/concurrent-root-1.mkv`

Verified:

- MP4 publish selected `direct` in UI, submitted `["realtime_proxy"]`, did not create `stream_asset`, and playback had no HLS `master_url`.
- MP4 Standard realtime proxy returned bytes and a saved sample at `/tmp/hackme_video_bt_hls_probe_20260603_full/mp4_default_realtime_sample.mp4` was ffprobe-decodable with audio and video streams.
- MKV publish auto-selected `prepared_hls`, queued HLS, reached ready state, and served master manifest, variant playlist, and a media segment.
- HLS sample `/tmp/hackme_video_bt_hls_probe_20260603_full/mkv_muxed_variant_hls_sample.bin` was ffprobe-decodable with audio and video streams.
- Subtitle upload through the frontend succeeded; playback exposed 4 subtitle tracks and the first VTT fetched successfully.
- Owner desktop player rendered without horizontal overflow.
- Shared HLS page rendered on desktop and mobile, with no horizontal overflow.
- Shared page exposed both Standard realtime and Premium HLS service options.
- Shared page exposed audio track payload and mounted 4 subtitle `<track>` elements.
- Shared MP4 page used realtime proxy only.

Screenshots:

- `/tmp/hackme_video_bt_hls_probe_20260603_full/owner_desktop_hls_detail.png`
- `/tmp/hackme_video_bt_hls_probe_20260603_full/shared_desktop_hls.png`
- `/tmp/hackme_video_bt_hls_probe_20260603_full/shared_mobile_hls.png`
- `/tmp/hackme_video_bt_hls_probe_20260603_full/shared_desktop_mp4_realtime.png`

## Commands

- `python3 /tmp/hackme_video_bt_hls_probe.py --base-url https://127.0.0.1:50931 --out-dir /tmp/hackme_video_bt_hls_probe_20260603_full --hls-timeout-seconds 900`
- `node --check public/js/39-videos.js`
- `pytest -q tests/frontend/video/test_frontend_videos.py`
- `git diff --check`

## Notes

Headless browser QA cannot prove speaker output acoustically. The audio check used the stricter server-side evidence available in automation: actual streamed bytes were saved and decoded with `ffprobe`, confirming audio streams are present and decodable for both Standard realtime and HLS playback.
