# Video Reference Set

Use this folder for deep video/media architecture docs. Entry guides remain in
[04_USER_GUIDE.md](../04_USER_GUIDE.md), [05_FEATURES_OVERVIEW.md](../05_FEATURES_OVERVIEW.md),
and [docs/README.md](../README.md).

- [VIDEO_PLATFORM.md](VIDEO_PLATFORM.md): detailed video module reference
- [VIDEO_STREAMING_ARCHITECTURE.md](VIDEO_STREAMING_ARCHITECTURE.md): HLS / encrypted-media streaming design
- [VIDEO_STREAMING_SERVICE_TIERS.md](VIDEO_STREAMING_SERVICE_TIERS.md): customer-facing Cloud Drive direct / video realtime proxy / prepared HLS service-tier explanation
- [VIDEO_STREAMING_LIFECYCLE.md](VIDEO_STREAMING_LIFECYCLE.md): default playback, cold HLS pruning, and root-tunable retention thresholds

Current operator notes:

- Video publish controls are button-opened, not a permanently visible card.
- Cloud Drive preview may use direct browser playback for compatible files,
  but published videos use prepared HLS first and realtime proxy as fallback.
  Direct is not a selectable Video Platform playback mode.
- Multi-audio and multi-subtitle videos need the prepared HLS path for the most
  reliable customer experience. Share video and shared-file preview routes must
  keep applying authorization to HLS variants, audio playlists, and subtitles.
- HLS preparation should be represented as a background job and handled by the
  external worker path; the main Flask server must not synchronously decode or
  package large media.
- Premium HLS worker concurrency should be sized with
  `scripts/testing/hls_premium_sizing_probe.py`. Use generated fixtures for
  quick regression checks and trimmed real customer-like sources before raising
  `HACKME_MEDIA_HLS_MAX_CONCURRENT` in production. Current Scarlet 60s/180s
  probes support `2` on the tested 12-core host; smaller hosts should stay at
  `1` unless monitored.
- Premium storage-saver mode is controlled by
  `HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE=never`; it skips the original HLS
  rendition when generated q480/q720 variants exist, which reduced the Scarlet
  60s derivative multiplier from 2.917x to 1.334x in the current probe.
- Premium presets can be applied with
  `HACKME_MEDIA_HLS_PROFILE=full|storage_saver|mobile_saver`. Current Scarlet
  60s `mobile_saver` probe reduced `jobs=2 / max=2` derivative multiplier to
  0.482x while keeping HLS, multi-audio, subtitles, and share authorization.
- Playback payloads also report the inferred prepared asset profile and
  `profile_drift` / `rebuild_recommended`, so operators can see when existing
  HLS derivatives were produced under an older Premium policy after a profile
  change.
- Use `scripts/testing/hls_premium_profile_matrix_probe.py` when comparing
  Premium profile pricing or capacity. It reuses one source fixture and emits a
  full/storage_saver/mobile_saver matrix instead of relying on hand-assembled
  probe runs.
- Videos that are still preparing HLS should not sit in the public video list
  as a permanent "preparing" item. Show the uploader a processing notice and
  send a notification when the derivative is ready.
- Share management is the canonical place to edit file / album / video share
  options. "My videos" share actions should hand off to Share Management so
  the owner can continue setting password, expiry, max views, preview
  permission, and targeted access.
- Strict E2EE Streaming v2 remains browser-decrypt: the server stores and
  serves encrypted manifest/chunks only, and never accepts raw `vk`, raw file
  key, or E2EE password.
