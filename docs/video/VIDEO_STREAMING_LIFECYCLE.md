# Video Streaming Lifecycle Policy

This document is the operator-facing source of truth for the current playback
and derivative-retention policy. It intentionally separates Cloud Drive preview
from the public video platform because those two surfaces have different user
expectations and different cost profiles.

## Default Playback Design

### Cloud Drive Preview

Cloud Drive is the user's own file manager, so preview should be conservative
and avoid changing the uploaded file unless the operator explicitly enables a
background derivative workflow.

Default order:

1. **Direct browser preview** for files the browser can realistically play by
   itself, such as normal MP4/WebM or common audio formats.
2. **Realtime proxy / transwrap** when direct playback is not browser-safe but
   the server can expose a bounded ffmpeg proxy.
3. **Prepared HLS** only when an HLS asset already exists, or when the operator
   has enabled Cloud Drive automatic HLS preparation.

Direct preview is deliberately strict. MKV, AVI, TS, E-AC-3-only audio, and
other browser-hostile combinations are reported as unavailable instead of being
shown as a broken direct stream. Download remains separate from preview.

### Video Platform

The Video Platform is for publishing media to other viewers. It optimizes for
stable playback, seeking, mobile compatibility, authorization on child assets,
and predictable server load.

Default order:

1. **Prepared HLS** when ready.
2. **Realtime proxy / transwrap** while HLS is unavailable or being rebuilt.
3. **Waiting / unavailable state** when neither path can be served.

The Video Platform does **not** expose Direct as a selectable playback mode.
Direct remains a Cloud Drive preview/download escape hatch, not a publishing
service mode. This avoids the historical failure mode where the simplest-looking
option produced the most playback bugs for MKV, multi-audio, embedded subtitle,
server-encrypted, and mobile cases.

## HLS Retention Policy

Prepared HLS costs CPU at build time and disk space afterwards. The platform
therefore keeps useful derivatives for active videos and prunes cold assets.
The cleanup job runs through storage maintenance and is root-configurable.

Default thresholds:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `video_hls_cold_cleanup_enabled` | `true` | Enables the automatic HLS retention pass. |
| `video_hls_protect_days_after_upload` | `7` | New uploads are not pruned during this window. |
| `video_hls_warm_plays_30d_below` | `20` | Below this 30-day play count, the video can be treated as warm/low-traffic. |
| `video_hls_cold_plays_30d_below` | `5` | Below this 30-day play count plus no recent plays, the video can be treated as cold. |
| `video_hls_cold_no_play_days` | `14` | No plays for this many days qualifies for cold handling. |
| `video_hls_very_cold_no_play_days` | `90` | No plays for this many days qualifies for very-cold handling. |
| `video_hls_warm_keep_variants` | `original,480p` | Warm videos keep original quality plus the mobile floor. |
| `video_hls_mobile_floor_keep_variants` | `480p` | Cold videos keep at least mobile-friendly 480p. |
| `video_hls_cold_cleanup_max_assets_per_run` | `25` | Upper bound per maintenance run. |
| `video_hls_rebuild_plays_24h` | `3` | Rebuild threshold for a pruned video that becomes active again. |
| `video_hls_rebuild_plays_7d` | `5` | Weekly rebuild threshold for a pruned video that becomes active again. |

Current cleanup actions:

| State | Action |
| --- | --- |
| Protected new upload | Do nothing. |
| Warm, low-traffic video | Keep `video_hls_warm_keep_variants`; remove larger/extra renditions. |
| Cold video | Keep `video_hls_mobile_floor_keep_variants`; by default this is 480p for mobile. |
| Very-cold public/unlisted video or active share | Keep the mobile floor so links still play without realtime proxy. |
| Very-cold private video with no active share | Delete prepared HLS assets; the original Cloud Drive file remains untouched. |

Audio playlists and subtitle tracks are retained when any video rendition is
kept. The cleanup job will not prune a ready HLS asset if none of the requested
keep variants exist, because deleting every playable rendition by accident is
worse than saving disk space.

## Rebuild Policy

The rebuild thresholds are root settings because the correct value depends on
hardware and traffic shape. They are policy inputs used to decide when a pruned
video has become active enough to justify rebuilding HLS. Until an automatic
requeue worker is wired to these counters, operators should treat them as the
standard thresholds for manual or scheduled rebuild tooling.

Recommended default behavior once auto-rebuild is enabled:

- If a pruned video reaches `video_hls_rebuild_plays_24h` in 24 hours, enqueue
  HLS preparation.
- If it reaches `video_hls_rebuild_plays_7d` in 7 days, enqueue HLS preparation.
- While rebuilding, desktop clients may use realtime proxy; mobile clients
  should prefer the retained 480p HLS floor when available.

## Root Operations

Root can tune these settings from the admin settings page under service billing
and video policy. Use lower thresholds on small disks or low-power hosts; use
higher thresholds when storage is cheap and user experience matters more than
space savings.

Operational rules:

- Do not enable Direct as a Video Platform playback mode.
- Do not transcode Cloud Drive originals just because they were uploaded.
- Do prepare HLS by default for published videos.
- Do keep at least 480p HLS for cold public/shared videos so mobile playback has
  a low-cost path.
- Do delete all HLS derivatives only for very-cold private videos without active
  shares.
