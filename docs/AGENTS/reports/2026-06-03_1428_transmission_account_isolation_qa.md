# Transmission BT Account Isolation QA - 2026-06-03 14:28 CST

## Scope

- Live browser/API QA on `https://127.0.0.1:50931`.
- Transmission-backed magnet downloads submitted through the Cloud Drive UI.
- Two concurrent root downloads and one concurrent admin download.
- Account isolation checks for task lists, completed storage files, direct file endpoints, storage attach, and video publish.

## Findings

No account isolation bypass was reproduced in the tested user-facing endpoints.

Confirmed behaviors:

- Root and admin task lists stayed separated during concurrent BT downloads.
- Root completed files landed as `owner_user_id=1` only:
  - `/爭取歐/concurrent-root-1.mkv`
  - `/爭取歐/concurrent-root-2.mkv`
- Admin `/api/storage/files` returned an empty file list while root had completed files.
- Admin negative access to a root uploaded file was blocked:
  - preview: `403 no_grant`
  - download: `403 no_grant`
  - storage-id download: `404`
  - attach existing root file into admin storage: `400`, `只能加入自己的檔案到 storage`
  - publish root file as admin video: `403`, `cannot publish another user's file`
- Root negative access to the admin remote-download task through the normal user API was blocked with `403`.
- Admin direct access to a root remote-download task was blocked with `404`.

## Fixed During This Pass

- Transmission completed files may be placed in Transmission's global incomplete directory rather than the per-task download directory. The remote downloader now locates completed Transmission file candidates, removes the torrent without deleting local data, stages completed files back into the per-task directory, and then saves them to Cloud Drive.
- Dev CLI remote download concurrency now prefers explicit `test_for_develop.sh --remote-download-global/--remote-download-per-user` values over stale persisted DB settings when those CLI options are used.
- Cloud Drive BT capability UI now retries transient capability failures instead of leaving magnet buttons disabled after temporary backend pressure.
- The existing Transmission setup helper can now disable RPC auth for local dev-only testing and supports passing the option through `test_for_develop.sh`.

## Evidence

- Multi-account Playwright artifact:
  `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/multi_account_bt_probe.json`
- Screenshots:
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/root_after_submit.png`
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/admin_after_submit.png`
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/root_mid_download.png`
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/admin_mid_download.png`
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/root_final.png`
  - `/tmp/hackme_web_multi_account_bt_probe_20260603_round2/admin_final.png`
- Negative isolation probe:
  `/tmp/hackme_account_isolation_probe.py`

## Limitations

- The admin magnet started correctly and remained isolated, but failed after a stall timeout at 4.8% due lack of download progress from peers. This prevented verifying admin completed-file landing for that specific torrent in this run.
- Root completed-file isolation was verified after two successful BT downloads.

## Verification

- `node --check public/js/35-drive.js`
- `python3 -m py_compile routes/files.py services/storage/remote_downloads.py`
- `bash -n test_for_develop.sh scripts/storage/setup_transmission_backend.sh`
- `git diff --check`
- `python3 -m pytest -q tests/storage/test_cloud_drive_attachments.py -k 'remote_download_dev_env_override or transmission or remote_download_tasks_can_run_concurrently_per_user or remote_download_third_task_waits'` -> 6 passed
- `python3 -m pytest -q tests/storage/test_remote_downloads.py` -> 19 passed
