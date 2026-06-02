# Video Streaming Architecture

This document is the formal Phase C streaming design for large media playback.
It is a canonical engineering reference, not a research draft.

Read this after:

- [VIDEO_PLATFORM.md](VIDEO_PLATFORM.md)
- [WEB.md](../WEB.md)
- [For_developer.md](../For_developer.md)

Current status:

- Video Platform v1 is implemented.
- Phase C-1 HLS streaming foundation is implemented here.
- Public/unlisted video publishes and `server_encrypted` media now try to
  prepare HLS derivatives automatically, while owners can manually retry
  preparation from the watch page.
- Prepared HLS supports multi-audio media by emitting alternate audio playlists
  and supports larger subtitle sets through extracted WebVTT subtitle tracks.
  Shared video and shared-file preview routes must authorize audio playlist and
  subtitle child resources the same way they authorize video playlists.
- Prepared HLS workers now expose Premium queue/resource state through Job
  Center metadata. Large HLS jobs use host-global file slots, controlled by
  `HACKME_MEDIA_HLS_MAX_CONCURRENT`, `HACKME_MEDIA_HLS_LOCK_DIR`,
  `HACKME_MEDIA_HLS_SERIALIZE_ALL`, and `HACKME_MEDIA_HLS_SERIALIZE_MIN_BYTES`,
  so preprocessing cost stays bounded across server workers.
  `scripts/testing/hls_worker_slot_probe.py` exercises this with two real
  workers and validates the Job Center queue stages.
  `scripts/testing/hls_premium_sizing_probe.py` measures wall time, ffmpeg/worker
  CPU, RSS, derivative bytes, and Job Center worker metrics so operators can set
  Premium concurrency from evidence instead of guessing.
- Premium HLS storage can be tuned with
  `HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE`. The default `always` keeps the
  original HLS rendition. `never` / `storage_saver` skips it when q480/q720 or
  another configured quality ladder is available, reducing derivative storage
  while keeping multi-audio, subtitles, and share authorization behavior intact.
- Premium HLS profiles can set the common storage/quality policy in one knob:
  `HACKME_MEDIA_HLS_PROFILE=full|storage_saver|mobile_saver`. `mobile_saver`
  defaults to q480 and 128k HLS audio, while explicit
  `HACKME_MEDIA_HLS_QUALITY_HEIGHTS` or `HACKME_MEDIA_HLS_AUDIO_BITRATE` still
  override the profile.
- Playback payloads expose the active Premium profile policy through
  `service_policy.premium_hls_profile_policy` and the prepared-HLS
  `streaming_options[].profile_policy`, so frontend/customer-support copy can
  explain fee differences without hard-coding them.
- The same policy payload includes the inferred prepared asset profile,
  `asset_profile_candidates`, `profile_matches_asset`, `profile_drift`, and
  `rebuild_recommended`. This distinguishes "current service tier setting" from
  "what this file was actually prepared as" after operators change Premium
  profile knobs.
- `scripts/testing/hls_premium_profile_matrix_probe.py` compares the same source
  fixture across Premium profiles and emits the storage/CPU/RSS/wall-time matrix
  used for pricing and capacity decisions.
- Standard realtime proxy endpoints are implemented behind
  `HACKME_MEDIA_REALTIME_PROXY_ENABLED`. They copy the source video when
  possible, transcode only the selected audio track to AAC stereo, strip
  subtitles/data/metadata tracks, and enforce timeout plus runtime-wide
  concurrency controls. With `HACKME_RUNTIME_DIR` or
  `HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR`, the concurrency cap is host-global
  across workers through file slots; without that runtime context it falls back
  to process-local slots. The QA probe supports both Flask and gunicorn modes
  and validates that gunicorn workers share the same realtime-proxy slot pool.
- Later Phase C stages are still not fully implemented yet.

## Problem Statement

The current platform can already stream plain media with HTTP range support, but
large files still show noticeable startup delay in one important path:

- `standard_plain`: direct file streaming is acceptable for compatible Cloud
  Drive preview and downloads, but not as a published-video playback mode.
- `server_encrypted`: the current implementation decrypts the entire file into
  memory before range slicing, which creates large startup latency and memory
  pressure for big video and audio files.
- `e2ee`: the server cannot safely decrypt plaintext, so it cannot transparently
  transcode or package a normal server-side streaming derivative. E2EE videos
  may still be published as `持連結可看` browser-side decrypted shares, but that
  path remains distinct from HLS.

Because of these constraints, browser-side buffering alone is not enough.

## Goals

Phase C should provide:

1. Lower startup latency for large video playback.
2. Seek behavior that does not require full-file decrypt first.
3. Adaptive streaming support for long/high-bitrate videos.
4. A formal split between:
   - user-private source files
   - server-streamable derivatives
   - strict E2EE files
5. A storage/layout model that stays compatible with snapshot/restore, quota,
   audit, and access control.

## Non-Goals

Phase C does not automatically mean:

- livestreaming
- LL-HLS / WebRTC
- full CDN integration on day one
- E2EE files becoming transparently streamable by the server
- replacing all direct file preview with HLS

Small media can continue using direct HTTP range streaming where it remains
simpler and cheaper.

## Current Baseline

Today:

- Cloud Drive preview for plain audio/video uses direct preview URLs.
- Video Platform v1 streams media through `/api/videos/<id>/stream`.
- E2EE files are intentionally blocked from server-streamed video publishing.
  They may instead use browser-side share playback with a wrapped file key
  envelope and a fragment-held viewer key.
- `server_encrypted` files can be streamed, but the current path is expensive
  for large files because it decrypts the whole object before serving ranges.
- Phase C-1 already adds:
  - `POST /api/media/<file_id>/prepare-stream`
  - `GET /api/media/<file_id>/stream-status`
  - `GET /api/videos/<id>/playback`
  - `GET /api/videos/<id>/hls/master.m3u8`
  - `GET /api/videos/<id>/hls/<variant>/playlist.m3u8`
  - `GET /api/videos/<id>/hls/<variant>/<segment>`
- Current implementation packages prepared HLS variants and lets the frontend
  prefer native-HLS playback when derivatives are ready. Browsers without native
  HLS support use hls.js where available.
- Auto-prepare is best-effort and runs through an external HLS worker. A failed
  derivative must not block the base video publish action. When HLS workers are
  queued, Job Center should show `waiting_worker_slot`; after a slot is acquired
  it should show `worker_slot_acquired` and then `transcoding`.

This design keeps those v1 rules, but adds a media-derivative pipeline for
larger assets.

## Why HLS

Normal large streaming platforms usually do not rely on a single giant `mp4`
response. The common pattern is:

- package video into many short segments
- provide a playlist manifest
- let the player fetch only the next few segments
- optionally switch bitrate based on current network conditions

For this project, the default target should be:

- HLS
- CMAF / fragmented MP4 segments (`.m4s`)
- multiple quality variants for larger videos
- conservative default quality generation: `480p` then `720p`; `1080p` is
  opt-in through `HACKME_MEDIA_HLS_QUALITY_HEIGHTS` because AV1 sources can be
  smaller than a generated 1080p H.264 derivative
- playback defaults to `720p` when available, then falls back to `480p` on poor
  network conditions; original quality remains available as a user-selected
  option unless the storage-saver original-rendition policy is enabled
- generated quality variants are compared against the original HLS package, or
  the original upload size when the original HLS rendition is skipped; if a
  lower-quality derivative is larger than that baseline, it is deleted and
  omitted from the quality selector

This gives better startup behavior, easier seeking, and a cleaner long-term path
to CDN or cache acceleration.

## Customer Service Tiers

The customer-facing product has three streaming service tiers. The detailed
customer explanation lives in
[VIDEO_STREAMING_SERVICE_TIERS.md](VIDEO_STREAMING_SERVICE_TIERS.md).

| Tier | Playback route | Cost driver | Operational note |
| --- | --- | --- | --- |
| Cloud Drive Basic | direct browser preview | file I/O and bandwidth only | only for browser-compatible personal-file preview/download; not a Video Platform publish mode |
| Video Standard | realtime proxy / transwrap | per-viewer ffmpeg CPU | fallback while HLS is unavailable; protected by feature flag, strict concurrency limits, timeout cleanup, and normal video/share ACL |
| Video Premium | prepared HLS | preprocessing CPU, derivative storage, cache invalidation, cleanup | default for public/shared published media, multi-audio, multi-subtitle, stable seek, mobile playback, and future CDN/cache acceleration |

`lcd_OS` is a useful reference for the Standard route: it keeps CPU low by
copying video when possible and only transcoding the selected audio track to a
browser-friendly stream. That approach is intentionally different from
prepared HLS: it saves storage and setup time, but every viewer creates live
server work and seek behavior is approximate.

## Mode Matrix

### `standard_plain`

Allowed:

- direct range preview
- HLS derivative generation
- direct Cloud Drive download

Recommended behavior:

- keep direct range playback for small media
- generate HLS derivatives for large published video

### `server_encrypted`

Allowed:

- server-side decrypt in controlled jobs
- server-generated HLS derivative pipeline

Required change:

- do not keep the current "decrypt whole file, then slice" model for large
  playback
- instead, generate and serve streamable derivatives

Recommended behavior:

- decrypt inside a background worker
- transcode / package to HLS
- store the resulting derivatives in server-controlled protected storage
- serve segments with per-request ACL checks

### `e2ee`

Not allowed:

- transparent server-side transcoding of the original file
- normal server-side HLS from the original ciphertext
- server access to the raw file key, share key, URL fragment key, or decrypted
  media bytes
- server-side `ffmpeg` packaging of strict E2EE originals into 480p / 720p /
  1080p derivatives

Recommended product rule:

- strict E2EE files remain unavailable for server-streamed video / HLS
- unlisted E2EE video shares may use browser-side decryption only:
  - owner enters the original E2EE password once at publish time
  - browser unwraps the original file key locally
  - browser generates a random 256-bit share key
  - browser re-wraps the file key with AES-GCM and stores only the wrapped
    envelope on the server
  - the share key travels only in the URL fragment `#vk=...`
- strict E2EE can achieve quota-exempt streaming behavior through browser-side
  encrypted streaming bundles: the service cache is quota-exempt, but the server
  still must not decrypt and transcode into 480p / 720p / 1080p variants.

That keeps the original E2EE promise intact instead of silently downgrading
security while still allowing users to share E2EE media.

## Strict E2EE Multi-Quality Boundary

Strict E2EE multi-quality is allowed only in this shape:

```text
publisher browser/client
  decrypts or reads the local original
  transcodes 720p / 480p locally
  encrypts each derivative locally
  uploads encrypted derivative bundles + manifests

server
  stores encrypted bundles and manifests only
  never receives plaintext media
  never receives raw file keys or share keys
  never runs ffmpeg on the strict E2EE original
```

The platform therefore distinguishes three runtime modes:

| Mode | Server HLS / Transcode | Playback | Notes |
| --- | --- | --- | --- |
| `plain/server_encrypted` | allowed | HLS / direct fallback according to policy | server-encrypted files may be decrypted only inside controlled worker flow |
| `strict_e2ee_original_only` | disabled | encrypted original / E2EE Streaming v2 | baseline strict E2EE mode |
| `strict_e2ee_derivatives` | disabled for server; client-side only | encrypted client-generated variants | implemented for browser-generated 720p / 480p encrypted variants when supported |

Derivative metadata must be explicit and auditable. Each encrypted derivative
needs at least:

- parent video / uploaded file id
- resolution and intended bitrate
- encrypted chunk manifest
- encrypted content hash or encrypted bundle digest
- parent-original ciphertext digest reference, not a plaintext media digest
- created-by user and created-at timestamp

The server must reject normal stream preparation for strict E2EE media. The
expected response for server HLS attempts is a visible error such as
`strict_e2ee_server_transcode_disabled`, not silent fallback to server
transcode.

Playback policy:

- plain/server-encrypted media can default to 720p, fall back to 480p, and
  expose original quality when available
- strict E2EE without client-generated derivatives uses original encrypted
  streaming
- strict E2EE with encrypted derivatives may default to 720p and fall back to
  480p, but missing derivatives must fall back to the encrypted original instead
  of failing playback

Quota policy:

- original uploads count against the user Cloud Drive quota
- HLS derivatives and strict E2EE encrypted streaming bundles are service cache
  and do not count against Cloud Drive quota by default
- root can enable/disable strict E2EE browser-generated derivatives, choose the
  allowed quality heights, decide whether oversized derivatives are rejected,
  and override whether those encrypted derivatives are quota-exempt
- generated derivatives are bound to the original and are removed when the
  original is permanently purged from Cloud Drive trash

Implemented E2EE derivative endpoints:

```text
POST /api/media/<file_id>/e2ee-stream-v2/variants/<variant_name>
GET  /api/videos/<id>/e2ee-stream-v2/variants/<variant_name>/manifest
GET  /api/videos/<id>/e2ee-stream-v2/variants/<variant_name>/chunks/<chunk_index>
GET  /api/videos/shared/<token>/e2ee-stream-v2/variants/<variant_name>/manifest
GET  /api/videos/shared/<token>/e2ee-stream-v2/variants/<variant_name>/chunks/<chunk_index>
```

The server accepts only encrypted Streaming v2 bundle data and manifest
metadata for root-allowed derivative heights such as `q720` / `q480`. By
default it rejects oversized encrypted derivatives that are not smaller than
the original upload, so larger "compressed" versions are not shown as
selectable qualities.

The browser-side E2EE derivative step is visible in the Job Center as a local
task. It reports local password unlock, browser transcode, local encryption,
original encrypted stream upload, and derivative upload stages. Because this
work happens in the browser, a page reload cannot continue the same local file
handle; the next visit shows a clear reminder to re-select the original file
and run the publish / derivative step again.

Required failure messages for client-side E2EE derivative creation:

- wrong E2EE password
- browser codec / MediaRecorder / WebCodecs not supported
- browser memory pressure or storage pressure
- local transcode interrupted
- encrypted derivative upload failed

## Storage Model

Derivatives should live under the existing runtime storage root, not in a new
top-level filesystem namespace.

Recommended layout:

```text
runtime/storage/media_derivatives/<uploaded_file_id>/
  master.m3u8
  1080p/
    playlist.m3u8
    init.mp4
    seg_00001.m4s
    seg_00002.m4s
  720p/
    playlist.m3u8
    init.mp4
    seg_00001.m4s
  audio_01_jpn/
    playlist.m3u8
    init.mp4
    seg_00001.m4s
  subtitles/
    sub01_jpn.vtt
```

This keeps:

- snapshot/restore compatibility
- storage quota accounting compatibility
- user quota accounting based on the original uploaded file only; HLS
  derivatives and E2EE streaming bundles are service cache and do not count
  against the user's Cloud Drive quota
- predictable cleanup behavior
- one canonical storage root

## Data Model

Recommended new tables:

### `media_stream_assets`

- `id`
- `uploaded_file_id`
- `source_mode`
- `status`
- `master_manifest_path`
- `duration_seconds`
- `created_at`
- `updated_at`

### `media_stream_variants`

- `asset_id`
- `name`
- `media_kind`: `variant` or `audio`
- `label`
- `language`
- `stream_index`
- `is_default`
- `width`
- `height`
- `bitrate`
- `codec`
- `playlist_path`

Audio tracks are stored in the same table as variants with
`media_kind='audio'` so the existing playlist/segment metadata path can serve
both video renditions and alternate audio renditions.

### `media_stream_segments`

- `variant_id`
- `sequence_number`
- `path`
- `duration_seconds`
- `byte_size`

### `media_stream_subtitles`

- `asset_id`
- `name`
- `label`
- `language`
- `codec`
- `path`
- `is_default`
- `is_forced`

### `media_stream_jobs`

- `asset_id`
- `job_type`
- `status`
- `started_at`
- `finished_at`
- `error_message`

## Pipeline

### Ingest and Preparation

1. User uploads or publishes a media file.
2. Server inspects metadata with `ffprobe`.
3. Policy decides whether Cloud Drive preview may direct-play the original, or
   whether published video needs a stream derivative.
4. If needed, a background job creates HLS output.

Suggested initial thresholds:

- file size over `200 MB`
- or duration over `10 minutes`
- or bitrate above a configurable threshold

### Derivative Build

For streamable assets:

1. open source file
2. decrypt if `server_encrypted`
3. transcode / package with `ffmpeg`
4. emit:
   - master playlist
   - variant playlists
   - alternate audio playlists for multi-audio or non-browser-native audio
     sources
   - extracted WebVTT subtitle tracks where the source subtitle codec is
     supported
   - CMAF init + media segments
5. write derivative metadata into DB
6. mark stream asset ready

### Playback

When a client opens a video page:

1. server resolves ACL
2. server chooses playback mode:
   - `hls`
   - `realtime_proxy`
   - `waiting_stream` / `unavailable`
3. frontend uses:
   - `hls.js` or native HLS support for HLS mode
   - native `<video>` only for Cloud Drive direct preview, not Video Platform

## API Shape

Implemented baseline:

### Preparation and status

```text
POST /api/media/<file_id>/prepare-stream
GET  /api/media/<file_id>/stream-status
```

### Video playback description

```text
GET /api/videos/<id>/playback
```

Current baseline response:

```json
{
  "ok": true,
  "mode": "hls",
  "master_url": "/api/videos/123/hls/master.m3u8",
  "stream_url": "",
  "fallback_url": "",
  "direct_fallback_allowed": false,
  "source_mode": "server_encrypted",
  "variants": [{"name": "q720", "label": "720p"}],
  "audio_tracks": [{"name": "audio_01_jpn", "label": "音軌 1 (jpn)"}],
  "subtitles": [{"name": "sub01_zh", "label": "中文"}],
  "service_policy": {
    "customer_selectable_modes": ["realtime_proxy", "prepared_hls"],
    "default_mode": "prepared_hls",
    "recommended_mode": "prepared_hls",
    "multi_track_recommended_mode": "prepared_hls",
    "fee_model": "standard_premium",
    "fee_difference_reason": "服務費差異來自每位觀看者即時 CPU、預處理 worker CPU、衍生檔儲存與快取清理成本。Direct 僅保留給 Cloud Drive 相容檔案預覽。"
  },
  "streaming_options": [
    {
      "mode": "realtime_proxy",
      "service_tier": "standard",
      "fee_level": "middle",
      "billing_basis": "per_viewer_cpu",
      "available": true,
      "cost_drivers": ["ffmpeg_process_per_viewer", "audio_transcode_cpu", "process_cleanup"],
      "media_support": {"multi_audio": "selected_track_only", "multi_subtitle": "external_or_browser_dependent"}
    },
    {
      "mode": "prepared_hls",
      "service_tier": "premium",
      "fee_level": "highest",
      "billing_basis": "preprocess_storage_cache",
      "available": true,
      "cost_drivers": ["worker_cpu", "derivative_storage", "segment_indexing", "cache_lifecycle", "cleanup"],
      "media_support": {"multi_audio": true, "multi_subtitle": true}
    }
  ]
}
```

### HLS routes

```text
GET /api/videos/<id>/hls/master.m3u8
GET /api/videos/<id>/hls/<variant>/playlist.m3u8
GET /api/videos/<id>/hls/<variant>/<segment>
```

## Encryption Strategy

### Plain media

- store plain source file as today
- generate plain or access-controlled HLS derivative

### Server-encrypted media

Short answer:

- decrypt in the background worker
- never require whole-file decrypt during end-user playback

Recommended derivative policy:

- original upload remains `server_encrypted`
- derivative segments are stored in protected server-side storage
- derivative access remains ACL-gated

If stronger at-rest protection is still required for derivatives, use a
segment-friendly server-side encryption scheme:

- fixed-size segments
- each segment independently encryptable/decryptable
- metadata keeps nonce/tag per segment

That allows on-demand decrypt of only the requested segment instead of the full
asset.

### E2EE media

Do not pretend the server can safely package E2EE media into HLS while keeping
the same trust boundary.

Approved product paths should be:

1. keep the file strict E2EE and disallow server-streamed publishing
2. or let the user explicitly create a separate streamable derivative under a
   different privacy mode

This preserves the E2EE behavior boundary.

## Frontend Strategy

### Cloud Drive

- use direct preview only when backend metadata marks the file browser-native
- use realtime proxy or existing prepared HLS for browser-hostile media
- do not automatically transcode a customer's personal files unless the operator enables it

### Video Platform

- prefer HLS for published videos
- fall back to realtime proxy while HLS is unavailable or rebuilding
- do not expose direct `/stream` as a selectable published-video mode

Suggested player behavior:

- start with poster + manifest fetch
- load first segments only
- let browser/player buffer forward naturally

## Operational Controls

Root-configurable knobs should include:

- enable/disable HLS derivative generation
- max simultaneous transcode jobs
- derivative segment duration
- bitrate ladder presets
- whether 1080p derivatives are generated automatically or only on demand
- minimum file size / duration before HLS generation
- whether Cloud Drive preview may use derivatives
- whether users may create streamable derivatives from non-plain source modes
- max extracted audio tracks and subtitle tracks for prepared streaming
- original HLS rendition policy:
  `HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE=always|auto|never`
- profile presets and audio bitrate:
  `HACKME_MEDIA_HLS_PROFILE`, `HACKME_MEDIA_HLS_AUDIO_BITRATE`
- realtime proxy concurrency and per-viewer CPU limits before that tier is
  exposed in production UI
- Premium HLS worker slot count from sizing evidence. On this host, Scarlet
  60s and 180s real-file probes with `jobs=2 / max=2` completed successfully,
  while `jobs=3 / max=2` confirmed the third job queues instead of starting a
  third ffmpeg. Similar 12-core deployments may use
  `HACKME_MEDIA_HLS_MAX_CONCURRENT=2`; keep `1` for smaller hosts or when
  CPU/IO monitoring is not available.

## Security Notes

- segment and manifest routes must reuse the same video/file ACL checks
- do not expose raw storage paths
- do not cache private content without scoped authorization
- signed URLs may be added later if CDN/front-cache support is needed
- audit should record derivative generation, playback mode, and privacy-mode
  transitions

## Rollout Plan

### Phase C-1

- metadata tables
- background media job
- plain-video HLS generation
- `server_encrypted` decrypt-and-package path
- video page playback decision route
- native-HLS-or-direct frontend fallback

### Phase C-2

- multi-variant ABR ladder with bitrate-capped ffmpeg output
- explicit queue/backoff and retry policy
- remove any remaining whole-file decrypt bottlenecks from non-derivative
  playback paths

### Phase C-3

- Cloud Drive large-video preview can reuse stream derivatives
- derivative lifecycle cleanup and quota accounting
- optional `hls.js` support for non-native browsers

### Phase C-4

- explicit user flow for "create streamable derivative" from E2EE-adjacent
  content
- no silent downgrade of strict E2EE assets

## Acceptance Criteria

Phase C should not be considered complete until:

1. large video startup latency is materially lower than direct whole-file
   decrypt playback
2. seeking no longer implies full source decrypt for `server_encrypted` assets
3. E2EE behavior remains explicit and honest
4. derivative files are covered by snapshot/restore and cleanup rules
5. video ACL and private/unlisted rules still apply to all manifests and
   segments

## Related Documents

- [VIDEO_PLATFORM.md](VIDEO_PLATFORM.md)
- [WEB.md](../WEB.md)
- [For_developer.md](../For_developer.md)
- [12_TROUBLESHOOTING.md](../12_TROUBLESHOOTING.md)
