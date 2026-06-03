# Latest Branch Strict QA Audit

Date: 2026-06-03

## Scope

This audit cloned and tested the latest `04.BLOCKCHAIN_RC1` branch of
`s9213712/hackme_web`. The clone started at `b936f762 Add runtime setup
controls`, with the immediately preceding update `a2feaf6f Improve cloud drive
remote downloads` included in the review window.

The audit was intentionally broader than the latest commits. It covered startup,
auth, member/profile flows, storage, E2EE, share links, videos and multi-audio
streaming, ComfyUI workflow UI, games, trading/economy, platform center, root
operations, and desktop/mobile Playwright layout checks.

## Pushed Fix Batches

- `c6a90025 Fix dev startup and auth early clicks`
- `fa9c4e40 Harden profile storage and register feedback`
- `409d5fd6 Fix mobile QA touch targets`
- `7747e5b4 Fix shared realtime video fallback QA`
- `d062e79f Stabilize platform root operations QA`

## Issues Found And Fixed

- `server.py` passed an obsolete `storage_root` keyword to the daily snapshot
  worker helper, breaking direct isolated startup.
- The unauthenticated login/register UI could miss the first auth-tab click or
  first submit action before full site config bootstrapping finished.
- Profile option rendering assumed a dict-like actor and crashed on
  `sqlite3.Row`.
- Storage admin cleanup could query video tables that do not exist in
  storage-only schemas.
- Registration success feedback was cleared by the generic inline-message timer
  and auto-switched users back to login too quickly.
- Mobile platform/admin controls and environment tables had undersized touch
  targets or overflow-prone table layout.
- Shared prepared-HLS-only videos advertised Standard realtime proxy fallback,
  but the shared realtime endpoint still returned 403 for otherwise available
  standard files.
- Browser-video QA assumed a direct media URL and did not accept the current MSE
  blob playback path.
- The platform root-operations QA reused a heavily mutated Playwright page,
  causing a false `ERR_TOO_MANY_RETRIES` navigation failure even while the
  server returned 200. The probe now validates root operations from a fresh root
  browser context.
- Video streaming tests still encoded older direct-stream defaults. They were
  updated to the current policy: Direct is not the default fallback, Standard
  realtime proxy uses mobile H.264 baseline transcode, and prepared HLS may
  expose realtime fallback without enabling direct stream.

## Verification

- Deep full-site Playwright:
  `/tmp/hackme_web_deep_round10_20260603/reports/qa/playwright_deep_site_check_20260603T020909Z.json`
  - PASS; `browser_errors: []`.
  - Covered root login, feature enablement, 33 authenticated API endpoints,
    mobile auth registration/login, admin member management, forum, drive E2EE,
    video upload/share/playback, chess/games, trading/economy, security health,
    ComfyUI workflow CRUD, 1366x768 and 390x844 module tabs, visual workflow
    drag/edge checks, Civitai missing-key guard, and chess probe.
- Platform center Playwright after QA-harness isolation:
  `/tmp/hackme_web_platform_round9_20260603/reports/qa/playwright_platform_health_check_20260603T021548Z.json`
  - PASS; `browser_errors: []`.
  - Covered job center, notifications, share management, trading asset overview,
    jobs/shares/economy at 390x844, 768x1024, 1366x768, and root
    health/capacity/env operations at the same viewports.
- Browser video compatibility:
  `/tmp/hackme_web_browser_video_round3_20260603/reports/qa/browser_video_compat.json`
  - PASS for Chromium desktop, Chromium mobile, Firefox desktop, Firefox mobile.
  - Validated shared Standard realtime proxy fallback through the actual
    playback descriptor and `/realtime-proxy` bytes.
- WebKit environment check:
  `/tmp/hackme_web_browser_video_webkit_env_20260603/reports/qa/browser_video_compat.json`
  - Expected host-level failure only: Playwright WebKit cannot start because
    `libicudata.so.70` is missing on this machine.
  - The probe now records the launch failure in JSON/Markdown instead of
    crashing before report generation.
- Member/account probe:
  `/tmp/hackme_web_member_round_20260603/member_probe/member_probe.json`
  - PASS; findings `[]`.
  - Covered root/test login, storage upload/previews, E2EE preview denial,
    malformed E2EE handling, share download, album password, localhost remote
    download blocking, BT task handling, video upload/password share/playback,
    unsupported video E2EE mode, trading fee equality, and root reserve
    allocation/verification.
- ComfyUI visual workflow builder:
  `python3 scripts/testing/playwright_comfyui_workflow_builder_check.py`
  - PASS; render, drag, wire, edge deletion, JSON import, and mobile layout.
- Video streaming pytest:
  `scripts/testing/pytest_in_tmp.sh -q tests/video/streaming/test_video_streaming.py`
  - PASS; 68 tests.
- Targeted profile/storage pytest:
  `tests/users/test_profile_friends.py::test_profile_and_target_options_accept_sqlite_row_actor`
  and
  `tests/storage/test_cloud_drive_attachments.py::test_storage_admin_summary_sync_and_root_purge`
  - PASS.
- Full profile pytest:
  `tests/users/test_profile_friends.py`
  - PASS; 8 tests.
- Static checks:
  - `node --check public/js/00-core.js`
  - `node --check public/js/40-auth-users.js`
  - `node --check public/js/50-admin.js`
  - `python3 -m py_compile routes/videos.py scripts/testing/playwright_browser_video_compat.py tests/video/streaming/test_video_streaming.py scripts/testing/playwright_platform_health_check.py`
  - `git diff --check`

## Residual Notes

- A full repository pytest run is not claimed as green. Earlier broad pytest
  runs included stale expectations and environment-dependent cases outside the
  fixed failures.
- The storage attachment suite still has likely test-drift/config mismatches:
  fake `.mp3` upload now returns 415 under native-playback hardening, and remote
  download concurrency tests assume two concurrent tasks while current defaults
  document a 1/1 cap.
- Civitai live checks were skipped by design because no live API key was
  configured.
- WebKit browser-video coverage is blocked by the local machine dependency
  `libicudata.so.70`, not by a reproduced application route or UI failure.

## Current Verdict

No reproducible application defect remains from the QA probes above after the
five pushed fix batches. The remaining non-pass items are environment
limitations or stale test expectations documented in this report.
