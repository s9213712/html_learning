# AI Agent Function Audit: Downloader, Albums, Media Transcoding

Date: 2026-06-22
Target: `https://127.0.0.1:54384`
Artifact: `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_media_downloader_probe_v2.json`

## Result

No blocking product bug was reproduced in the underlying downloader, album, or media/HLS stack. The confirmed AI Agent boundary remains a feature gap: the Agent has no write tools for BT/Direct downloader, album CRUD, video upload, HLS rebuild/transcode, or subtitles. It rejected unsupported tool names with `400` and did not silently claim success in chat.

## Coverage Matrix

| Case | Area | Result | Evidence |
| --- | --- | --- | --- |
| MDT-01 | Downloader / albums / video APIs | Pass | `/api/cloud-drive/remote-download/capabilities`, `/tasks`, `/api/storage/albums`, `/api/videos`, `/api/videos/manage` all returned `200`. |
| MDT-02 | Downloader safety and mode validation | Pass | `file:///etc/passwd` rejected `400`; magnet through direct mode rejected `400`; HTTP URL through torrent mode rejected `400`. |
| MDT-03 | Albums | Pass | Album create/detail/update succeeded; smart organize succeeded and created `/Media` album entry. |
| MDT-04 | Video upload and HLS | Pass | 4.8 KB valid MP4 uploaded; video id `2`; upload job and HLS job appeared in Job Center; HLS job succeeded with `master.m3u8`, one segment, and playback `mode=hls`. |
| MDT-05 | AI Agent tool boundary | Expected gap | 13/13 downloader/album/media tool candidates rejected as unsupported; listed Agent tools remain unrelated to downloader/media. |
| MDT-06 | Natural-language request | Expected gap | Agent response in `0.567s`; no downloader/album/video write request was made; response explicitly said it cannot perform those actions yet. |
| MDT-07 | Member permissions | Pass | Member can read capabilities/albums but gets `403` for write-tools endpoints. |
| MDT-08 | Response health | Pass | Empty messages `0`; adjacent repeats `0`; total messages `2`; response `0.567s`. |

## Ability Boundary

| Capability | Site Backend | AI Agent Today | Required Direction |
| --- | --- | --- | --- |
| Direct download | Available, with SSRF/mode guards | Not exposed | Add root-scoped tool for create/list/pause/resume/cancel/recover tasks with URL validation surfaced before execution. |
| BT/magnet download | Available through aria2 backend; Transmission RPC unavailable in this environment | Not exposed | Add magnet and `.torrent` task tools; expose backend/capability state so Agent can explain aria2 vs Transmission. |
| Albums | CRUD and smart organize work | Not exposed | Add album create/update/add/remove/list tools with owner and visibility constraints. |
| Video upload/publish | Works through multipart upload | Not exposed | Browser Agent cannot upload arbitrary local files without a selected file handle; define tool around existing cloud file IDs or user-confirmed attachments. |
| HLS transcode/rebuild | Background HLS worker and Job Center tracking work | Not exposed | Add HLS status/retry/rebuild tools and progress polling; keep long jobs non-blocking like ComfyUI. |
| Subtitles | Route family exists | Not exposed in this probe | Add subtitle upload/shift/list tools after file-handle boundary is designed. |

## Notes

- The probe created a real MP4 fixture with ffmpeg and verified the background HLS job reached `succeeded`; this was not a fake `.mp4` filename smoke test.
- Downloader tests intentionally used invalid/local/mismatched sources to verify safety and mode errors without pulling external content.
- No console errors or page errors were recorded.
