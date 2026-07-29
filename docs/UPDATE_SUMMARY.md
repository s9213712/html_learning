# Update Summary

Release ID: `06_2026.07.29-001`

## 06_2026.07.29-001

- Reworked image editing validation around reviewable artifacts: HF/Diffusers
  product i2i soak and cached standalone i2i probes now retain output images,
  while the ComfyUI matrix covers i2i, inpaint, outpaint, upscale and blend.
  Semantic SAM3 outpaint now validates canvas geometry and alpha before
  delivering only the final server-side composite, preventing intermediate
  background/foreground files or opaque source rectangles from being presented
  as a successful edit.
- Corrected HF/Diffusers img2img capability discovery for regular
  `model_index.json` pipelines and added strict tests for Flux Fill outpaint
  graph construction and semantic-composite failures.
- Restored AI Agent reliability for Windows-hosted local Ollama from WSL with a
  narrowly scoped, credential-free local fallback. Increased the bounded
  browser/planner window so healthy schema-heavy tool planning is not aborted
  before the normal chat budget, and covered it with backend and frontend tests.
- Added server-owned bot protections: DCA upper/lower price bands produce
  auditable skips without disabling protective exits; Workflow bot UTC daily
  run limits are reserved atomically across manual and background scans and
  released only when order placement fails.
- Expanded operational evidence and stress scripts for mixed account traffic,
  BT lifecycle, Cloud Drive streaming, HLS quality, storage, SQLite contention,
  capacity selection, browser acceptance and 24-hour campaign contracts. The
  documented BT multi-IP requirement remains fail-closed until exercised on a
  genuinely multi-address private network.
- Hardened responsive UI, upload/storage paths, backpressure, audit and
  platform-health checks. Operator documentation now separates HF/Diffusers
  from ComfyUI execution and requires visual review for image-edit output.

## 05_2026.07.20-001

- Removed generated image trees from `output/` and `public/generated/`, plus
  residual game evidence `_runtime` reports. Added ignore and pre-push guards
  for generated roots, SQLite sidecars, runtime keys, and nested evidence
  runtime paths.
- Moved the one reusable AI image-edit input into an explicit test fixture and
  redirected AI Agent probe reports/results to the external test-artifact root.
  Isolated test and development copies now exclude generated public images.
- Prevented Python entrypoints and hooks from creating bytecode in source,
  moved CI secret reports to `/tmp`, and corrected the BTC bridge root/runtime
  fallback so standalone use writes to external XDG state.

## 05_2026.07.15-001

- Completed the fail-closed qualification path for the 24-hour operational
  campaign: verified cgroup leaves and namespace confinement, an external
  watchdog, hard-stop state transitions, continuous-active-time accounting,
  exact source freeze, field-complete resource samples, and sealed evidence
  authority are now enforced by the supervisor.
- Added live, terminal-state scenario contracts for AI Agent operations,
  Cloud Drive and long-video/HLS sharing, BT, ComfyUI workflows, trading and
  background trading, governance, restart, backup/restore, incident response,
  mobile/browser UX, and the complete 41-role evidence projection. A result
  cannot be promoted to PASS after a runner or supervisor failure.
- Hardened AI Agent compound read/write intent and reversible cleanup, ComfyUI
  model/input binding and managed-backend ownership, supervised restart and
  snapshot compatibility, artifact reopening, credential scanning, and
  orphan-process/listener detection.

## 05_2026.07.12-003

- Hardened the 24-hour operational campaign harness with verified cgroup v2
  limits, an out-of-scope watchdog, atomic hard stops and dual checkpoints,
  layered readiness, production-security sentinels, source-drift detection,
  field-level resource completeness, strict scenario contracts, and validated
  artifact/gate indexes. Formal execution remains fail-closed until all
  qualification gates and the 60-minute rehearsal have machine evidence.
- Fixed the deep Playwright account-isolation call signature and contained the
  root account/server-management layouts at 360, 390, and 768-pixel viewports,
  including card-style account rows and bounded operation menus.

- Added an explicit pre-handler rejection contract for backpressure. Browser,
  Playwright, stress, capacity, and pentest clients retry/classify a 503 as
  controlled only when the response has
  `X-Hackme-Backpressure-Rejected: 1` and exact `error=server_busy`; normal
  business 503s and edge 429s cannot be mistaken for safe-to-replay writes.
- Replaced Cloud Drive multi-request batch sharing with one SQLite savepoint
  transaction. Creating the unlisted album, share link, and 1-100 file
  memberships either completes fully or rolls back, and network errors no
  longer blindly replay uncertain mutations.
- Enforced accepted friendship for direct strict-E2EE key sharing at both route
  and service boundaries, with denied attempts audited. Game invite, PM/private
  group, and E2EE documentation now matches the backend policy.
- Made admin readiness use the schema-only DB summary while retaining full
  `PRAGMA quick_check` on the dedicated integrity endpoint. Mobile Drive
  breadcrumbs wrap instead of clipping, and silent active-operation failures
  now enter the bounded frontend failure buffer or show a user-facing warning.
- Unified main-app CSS/JS cache busting on `SERVER_RELEASE_ID`, including
  dynamically loaded game modules, so immutable asset caching cannot preserve
  stale frontend code across a release.
- Expanded real-browser coverage to dynamically visit every visible module for
  root and a 390px member session. Root Agent QA now executes the launch
  preflight dry-run, verifies the mode is unchanged, and confirms that an
  implicit production switch is rejected without exact `GO_LIVE`.
- Hardened the eight-hour operational soak with environment-only child
  credentials, atomic checkpoints, source-harness SHA-256 drift detection,
  exact backpressure evidence, sentinel p95 SLOs, early child reaping, and
  dynamic Gunicorn process-tree RSS plus DB/WAL/memory/disk evidence.
- Added canonical-doc command target validation and fixed broken-symlink
  reporting in prepush. Archived completed AI Agent/RC1 evidence, added a
  PointsChain document map, repaired a stale game archive link, and removed
  machine-specific paths from active operator tutorials.

## 05_2026.07.11-001

- Made Root AI Agent launch preflight dry-run by default. Production switching
  now requires an explicit `auto_switch=true` request and exact `GO_LIVE`
  confirmation at the backend boundary; missing reports return actionable
  report-generation commands instead of a false success.
- Reworked full-feature stress into true per-account sessions with deterministic
  operation rotation, account/operation coverage evidence, and configurable
  server-busy SLA enforcement. The legacy long-needle runner now provisions
  real accounts instead of cloning one `test` session under fake names.
- Added an eight-hour minimum operational soak orchestrator with concurrent
  member traffic, root/manager sentinels, PointsChain destructive stress,
  repeated Playwright checks, and `/tmp`-only artifacts. Non-loopback targets
  require an explicit ownership acknowledgement.
- Expanded deep browser QA across every visible module at 360x800, 390x844,
  768x1024, and desktop viewports. Added a bounded, redacted frontend failure
  buffer and wired previously silent background failures into browser evidence.
- Updated production-gate timing to match real suite duration: 7,200 seconds
  for nested full pytest and 10,800 seconds for the outer whole-site wrapper.
- Hardened repository readiness checks with recursive canonical-document link
  validation, reverse script-index validation, and broken symlink detection.
  Removed stale release evidence and documented the current Agent, deployment,
  mobile, and operational-soak workflows.

## 05_2026.07.10-001

- Rebuilt AI Agent actions around explicit role and operation-mode policy.
  Users can confirm own-scope safe actions, managers can operate bounded member
  and community governance tools, and root retains audited system, emergency,
  launch, and Codex handoff controls. Tool discovery and execution now share the
  same effective-policy contract.
- Made AI audit write locks persistent across workers, added encrypted
  conversation isolation and role-scoped read snapshots, and extended the live
  Playwright journey across user, manager, and root Agent workflows.
- Hardened ComfyUI terminal job commits and semantic i2i/edit delivery review so
  domain status, Job Center status, progress, and review evidence cannot expose
  partially committed success.
- Made video tips atomic and idempotent across split databases, corrected media
  type handling and SVG isolation, and tightened album/upload/stream contracts.
- Removed false-green CI paths, registered the expanded QA inventory, kept all
  test runtimes under `/tmp`, and made the deep Playwright runner directly
  executable without an external `PYTHONPATH` workaround.
- Made Cloud Drive dashboard reads navigation-scoped, abortable, deduplicated,
  and cache-safe so fast module changes cannot leave stale task requests or
  overwrite a newer remote-download view.
- Made video upload progress represent server-side publish completion rather
  than transport completion, and invalidated stale detail navigation so a late
  publish callback cannot pull the user away from the video list.
- Normalized and deduplicated dev-server shutdown PID candidates before process
  termination, preventing adjacent listener/worker IDs from being concatenated
  into a false non-dev PID during multi-worker shutdown.
- Made gitleaks timeouts bounded and actionable for the large audit repository,
  aligned the CI job budget with the quick-test timeout, and closed two
  nondeterministic gates: stale ComfyUI progress snapshots and minute-boundary
  margin-collateral retries.
- Added bounded fresh-connection retry for management-plane job enqueue under
  SQLite write contention, preventing burst root operations from surfacing a
  lock exception; the lock regression now runs in the pre-push quick suite.

## 05_2026.06.08-001

- Fixed AI Agent and settings validation regressions discovered after the
  rebase: `site_name` / login / success text settings now enforce explicit
  max lengths to match API validation, and AI Agent role normalization now
  preserves `admin` as an independent non-root role while `root` remains
  super-privileged.

## 05_2026.06.06-007

- Rebased 05.AI_Agent onto 04.BLOCKCHAIN_RC1 `04_2026.06.06-007`, keeping the
  05 AI Agent prototype while inheriting workflow-template execution fidelity,
  SDXL skip-refiner custom checkpoint fixes, random-by-default workflow seed
  mode, and workflow `run_count` handling that stays separate from batches.

## 05_2026.06.06-005

- Rebased 05.AI_Agent onto 04.BLOCKCHAIN_RC1 `04_2026.06.06-005`, keeping the
  05 AI Agent prototype while inheriting request-scope SQLite connection
  cleanup that prevents leaked route connections from holding write locks and
  surfacing repeated `目前資料庫忙碌，請稍候再試` responses.

## 05_2026.06.06-004

- Rebased 05.AI_Agent onto 04.BLOCKCHAIN_RC1 `04_2026.06.06-004`, keeping the
  05 AI Agent prototype while inheriting the fixed always-visible `Civitai /
  模型管理` AI image-generation subtab.
- Inherited the ComfyUI history restore/rerun fix that preserves and reapplies
  the full normalized generation payload, including HF repo/variant, UI
  batch/run count, seed-after-generate mode, source/mask refs, and ControlNet
  image refs.

## 05_2026.06.06-003

- Rebased 05.AI_Agent onto 04.BLOCKCHAIN_RC1 `04_2026.06.06-003`, keeping the
  05 AI Agent prototype while inheriting the live Playwright fix that refreshes
  Civitai/root-panel visibility when switching to the active HF surface.

## 05_2026.06.06-002

- Rebased 05.AI_Agent onto 04.BLOCKCHAIN_RC1 `04_2026.06.06-002`, keeping the
  05 AI Agent prototype while inheriting the cleaned HF/ComfyUI settings split
  and explicit Civitai model-import entry.

## 04_2026.06.06-007

- Fixed workflow-template execution fidelity so background workers use the
  current `comfyui_workflow_runs` snapshot instead of falling back to preset
  defaults. User-selected checkpoint/model values now drive the sent workflow,
  result metadata, and history payload consistently across templates.
- Hardened SDXL skip-refiner runs: selecting a custom base checkpoint while
  skipping the refiner removes refiner nodes from the runtime workflow and
  records the selected base checkpoint as `model`/`checkpoint`, preventing
  `sd_xl_refiner_1.0.safetensors` from reappearing in output cards or history.
- Workflow templates now default seed-after-generate to random and pass
  `run_count` separately from `batch_size`; the frontend executes workflow
  run count as repeated workflow runs rather than increasing batches.
- History one-click reruns randomize sampler seeds again when the stored run
  uses random seed mode, while explicit fixed/increment/decrement histories
  keep their recorded behavior.

## 04_2026.06.06-006

- Fixed ComfyUI history restore for workflow runs. The history payload now
  carries workflow preset identity and params, and the frontend restores the
  selected workflow, SDXL skip-refiner state, custom runtime model values,
  trigger-word prompt text, width/height, steps, CFG, sampler/scheduler, batch,
  run count, seed, and seed-after-generate mode from the workflow snapshot.
- Added KSamplerAdvanced/noise_seed mapping so workflow histories using
  Advanced samplers restore the visible seed field instead of falling back to
  defaults.

## 04_2026.06.06-005

- Added request-scope SQLite connection cleanup. Every Flask request now tracks
  opened DB connections and teardown rolls back uncommitted transactions before
  closing them, preventing leaked route connections from holding SQLite write
  locks and surfacing repeated `目前資料庫忙碌，請稍候再試` responses.

## 04_2026.06.06-004

- Promoted `Civitai / 模型管理` to the same AI image-generation subnav row as
  `Workflow` and `歷史重跑`. The tab is no longer hidden behind root-mode state
  detection; non-root users see the fixed entry with a permission note, while
  root gets the Civitai search/download/upload form in local and remote ComfyUI
  modes.
- Fixed regular ComfyUI history restore/rerun to preserve and reapply the full
  normalized generation payload, including HF repo/variant, UI batch/run count,
  seed-after-generate mode, source/mask refs, and ControlNet image refs.

## 04_2026.06.06-003

- Fixed a live UI state leak found by Playwright: switching from the ComfyUI
  surface to `HF / Diffusers` now refreshes root-panel visibility immediately,
  so the `Civitai / 模型匯入` shortcut does not remain visible on the active HF
  surface.

## 04_2026.06.06-002

- Cleaned up the AI backend settings split: the HF family now only shows
  Hugging Face / Diffusers fields, while Civitai, ComfyUI Account API Key,
  local/remote ComfyUI settings, batch limits, and default dimensions stay in
  the ComfyUI family.
- Added an explicit root-only `Civitai / 模型匯入` button on the ComfyUI
  generation surface and renamed the model-management subtab to
  `Civitai / 模型管理`, so the Civitai import tools are discoverable without
  guessing that they live behind a generic model tab. The shortcut is hidden
  while the active surface is HF to keep the HF view clean.

## 04_2026.06.06-001

- Hardened Hugging Face / Diffusers repo inspection for multi-capability repos:
  backend responses now separate all detected `supported_modes` from locally
  runnable `runnable_modes`, so t2t/i2t metadata is visible without pretending
  it can run through the current image-generation path.
- Added common HF repo shortcuts for SD 1.5, Juggernaut XL, RealVisXL,
  Animagine, AnimeMix, FLUX.1 schnell, Qwen Image/Edit, and Z-Image Turbo.
  Qwen-VL and GPT-OSS style repos are detected as non-image-generation modes;
  Anima modular repos report ModularPipeline incompatibility instead of being
  offered as runnable Diffusers repos.
- Updated the HF/Diffusers UI to keep image-source controls visible when a repo
  supports i2i, show supported/runnable mode labels, and let users manage a
  small local custom common-repo list.
- Kept root Civitai model import visible across local, remote, and Diffusers
  modes, and moved the Hugging Face token quick setting out of the generation
  form into the gear-menu AI image settings.
- Changed development launch defaults to bind `0.0.0.0`, disable trusted-host
  enforcement unless explicitly re-enabled, and write generated restart
  shortcuts under the active runtime root. The repo root restart shortcut is no
  longer ignored so accidental generation there is visible in `git status`.
- Aligned dev/probe launchers and the Transmission setup helper with the same
  `0.0.0.0` testing policy; Transmission RPC whitelist defaults are disabled for
  isolated dev runs.
- Fixed a Job Center schema-ready cache race after rolled-back schema creation,
  preventing first-read remote-download task 500s in deep browser probes.

## 2026.06.01-003

- Hardened the 3D Rubik's Cube mobile layout: the cube, solver notes, hints, and
  compact controls now stack cleanly on phone-width screens without horizontal
  overflow or text overlap.
- Added touch-specific Rubik interaction CSS so finger drags on stickers stay
  inside the cube gesture handler instead of fighting page scroll, while empty
  stage drags continue to rotate the view.
- Limited solver hints to three assisted moves per scramble. The hint button
  still performs the move for the user, but it can no longer be repeated to
  solve the whole cube without choosing the explicit auto-solve action.

## 2026.06.01-002

- Exposed the 3D Rubik's Cube game through the backend game catalog, solo-score
  allowlists, and frontend cache keys so it appears in the game selector.
- Reworked the Rubik's Cube renderer from six flat face panels into 26 visible
  3D cubies with 54 stickers. Dragging a row or column now rotates the affected
  visible cubies around the correct axis before committing the cube state,
  while the hint button directly performs the next move with the same motion.

## 2026.06.01-001

- Added dev-startup support for machine-readable capacity JSON reports, with local report files ignored by git.
- Fixed CI regressions in video direct-stream gating, X-Accel offload defaults, remote-download queue messaging, and platform Playwright forced-error waiting.
- Updated video debug bandwidth labels to avoid legacy rate-unit text in repository scans.

## 2026.05.28-008

- Added official ComfyUI GGUF profiles for
  `sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF` and
  `calcuis/sd3.5-large-gguf`. SD3.5 exposes Q4_0/Q4_1/Q5_0/Q5_1/Q8_0/F16
  variants so users can select precision explicitly.
- Added installed-GGUF inventory in `/api/comfyui/models` and
  `/api/comfyui/installed-gguf`, with frontend display near the official GGUF
  selector.
- Updated the standalone GGUF probe to support SD3.5-style
  `UnetLoaderGGUF + TripleCLIPLoader` workflows with a third text encoder/T5
  slot.
- Added a reusable `hackme-gguf-profile` skill and repo backup for adding,
  validating, testing, and documenting future Hugging Face GGUF profiles.

## 2026.05.28-007

- BT/aria2 remote-download failure reporting now tails logs with a bounded
  `deque(maxlen=N)` window instead of reading the whole aria2 log file before
  trimming the error message.

## 2026.05.28-006

- Root/admin log tails now use bounded `deque(maxlen=N)` reads instead of
  loading entire log files before slicing the last lines.

## 2026.05.28-005

- Fast `/api/admin/health` now reuses one audit-chain result across the response
  and passes a schema-only DB summary into readiness. Full `PRAGMA quick_check`
  and `foreign_key_check` remain on the explicit `/api/admin/health/db-integrity`
  endpoint.
- `/api/admin/security-center` readiness also uses the schema-only DB summary,
  while still reusing one audit-chain integrity result across readiness,
  anomaly, and audit summary sections.

## 2026.05.28-004

- Continued the adversarial load audit on platform centers. `GET /api/jobs`
  and `GET /api/admin/jobs` no longer run stale cloud-download cleanup,
  resumable-upload cleanup, HLS cleanup, and terminal-job purge on every list
  read.
- Job Center list maintenance is now process-local rate-limited by
  `HACKME_JOB_LIST_MAINTENANCE_INTERVAL_SECONDS` (default `30` seconds).
  Responses include a compact `maintenance` object with run/skip reason and
  cleanup counts, while root can force a sweep with `maintenance=1` or
  `sweep=1`.
- Documented that `/api/admin/jobs` is root-only and that list polling should
  remain observational; maintenance work should not be amplified by visible
  Job Center refresh intervals.
- `GET /api/cloud-drive/refs` now has a hard `limit` / `offset` cap and
  pagination metadata, preventing large chat/forum/announcement contexts from
  hydrating every attachment and running per-row permission checks in one
  request.
- Chat message synchronous fanout is bounded: group notifications respect
  `HACKME_CHAT_NOTIFICATION_FANOUT_LIMIT`, while attachment grant creation
  refuses rooms above `HACKME_CHAT_ATTACHMENT_GRANT_SYNC_LIMIT` with a clear
  409 instead of trying to insert unbounded grant rows inside the send request.
- Chat room JSON export now returns a bounded latest page (`limit` default
  `1000`, max `5000`) plus `pagination.next_before_id`, so exporting a busy room
  no longer hydrates the entire message history in one request.
- `/api/admin/security-center` now computes audit-chain integrity once and
  reuses that result across readiness, anomaly, and audit summary sections
  instead of repeating the same verification work inside one response.

## 2026.05.28-003

- Continued adversarial management-plane audit after the finance 50K work. The
  remaining synchronous PointsChain pre-checks now use bounded verification:
  due seal, force seal, server-update recovery checks, root verify jobs,
  post-restore PointsChain validation, and recovery auto-handle no longer scan
  the full ledger on the request path.
- `/api/points/transactions` non-compact root reads now fully respect the
  explicit `sweep=1` switch. Ordinary list reads no longer finalize pending
  transfer rows as a hidden side effect.
- Health Center finality-sweep status now peeks the latest management snapshot
  read-only instead of creating management-plane tables from a health GET.
- `/api/admin/platform-stats`, storage capacity health reads, root/admin
  storage user lists, storage quota sync, storage trash purge, scheduled
  storage maintenance, announcement attachment request listing, and violation
  integrity summaries were bounded so common admin pages do not sweep all users
  or hydrate unbounded lists.
- PointsChain forensic bundles no longer inline the full ledger into JSON after
  a safe-mode incident; they keep head/counts plus recent ledger/block/audit
  samples and leave full ledger export out of the request path.

## 2026.05.28-002

- Added optional Nginx `X-Accel-Redirect` offload for storage-local files that
  do not require Python to transform the payload: plain Cloud Drive downloads,
  storage-share downloads, E2EE ciphertext delivery, direct video streams, and
  HLS playlists/segments/subtitles.
- Kept server-side encrypted downloads and previews on the existing
  range-aware chunked decrypt streaming path, so plaintext is never exposed to
  the web server offload location.
- Added transfer-path headers (`X-Hackme-Transfer-Mode` /
  `X-Hackme-Transfer-Offload`) so probes can distinguish X-Accel offload,
  Python `send_file`, buffered bytes, throttled stream, chunked decrypt, and
  E2EE chunk delivery. Realtime proxy streams are explicitly marked as
  `python_realtime_proxy`.
- Changed resumable Cloud Drive chunk uploads to stream each chunk directly to
  a temporary part file with bounded reads instead of materializing the chunk in
  Python memory.
- Cloud Drive upload transfer limits still reject disabled upload tiers, but
  app-side sleep shaping is now opt-in via
  `HACKME_CLOUD_DRIVE_UPLOAD_SLEEP_SHAPER=1` so high-volume uploads do not
  sleep inside Flask workers by default.
- Hardened community/chat/notification read load:
  chat polling can request only messages after the last seen id, notification
  background polling uses `/api/notifications/unread-count` while the panel is
  closed, delta chat polls can skip member-count metadata, announcement reads
  support frontend ETag/304 revalidation, forum category/thread count endpoints
  avoid per-row count queries, and thread-detail replies are bounded with
  pagination metadata plus on-demand frontend loading.
- Hardened auth/member management hot paths: user identity migrations now add
  indexes for status/role/effective-level/sanction/lowercase username/email
  lookups, auth DB setup adds CSRF/login-attempt/session composite indexes, and
  the admin user list scopes online-session aggregation to the visible page
  instead of grouping every active session.
- Registration now records a `signup_bonus_deferred` flag when wallet/points
  initialization fails, and only flagged accounts are eligible for first-login
  signup-gift reissue after approval. Existing active users no longer receive a
  signup bonus just because the login path runs points onboarding checks.
- Reduced ComfyUI/Hugging Face control-plane chatter: Hugging Face Diffusers
  repo metadata inspection is short-cached server-side and deduped client-side,
  ComfyUI model-list loads are deduped with a short frontend cache, and manual
  refresh/start/download/upload paths explicitly bust that cache.
- Optimized games and experiments read/render load: solo score submits use a
  compact response by default and refresh the leaderboard once, game leaderboard
  / visible-match / invite queries gained hot indexes, and the browser-only
  experiments area now scales particles/DPR down for low-core or
  reduced-motion clients while clamping large animation frame gaps.
- Moved the remaining heavy PointsChain control-plane reads further off the
  request path: financial invariant audit, economy stats, and abnormal-chain
  auto-handle now run as management-plane jobs with latest snapshot reads.
  Added a bounded operations snapshot for service-fee linkage, private-chain
  queue health, exchange-fund watermarks, emergency governance, and initial
  distribution status.
- Hardened root/admin browser control-plane load: server output, backpressure
  traffic, and system resource boards now stop polling while the page is hidden
  or being unloaded, then resume only for the currently active management tab.
  Health-page platform/update reads are deferred to idle time, and mobile
  admin/health/resource panels gained overflow and touch-layout hardening.
- Added app-level QoS and edge burst hardening: responses now carry
  `X-Hackme-QoS-Class`, high-risk auth/root-admin/upload entry points have a
  process-local pre-DB burst guard that returns `429 edge_rate_limited`, and the
  root backpressure dashboard reports edge-guard rejections alongside normal,
  heavy, root, and fast-lane traffic.
- New deployment knobs:
  `HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX` / `HACKME_X_ACCEL_STORAGE_PREFIX` for the
  internal Nginx location, and optional
  `HACKME_CLOUD_DRIVE_X_ACCEL_STORAGE_ROOT` / `HACKME_X_ACCEL_STORAGE_ROOT` when
  the Nginx alias root differs from the app storage root.
- New QoS/anti-burst knobs:
  `HACKME_EDGE_BURST_GUARD_ENABLED`,
  `HACKME_EDGE_BURST_WINDOW_SECONDS`,
  `HACKME_EDGE_AUTH_BURST_LIMIT`,
  `HACKME_EDGE_MANAGEMENT_BURST_LIMIT`, and
  `HACKME_EDGE_UPLOAD_BURST_LIMIT`.
- Added a Playwright root-operations mobile smoke that opens the actual Health,
  Capacity, and Environment management tabs at mobile/tablet/desktop viewports
  and checks viewport overflow. The Health Center now includes frontend timing
  marks for management `first-summary` and `secondary-chart` render time.
- Extended the production Nginx example with split `limit_req` lanes for auth,
  root/admin management, upload/heavy-transfer, static assets, and generic API
  traffic so boundary throttling can match app QoS classes.
- Added a long needle simulation probe for economy, PointsChain/private-chain,
  and full-feature load. The probe enables all feature flags in its isolated
  runtime, records resource/QoS summaries, and found then verified a fix for
  compact root transaction lists skipping the bounded proved-pending finality
  sweep.
- Added a `long-needle-simulation` GitHub Actions workflow: PR/push changes on
  PointsChain/economy/stress paths run the quick profile, scheduled nightly
  runs use the medium profile, and QA artifacts are uploaded from the isolated
  runtime.
- The Health Center now receives a bounded PointsChain transfer-finality
  observability snapshot plus split-DB maintenance file totals, so root/admin
  can see pending transfer age, compact sweep activity, unsealed ledger sample
  pressure, DB sidecars, and largest DB file without starting a heavy report.
- Root can now start `POST /api/root/points/finality-sweep` from the Health
  Center. It queues a bounded management-plane job, serializes on the finance
  DB resource lock, writes a latest snapshot, and gives finality maintenance a
  first-class path outside transaction-list refreshes. Health also reads the
  persisted latest sweep snapshot, so the last maintenance result survives
  process restarts.
- `/api/points/transactions` no longer runs finality/deposit maintenance by
  default. Root can still request the legacy bounded behavior with `sweep=1`,
  but the destructive stress harness now queues the explicit finality-sweep job
  and uses compact transaction lists only for observation.
- The Nginx production example now emits `X-Hackme-Edge-Lane` and
  `X-Hackme-RateLimit-Status`, records lane/limit status in access logs, and
  orders upload regex locations ahead of generic root/admin management routing.

## 2026.05.28-001

- Added Premium HLS asset/profile drift detection to playback payloads. The
  API now reports the current requested profile, inferred prepared asset
  profile, asset variant/quality/audio signature, match status, drift flag, and
  whether HLS should be rebuilt after operators change
  `HACKME_MEDIA_HLS_PROFILE` or related profile knobs.
- Mirrored the drift fields into prepared-HLS `quality_policy` so normal video,
  shared video, Cloud Drive preview, and storage-share preview clients can make
  the same support/billing decision from their playback payload without
  re-querying management endpoints.

## 2026.05.27-008

- Added multi-audio prepared-HLS support for stream assets: browser-ready
  alternate audio playlists are generated for multi-track or non-browser-native
  audio sources, while H.264 video can stay video-only/copy instead of being
  fully retranscoded just because the source audio is E-AC-3.
- Extended HLS subtitle extraction from a fixed 20-track cap to the configured
  stream subtitle limit, preserves forced subtitle metadata, and carries audio
  playlists through normal video, shared video, Cloud Drive preview, and storage
  share preview HLS routes.
- Added customer-facing documentation for the three streaming service tiers:
  direct streaming, realtime proxy/transwrap, and prepared HLS, including why
  Basic / Standard / Premium service fees differ.
- Implemented Standard realtime proxy/transwrap Phase 1 behind
  `HACKME_MEDIA_REALTIME_PROXY_ENABLED`, with video/shared-video/Cloud Drive/
  storage-share routes, selected-audio AAC transcode, fragmented MP4 output,
  ffmpeg timeout cleanup, and concurrency limits.
- Wired the Basic / Standard / Premium selector into normal video playback,
  shared-video playback, Cloud Drive preview, and storage-share preview; Standard
  mode now applies selected audio tracks through the realtime proxy URL.
- Added Standard realtime proxy runtime guardrails for slot release on client
  close, busy `429` classification, shared-video `share_session` URLs, and Cloud
  Drive / storage-share authorization plus audio/start parameter forwarding.
- Added a real ffmpeg/ffprobe Standard realtime proxy smoke that generates a
  multi-audio MKV fixture, proxies the selected English track, and verifies the
  resulting MP4 contains H.264 video plus one AAC stereo audio stream with no
  subtitle/data tracks.
- Extended the Playwright browser video compatibility probe to generate a
  multi-audio MKV, enable Standard realtime proxy mode, switch shared-video
  playback from Premium HLS to Standard, switch audio tracks, and fetch a real
  `/realtime-proxy` MP4 chunk.
- Added Standard realtime proxy stress instrumentation and probe coverage:
  concurrency slots are acquired before ffprobe/ffmpeg setup, streams now expose
  first-chunk latency/bytes/RSS/CPU/disconnect metrics, and
  `scripts/testing/realtime_proxy_stress_probe.py` verifies busy-limit,
  disconnect cleanup, and selected-audio reopen behavior with a generated
  multi-audio MKV.
- Added `scripts/testing/realtime_proxy_http_concurrency_probe.py`, which starts
  an isolated runtime, publishes a shared multi-audio MKV, holds one Standard
  realtime proxy HTTP stream open, verifies a concurrent Standard request gets
  `429 realtime_proxy_busy`, and confirms Basic direct plus Premium HLS still
  serve first chunks.
- Added route-level realtime proxy metrics JSONL artifacts so live HTTP probes
  can validate server-side bytes, RSS, CPU, disconnect, and runtime-slot state.
- Changed Standard realtime proxy concurrency from process-local only to
  host-global file slots when a runtime/lock dir is configured, with
  process-local fallback for non-runtime or non-Unix environments.
- Extended the realtime proxy HTTP concurrency probe with a gunicorn
  multi-worker mode and verified two workers share the same host-global Standard
  slot while Basic direct and Premium HLS continue serving first chunks.
- Added configurable host-global Premium HLS worker slots:
  `HACKME_MEDIA_HLS_MAX_CONCURRENT`, `HACKME_MEDIA_HLS_LOCK_DIR`,
  `HACKME_MEDIA_HLS_SERIALIZE_ALL`, and `HACKME_MEDIA_HLS_SERIALIZE_MIN_BYTES`
  now bound large prepared-HLS jobs across workers and report slot state through
  Job Center metadata/results.
- Added `scripts/testing/hls_worker_slot_probe.py`, a live dual-worker Premium
  HLS probe that verifies the second worker enters `waiting_worker_slot`, then
  acquires the host-global slot after the first worker releases it.
- Added worker-side Premium HLS cost telemetry and
  `scripts/testing/hls_premium_sizing_probe.py`, which samples real ffmpeg/HLS
  worker CPU/RSS, derivative bytes, queue wait, and Job Center result metrics to
  recommend a conservative `HACKME_MEDIA_HLS_MAX_CONCURRENT`.
- Calibrated Premium HLS `max=2` with Scarlet 60s/180s real-file probes and
  added queue observation checks so `jobs > max_concurrent` must show a waiting
  worker while ffmpeg process peak remains capped.
- Added Premium HLS storage-saver policy through
  `HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE`, allowing deployments to skip the
  original HLS rendition when generated q480/q720 variants exist. Scarlet 60s
  `jobs=2 / max=2` derivative multiplier dropped from 2.917x to 1.334x.
- Added Premium HLS profile presets and audio-bitrate control:
  `HACKME_MEDIA_HLS_PROFILE=full|storage_saver|mobile_saver` and
  `HACKME_MEDIA_HLS_AUDIO_BITRATE`. Scarlet 60s `mobile_saver` with
  `jobs=2 / max=2` reduced derivative multiplier to 0.482x while preserving
  HLS, multi-audio, subtitles, and share authorization.
- Exposed Premium HLS profile policy in playback payloads through
  `service_policy.premium_hls_profile_policy` and prepared-HLS
  `streaming_options[].profile_policy`, including relative fee, storage cost,
  quality ladder, original-rendition policy, best-fit scenarios, and tradeoffs.
- Added `scripts/testing/hls_premium_profile_matrix_probe.py`, which reuses one
  source fixture to compare full / storage_saver / mobile_saver Premium HLS
  profiles across derivative multiplier, storage reduction, CPU/RSS, wall time,
  and output variants.
- Fixed prepared-HLS transcode segmenting by forcing keyframes at the configured
  HLS segment interval and disabling scene-cut keyframe drift, restoring 4s
  segment granularity for q480/q720 derivatives.

## 2026.05.27-007

- Generalized Playwright platform-health filtering for expected offline
  `503/404` responses under trading API namespaces, while preserving failures
  for unexpected `500` errors.

## 2026.05.27-006

- Made Playwright platform health more tolerant of CI timing by extending the
  authenticated-app wait helper and guarding final screenshot capture instead
  of crashing after an already-recorded viewport failure.

## 2026.05.27-005

- Updated Playwright platform-health browser-error filtering for the current
  Chromium console format (`503 <url>` / `404 <url>`), so expected offline
  trading/report probes do not fail the acceptance job after all checks pass.

## 2026.05.27-004

- Hardened the Playwright acceptance login helper so root login reload waits
  for `domcontentloaded` instead of `networkidle`, avoiding CI timeouts from
  legitimate background polling after authentication.

## 2026.05.27-003

- Implemented Management Plane Async/Snapshot Phase 2a: management-plane jobs
  now record queue class and resource locks, reuse fresh successful snapshots
  for bursty root/admin refreshes, and expose those details in `202` start
  payloads.
- Split heavy finance management jobs into explicit `points_chain_admin` and
  `trading_admin` queues while serializing both on the shared `finance_db`
  resource lock, keeping DB-heavy control work from stampeding SQLite.
- Moved `/api/root/trading/verify` to the async/snapshot contract with
  `/api/root/trading/verify/jobs` and `/api/root/trading/verify/latest`.
- Routed root economy UI and the 50K destructive stress harness transaction
  reads through `compact=1` bounded cursor mode instead of the full hydrate /
  hidden-maintenance path.

## 2026.05.27-002

- Fixed CI acceptance gates after the finance DB split: the Playwright platform
  probe now seeds trading fixtures into `finance.db`, validates the lightweight
  trading asset overview read model, and reads the current trading asset
  overview UI IDs.
- Registered the missing maintained QA/security scripts in `scripts/INDEX.md`
  and removed a fixed-port test fixture that violated CI portability checks.
- Renamed the cold-wallet unlock prompt variable in the economy frontend so the
  plaintext secrets scanner no longer misclassifies runtime user input as a
  hard-coded credential.

## 2026.05.27-001

- Implemented Management Plane Async/Snapshot Phase 1 for finance 50K scale:
  PointsChain seal, verify, and root report now start Job Center jobs and return
  `202 + job_id` instead of scanning the large finance DB in the request path.
- Changed trading sitewide root refresh to an async management-plane job while
  keeping sitewide pools/user-position reads snapshot-backed.
- Serialized heavy management-plane background workers with a local file lock,
  preventing concurrent root jobs from failing each other with SQLite
  `database is locked`.
- Added compact wallet-transfer submit responses, explicit compact/cursor
  transaction-list mode, wallet summary snapshot reads, and management-plane
  microbenchmark headers/log fields for handler time, SQL time, Python
  aggregation time, JSON serialization time, response bytes, RSS, and slow
  reason classification.
- Updated the 50K destructive stress harness to accept async root/admin starts,
  read latest snapshots separately, keep finalizer sweeps explicit, and default
  high-volume transaction submits to compact responses.
- Updated root economy/trading frontend flows to show queued management jobs and
  keep reading latest snapshots instead of waiting on synchronous root reports.

## 2026.05.23-004

- Fixed the PointsChain root report so expired provisional address freezes are
  marked expired and filtered out instead of returning `null` entries that break
  the private-chain dashboard.
- Hardened the private-chain frontend list rendering against null governance
  rows, preventing one stale dispute/freeze row from turning the whole page into
  a read failure.

## 2026.05.23-003

- Changed account management to server-side pagination with stable `id ASC`
  ordering so manager/root review lists no longer reorder by relationship or
  username.
- Changed the violation center to a per-account selector before loading
  violation reasons, keeping the admin page from rendering every violation
  record by default.
- Redesigned feature packages as a select-preview-apply workflow and kept job
  center background polling from refreshing the visible status message on every
  quiet poll.
- Moved DB stress audit writes out of the production `secure_audit` hash-chain
  into an isolated `db_stress_audit_events` table, preventing stress probes from
  breaking audit-chain integrity.

## 2026.05.23-002

- Added the PointsChain hard-cap monetary policy amendment path: ordinary mint
  remains capped, exhausted supply returns `mint_supply_exhausted`, and supply
  expansion only authorizes a max-supply increase through critical
  `SUPPLY_EXPANSION_REQUEST` governance.
- Restricted expanded supply minting to the destination fund approved by the
  constitutional proposal, keeping max-supply change, mint, and spend as
  separate audited steps.
- Kept isolated `/tmp/hackme_web_isolated_*` runtimes with built-in
  `root/admin/test` passwords from re-triggering forced password change when
  started directly for debugging.
- Fixed the CI repository text guard by avoiding literal legacy rate-unit labels
  in PointsChain governance code and tests.

## 2026.05.23-001

- Restored CI coverage for the renamed `04.BLOCKCHAIN_RC1` branch across the
  core, Playwright, and secrets-scan workflows.
- Registered the PointsChain stress, recovery, dispute, and governance QA
  probes in `scripts/INDEX.md` so the pre-push gate can verify their operator
  contract.
- Fixed root simulated spot buys so trading funding-source tracking does not
  reference chain spend data that only exists for PointsChain-backed orders.
- Renamed recovery compensation rate fields away from legacy trading-rate unit
  wording and kept generated artifacts out of the repository text scan.

## 2026.05.21-192

- Kept development isolated servers usable with seeded default accounts:
  `HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS=1` plus disabled dev security no longer
  re-flags root/admin/test for first-login password reset during bootstrap.
- Made PointsChain explorer transaction, wallet, block, and search GET routes
  public safe reads, matching the Etherscan-style requirement that anyone with a
  transaction hash, block reference, or wallet address can inspect chain data.
- Added regression coverage for the relaxed development bootstrap gate and for
  explorer GET routes staying free of login/CSRF-safe wrappers.

## 2026.05.21-191

- Changed user wallet transfers to a blockchain-like pending flow: submit creates
  a transaction hash and notifies sender/recipient, but does not credit the
  recipient until the transaction reaches 20/20 Proved.
- Added finality countdown refresh in the PointsChain explorer so pending
  transactions visibly count toward the next Proved step and auto-refresh when a
  proof mark is reached.
- Added `/api/points/transactions/submit` for user-authorized wallet-to-wallet
  transfers with selectable source wallet, destination address, value, fee, and
  input data memo. Transaction fees route to the official Treasury wallet.
- Added acceptance coverage for pending transfer notifications, no early
  recipient credit, finality-triggered append-only ledger creation, and completed
  notifications for both users.

## 2026.05.21-190

- Added a logged-in PointsChain explorer page for transaction hash, Ledger UUID,
  wallet address, block hash, and block number lookup.
- Added simulated finality status: 20 Proved is treated as settlement, with the
  default estimate corrected to 2-3 minutes. Pending transactions now use a
  deterministic proof schedule so `1/20` through `19/20` looks stable and
  realistic instead of linearly jumping on refresh.
- Added append-only chain acceleration requests. User-paid acceleration fees are
  recorded as normal ledger debits and routed to BURN, with idempotency conflict
  checks to prevent duplicate or mismatched fee effects.
- Added a chain-fee exemption policy for configured automatic distributions
  such as signup, genesis, birthday, game, and scheduled rewards. Root manual
  official-wallet operations remain normal manual transactions and do not inherit
  the exemption.
- Added explorer route and frontend coverage for sanitized transaction, wallet,
  block, and acceleration views.

## 2026.05.21-189

- Removed the legacy `trading_` prefix fund guess from PointsChain walletized
  ledger replay. User spot principal, position movement, and other trading
  asset swaps no longer inflate `EXCHANGE` fund.
- Mirrored explicit trading reserve-pool events into PointsChain economy events
  only when they represent real exchange fund inflow/outflow, such as retained
  fees, interest, repayments, reserve allocation, principal lending, or profit
  paid.
- Added immutable wallet-flow snapshots to new ledger rows so historical
  transaction details keep the wallet address used at write time. Deleting cold
  wallet A, binding cold wallet B, or restoring A no longer rewrites old ledger
  display.
- Wallet read payloads now report active wallet-derived balances when a user has
  wallet identities. A deleted/lost cold wallet leaves the account ledger intact
  but the current active wallet balance displays as zero until the original
  private key restores that wallet.

## 2026.05.21-188

- Changed the simulated BURN wallet to the fixed null-style address
  `pc1000000000000000000000000000000000000000000000000`.
- System wallet and economy fund bootstrap now realign existing BURN wallet
  rows to that fixed address without rewriting append-only ledger or economy
  event history.

## 2026.05.21-187

- Added a root-only synchronous refresh endpoint for trading sitewide snapshots,
  so root「全用戶倉位管理」rebuilds the stored report before reading it.
- Updated the economy root funding/all-position tabs and refresh buttons to
  refresh trading snapshots first, preventing stale reports after admin/member
  spot, margin, order, or bot changes.
- Extended sitewide position tests to cover manager/admin-owned trading
  positions and the new manual snapshot refresh path.
- Renamed root's balance tab context to「錢包管理」while keeping normal users on
  「積分錢包」, and removed the manager-facing「審核」label from the economy tab.
- Moved special fund wallet balances, statuses, and full addresses into root
  「錢包管理」, leaving the private-chain dashboard's top cards for chain health,
  supply, replay, and snapshot checks.
- Added user cold-wallet deletion/recovery semantics: only self-custody cold
  wallets can be removed, removal only marks the identity as lost, and restoring
  the same address requires proving the private key again; official hot wallets
  cannot be deleted.

## 2026.05.21-186

- Removed the duplicate root「積分總覽」card from the wallet balance page so
  root supply checks live in the PointsChain private-chain dashboard.
- Rebuilt the closed-loop supply formula as inline formula cards
  (`總上限 = 已 burn + 官方錢包 + 在外用戶總量 + 未發放 mint 量 + 交易所基金 + PROMO 基金`)
  instead of a separate log-style box.
- Removed the duplicate member-wallet summary from「全用戶倉位管理」and stopped
  returning the legacy `points_wallets` wallet list from the sitewide positions
  payload; that page now focuses on spot, margin, orders, and bots only.

## 2026.05.21-185

- Aligned the trading reserve / borrowing funding pool with the PointsChain
  `EXCHANGE` fund default, so root trading pool management now starts from
  5,000,000 POINTS instead of the legacy 10,000 bootstrap.
- Added an idempotent legacy runtime alignment event
  `walletized_exchange_fund_alignment` that preserves old `initial_funding`
  history while topping existing 10,000-point pools up to the exchange fund
  baseline.
- Updated trading regression coverage so reserve-pool assertions and borrowing
  pressure-rate expectations derive from the walletized exchange fund size.
- Corrected wallet cumulative income/expense for spot trading: buy/sell
  principal is treated as an asset swap, while realized spot PnL determines the
  statement income or expense shown in「累計收支」.
- Preserved Diffusers `python_log_tail` on failed ComfyUI generation jobs so
  the frontend Python log panel still shows the failure trail instead of going
  blank.

## 2026.05.21-184

- Started the full-site walletization cutover by wiring the shared
  `PointsLedgerService._record_transaction` path into the append-only economy
  event ledger, so normal grants and spends now change fund-wallet balances.
- Closed the major display/accounting gap: signup and default grants flow from
  PROMO, manual root credits flow from official treasury, service spends flow
  to BURN, and trading actions flow through EXCHANGE at the fund-ledger level.
- Updated ledger wallet-flow projection so rows no longer pretend MINT sends
  directly to users; fund source/destination labels now match the closed-loop
  wallet event generated for the ledger row.

## 2026.05.21-183

- Added a read-only Phase 1A.5 legacy bridge report so legacy admin/test signup
  grants are visible in the economy dashboard without mutating PROMO Fund
  events or connecting product reward flows.
- The root「積分私有鏈」dashboard now shows legacy user outstanding points,
  PROMO debit required for a future bridge, PROMO balance after that bridge,
  and the supply-equation gap using
  `burned + official + user_outstanding + mint_remaining + exchange + promo`.
- Isolated ComfyUI billing helper dependencies per app instance so async
  generation jobs cannot charge through a points service overwritten by another
  test/server instance.
- Added regression coverage for the bridge math and frontend dashboard fields.

## 2026.05.21-182

- Completed the Phase 1A.5 PointsChain economy-layer acceptance review and
  added the acceptance report / JSON evidence under `docs/AGENTS/reports`.
- Expanded the root「積分私有鏈」dashboard so max supply, minted total, mint
  remaining, reserved locked, active supply, circulating supply, fund balances,
  burn, replay snapshot, derived verify, and health/stress status are visible.
- Added economy acceptance tests for repeated bootstrap idempotency and corrupt
  burn replay rejection, and documented the Phase 1A accepted / Phase 1B-1D
  not-yet-connected boundary.

## 2026.05.21-181

- Added the Phase 1A private economy layer foundation for PointsChain with
  deterministic MINT, BURN, official treasury, PROMO fund, and EXCHANGE fund
  wallets.
- Added append-only economy events, replay-derived fund balances, derived-cache
  rebuild / verify helpers, replay snapshots, and root dashboard summary cards.
- Documented economy-layer guardrails and added tests for bootstrap idempotency,
  mint cap enforcement, burn replay, derived-cache verification, incident
  append-only behavior, and idempotency conflict handling.
- Normalized completed ComfyUI async job payloads so a completed job always
  reports 100% progress to the frontend.

## 2026.05.21-180

- Added a read-only wallet-address flow projection to PointsChain ledger reads
  so credits display as official issuance wallet to user `pc1...` wallet when a
  wallet identity exists.
- Clarified economy wallet / ledger labels: legacy `public_account_id` is now
  labeled as a legacy ledger identity, and ledger UUIDs are explicitly labeled
  as transaction IDs instead of source addresses.

## 2026.05.21-179

- Fixed the PointsChain simulated wallet address display so long `pc1...`
  addresses wrap inside the wallet card instead of stretching the grid.
- Kept the full address available as hover title text while using compact
  monospace styling for address fields.

## 2026.05.21-178

- Added the simulated PointsChain wallet identity layer for official hot
  wallets, browser-held cold wallets, imported cold wallets, multisig policy
  wallets, and mint / burn system wallet identities.
- Deferred non-root signup bonus issuance until wallet onboarding completes,
  while keeping existing ComfyUI, Trading, Video, Storage, Games, and product
  billing flows untouched for this Phase 1 slice.
- Added wallet identity contract tests for private-key rejection, public-key
  signature binding, signup-bonus idempotency after onboarding, and system
  wallet identity-only behavior.

## 2026.05.21-177

- Merged the latest `03.Points` ComfyUI / Hugging Face GGUF routing work into
  `04.BLOCKCHAIN` while retaining the Phase 0 / Phase 1 walletization gates.
- Added automatic GGUF backend routing for Hugging Face mode: Diffusers-compatible
  GGUF files stay in Diffusers, while ComfyUI-GGUF native UNet files route to a
  ComfyUI `UnetLoaderGGUF` workflow.
- Added an SDXL GGUF text-to-image system workflow and ComfyUI-GGUF template
  metadata, including local `models/unet` auto-attach support and explicit
  remote ComfyUI administrator guidance.

## 2026.05.21-176

- Added root-controlled local ComfyUI `main.py` performance controls for VRAM
  mode, precision, UNet/VAE/text-encoder dtype, CPU VAE, attention backend,
  cuda malloc, async offload, cache mode, deterministic mode, smart-memory
  behavior, and reserved VRAM.
- Wired those controls into local ComfyUI startup through allowlisted command
  arguments plus `COMFYUI_EXTRA_ARGS`, while keeping remote API mode clearly
  marked as not applicable for server startup flags.
- Updated the downloadable Linux ComfyUI startup template so custom local
  installs can pass through the managed performance arguments.

## 2026.05.21-175

- Added a root-controlled Diffusers downloaded-model cache policy, exposed in
  the full settings panel and right-side quick settings.
- Exposed Diffusers `device_map` and `low_cpu_mem_usage` as root-controlled
  loading parameters so large-model behavior can be tuned per machine.
- Added a root-controlled CUDA fallback setting so Hugging Face / Diffusers can
  switch to CPU on low-VRAM auto runs or CUDA load / inference failures.
- Added earlier Diffusers Python runtime checkpoints around dependency import,
  Torch import, Hugging Face snapshot download, pipeline class selection, and
  `from_pretrained` startup so stalled runs show where they are blocked.
- Mark Hugging Face cache-hit runs separately when no download bytes are
  reported, and include CUDA VRAM details in Diffusers Python logs before heavy
  pipeline loading begins.
- Adjusted Diffusers `device_map=auto` so CUDA devices below 8GB VRAM prefer
  `balanced` placement instead of forcing the full pipeline onto GPU memory.
- Adjusted Diffusers frontend progress / preview wording so Hugging Face mode no
  longer says it is waiting for ComfyUI output.
- Load live Diffusers settings from the database when building the backend
  client so quick-setting changes do not get hidden by a stale gunicorn worker
  cache.

## 2026.05.20-173

- Captured real Hugging Face / Diffusers Python runtime output in the frontend
  log panel, including stdout, stderr, warnings, logging records, and tqdm-style
  download / loading progress.
- Re-enabled Diffusers progress bars during in-process runtime execution while
  still forwarding structured progress and redacting Hugging Face tokens from
  `python_log_tail`.

## 2026.05.20-172

- Made the ComfyUI Diffusers progress panel always show a Python log area in
  Hugging Face mode, even before logger output has arrived.
- Propagated Diffusers failure reasons through job progress with
  `backend_kind`, `error_message`, and preserved `python_log_tail` so failed
  generations do not collapse to a generic error.
- Improved frontend job failure messages by combining backend error, progress
  detail, and Diffusers log guidance.

## 2026.05.20-171

- Hardened the Phase 1 Wallet Service Facade contract so completed idempotent
  replays bypass later write guards without duplicating effects.
- Added compensation de-duplication for refund / rollback when the same
  original ledger is retried with a different idempotency key.
- Locked refund / rollback wallet guards to the target ledger user instead of
  the acting operator wallet, with frozen / closed target wallet coverage.

## 2026.05.20-170

- Added the Phase 1 Wallet Service Facade contract skeleton without wiring it
  into ComfyUI, trading, video, storage, games, or existing route behavior.
- Added database-level idempotency contract coverage, same-request replay /
  different-payload conflict tests, append-only refund / rollback tests, and
  safe-mode / sanctioned-wallet guard tests.
- Strengthened the wallet direct-call inventory scanner for bare function calls
  and simple imported aliases while keeping the Phase 0 migrate list unchanged.

## 2026.05.20-169

- Ran a deeper front/back audit after root `pytest` collection exposed archived
  docs scripts and the first isolated full pytest pass found 17 regressions.
- Fixed pytest discovery, pre-push API/snapshot/log-chain/smoke gates, and the
  video-module pentest expectation so audit tooling matches the current repo
  layout and security model.
- Fixed ComfyUI shared-backend interrupt policy, restored the global language
  switcher wiring, returned muted text color to the WCAG AA regression target,
  and repaired game invite/practice/chess neural accumulator coverage.
- Fixed Cloud Drive remote-download task status/list fallback through Job
  Center so multi-worker polling does not turn active or failed BT tasks into
  silent 404s; refreshed the member-probe QA script for current BT tracker and
  video stream-readiness behavior.
- Added a critical API contract snapshot and release-visible audit report for
  this full front/back pass.

## 2026.05.20-168

- Re-ran the ru4vm4 full-site audit across deep Playwright, platform centers,
  member probe, security headers, low-volume HTTP stress, ComfyUI/template UI,
  games, video/HLS, remote downloads, CSRF, plaintext secrets, and trading.
- Fixed the deep Playwright ComfyUI workflow action check so collapsed
  "更多操作" buttons are opened before visibility assertions.
- Fixed trading live-price quote caching so `boot_pending` warmup responses are
  never served from cache; a second stable live quote can now release bot /
  matching / risk gates promptly.
- Recorded the new audit findings and environment blockers in
  `docs/AGENTS/reports/2026-05-20_1055_ru4vm4_full_site_audit.md`.

## 2026.05.20-167

- Ran a full-site isolated QA audit covering auth, admin, Cloud Drive/E2EE,
  video sharing/HLS, ComfyUI frontend/template schema, games, economy/trading,
  platform centers, security headers, low-volume HTTP stress, and plaintext
  secret scanning.
- Fixed direct-entry security/traffic audit scripts so `header_security_check.py`
  and `stress_test.py` can import repo modules when executed by path in CI.
- Updated the ComfyUI media preview smoke assertion to match the current
  MIME-aware `<video><source ...>` renderer used for generated video previews.
- Recorded the audit artifacts and environment blockers in
  `docs/AGENTS/reports/2026-05-20_1030_full_site_audit.md`.

## 2026.05.20-166

- Isolated trading cached-fallback tests from external market connectivity by
  stubbing both the configured Binance fetcher and fused fallback path, so CI
  cannot accidentally use live public prices in unit tests.

## 2026.05.20-165

- Aligned the main `ci` workflow with the dedicated secrets workflow by
  installing gitleaks before the pre-push gate, so default-branch pushes do not
  fail on missing CI tooling.
- Rechecked repo cache/runtime cleanup and recorded the latest branch Actions
  status after the security scanner fix.

## 2026.05.20-164

- Updated the plaintext secrets scanner rules/allowlist so CI distinguishes
  dynamic request values and documented test fixtures from real committed
  secrets.

## 2026.05.20-163

- Fixed the plaintext secrets scanner entrypoint so the security workflow can
  import the project `scripts` package when run directly by GitHub Actions.

## 2026.05.20-162

- Enabled push-triggered GitHub Actions for the current default branch
  `03.Points` across the smoke/security and Playwright workflows, so default
  branch pushes no longer rely only on scheduled runs.

## 2026.05.20-161

- Fixed Diffusers mode progress language so Hugging Face download/model-load
  jobs no longer claim the ComfyUI backend is unresponsive; the frontend now
  shows sanitized Diffusers Python log tail text while the job is running.
- Reduced false CSRF security alerts from same-session multi-tab/concurrent
  requests by keeping a short recent authenticated-token window, while keeping
  public/login CSRF tokens single-use.
- Changed Diffusers repo inspection to a safe read endpoint so the quick
  settings probe no longer needs a mutation CSRF token.
- Updated the ComfyUI workflow builder Playwright smoke so CI handles the
  intentionally collapsed node toolbox/categories before clicking catalog or
  built-in nodes.
- Cleaned repo cache/runtime artifacts, refreshed release-visible docs, and
  recorded the focused QA checks for the Diffusers/CSRF changes.

## 2026.05.19-160

- Fixed ComfyUI workflow media classification so `SaveVideo` and video-file
  outputs are returned as playable media even when a ComfyUI node reports them
  under an `images` output key.
- Kept ComfyUI workflow template model fields on their template defaults unless
  the user explicitly opens the edit control, avoiding cross-template/global
  model selection leakage while preserving LoRA selection controls.
- Added a Playwright QA helper and report for default official ComfyUI template
  frontend preview checks against a remote ComfyUI API.
- Cleaned CI gate metadata by registering maintained QA/probe scripts and
  removing a workstation-specific example video path from the HLS stress probe.

## 2026.05.18-159

- Added root-configurable server application timezone support. The admin
  settings page now lets root choose an IANA timezone such as `UTC` or
  `Asia/Taipei`, shows the server local/UTC time, and compares server clock
  drift against the browser clock through `/api/version`.
- Made runtime version metadata timezone-explicit: `SERVER_STARTED_AT` is now
  emitted as UTC with a `Z` suffix, and `/api/site-config`, `/api/version`, and
  `/readyz` include a structured `server_time` payload.
- Bumped the published release id so login/sidebar version badges reflect the
  code currently being served.
- Fixed default account point seeding: when the economy module is enabled,
  startup/dev bootstrap now idempotently backfills the default `admin` manager
  grant and `test` user grant, including runtimes that already have existing
  PointsChain blocks.

## 2026.05.17-158

- Hardened the CI/pre-push gate after recent game, experiment, and deployment
  changes: generated chess runtime DB files are no longer tracked, local
  workstation paths were replaced with deployable defaults, maintained Exp6 /
  stress / browser QA scripts were registered in `scripts/INDEX.md`, and
  gitleaks now skips generated chess runtime evidence.
- Updated regression assertions to match the current lazy module-loading,
  anonymous chat-avatar, and deferred margin-fee settlement behavior.
- Kept the recent frontend gameplay and experiment-area polish in the release
  train so the published release id matches the code users are actually served.
- Reworked the open-world game map from a uniform road grid into a more
  readable city: varied avenue widths, sidewalks, medians, crosswalks, alleys,
  waterfront, park, hospital, market, industrial props, and matching minimap
  colors now make districts easier to read while preserving existing missions
  and vehicle routing.

## 2026.05.13-157

- Reorganized `docs/` and `docs/games/` into current guides, reports,
  references, evidence, model snapshots, and archive indexes.
- Added `scripts/CALL_MAP.md` and linked it from `scripts/README.md` so script
  entrypoints, call chains, and artifact locations are discoverable.
- Cleaned generated runtime/cache artifacts, expanded `.gitignore` exceptions
  for intentional documentation evidence, and hardened prepush checks against
  decorative separator false positives.

## 2026.05.13-156

- Added tactical enemy AI for 2D Stickman Shooter and 3D FPS Arena. Enemies now
  use role-specific ranges, movement, cover seeking, flanking, suppression, and
  trap or collision-aware movement instead of only walking directly at the
  player.
- Fixed CI entrypoint portability for `scripts/prepush/pre_push_checks.py` by
  inserting the repository root on `sys.path` before importing
  `scripts.prepush.runner`.
- Hardened Playwright platform acceptance by using the shared JSON request helper
  for job retry POSTs, preserving CSRF enforcement while allowing the test to
  refresh and retry after a legitimate `csrf_invalid` response.
- Removed tracked generated runtime/model artifacts from version control,
  replaced machine-local chess audit defaults with repo-relative paths, repaired
  agent report markdown links, and registered maintained QA/chess scripts in
  `scripts/INDEX.md`.

## 2026.05.10 — Platform Center Phase 1.5

- Added a platform-center surface for background jobs, notifications, share
  links, and trading asset overview. Job Center reads `/api/jobs` for normal
  users and `/api/admin/jobs` for manager/root, displays progress/stage/error
  details, asks for confirmation before cancel, and can retry failed/cancelled
  jobs.
- Notification Center now tracks `dismissed_at`; dismissed notifications are
  hidden from the default list and unread count, while admin/root audience
  boundaries remain enforced.
- Share Link Management lists file / album / video shares, shows expires/max
  views/password status, records access events, and revokes through
  `/api/shares/<type>/<id>/revoke`. External share URLs are not copied blindly
  by the frontend.
- Trading Asset Overview now includes available points, locked points, spot
  market value, margin / lending position equity, accrued interest, and low
  confidence price count. Price confidence is advisory for points trading and
  API failures are surfaced in-page instead of being silently ignored.
- Added `scripts/testing/playwright_platform_health_check.py`, which starts an
  isolated `/tmp` QA server on a random non-5000 port and uses Playwright to
  exercise Job Center, notifications, share management, trading overview, and
  mobile viewport layout. The script writes JSON and Markdown evidence under
  the isolated runtime's `reports/qa/` directory.

## 2026.05.08 — Audit-cycle bugfixes (issues #183/#184/#185/#186/#187)

- **#183 chess invite 500 / FK corruption** — `bootstrap.schema.sql` now ships
  `game_matches` with the full new schema (`human_side`, `computer_difficulty`,
  `white_deleted_at`, `black_deleted_at`, and the latest `experiment 2:nn` /
  `experiment 3:dl` CHECK options) so the schema-rebuild path is no longer
  triggered on a fresh deploy. `routes/games.py::rebuild_game_matches_table`
  also wraps its rename/rebuild in `PRAGMA legacy_alter_table=ON` and
  `PRAGMA foreign_keys=OFF`, so existing instances upgrading from the old
  schema no longer leave `game_invites.match_id` pointing at a dangling
  `game_matches_old` table.
- **#184 trading conservative-price guard gaps** —
  `_assert_price_meta_allows_high_risk_use` now runs at every previously
  unguarded entry point: root derivatives open/close (`services/trading/funding.py`),
  grid bot create/scan (`services/trading/grid.py`), trial-credit forced sell
  (`services/trading/trial_credit.py`), bot trigger
  (`services/trading/bots/service.py`), market/limit order open + match
  (`services/trading/orders.py`), and margin position open
  (`services/trading/margin.py`).
- **#185 test fixture drift** — `test_account_lockout` lambdas accept the new
  `notify_security_event` kwarg; chat/community `CREATE TABLE users` fixtures
  carry `avatar_file_id` and `avatar_crop_json` columns; trading
  `EXPECTED_SETTINGS_KEYS` lists the new `trading.price_degrade_pause_*`,
  `trading.price_fusion_trade_min_provider_count`, `trading.warning_language`,
  and `trading.simulated_slippage_*` defaults.
- **#186 cached-fallback caller-side policy** —
  `_assert_price_meta_allows_high_risk_use` now distinguishes hard-block
  sources (`manual_root` always, `*_cached` for grid/margin/derivatives/
  trial-credit/bot usages) from caller-side allow paths (`market order`,
  `immediately executable limit order`, `limit order match` may use cached
  fallback when the operator opts into it via meta and the
  `trading.price_degrade_pause_*` policy is left disabled).
- **#187 errorhandler swallowed traceback** — `server.py`'s global
  `app.errorhandler(Exception)` now `app.logger.exception(...)` before
  returning the sanitized 500 JSON, guarded by an inner try/except so logging
  itself can't recurse.
- **Pre-launch report shortcuts** — added `scripts/on_live_reports/` with one
  `.py` entry per report type. Pure single-driver entries are symlinks; pytest
  / API / composite drivers are thin Python wrappers. See the directory
  README for the report-type → driver table.
- Origin-side breakage uncovered while integrating the audit fixes was also
  cleaned up: missing `ipaddress` import in `routes/system_admin.py`, missing
  `register_comfyui_workflow_routes` import in `routes/comfyui.py`, and three
  pentest scripts (`session_security_pentest.py`,
  `functional_permission_pentest.py`, `trading_stress_pentest.py`) that ran as
  entry points but imported `scripts.security.common_paths` without the
  `repo-root → sys.path` prologue. Storage-album / upload-security imports in
  the new `routes/file_sections/` and `routes/system_admin_sections/` modules
  also point at the canonical `services.storage.storage_albums` /
  `services.security.upload_security` paths instead of the legacy top-level
  module names.

## 2026.05.07-155

- Reorganized deep feature docs into bounded subdirectories so `docs/` root can
  stay focused on entry guides and cross-cutting references.
- Trading deep references now live under `docs/trading/`; video/media
  architecture under `docs/video/`; runtime-boundary docs under
  `docs/ops_boundaries/`; ComfyUI operator docs under `docs/comfyui/`; and Server
  Mode v2 spec bundles under `docs/server_mode/`.
- Added per-folder `README.md` entry files plus updated canonical doc index and
  release policy coverage so future doc growth follows the same placement
  rules.

## 2026.05.07-154

- Trading markets no longer become boot-ready on the very first live quote
  after a fresh boot or provider recovery. The first healthy quote now only
  starts a warmup candidate (`live_price_warmup_started_at`), and high-risk
  paths stay fail-closed until a second stable live quote confirms the market.
- This closes the startup/default-price jump hole where a market could flip
  from a seeded placeholder directly to a live API quote and immediately
  release bots, matching, or other risk-grade actions.
- `get_live_market_quote` and the trading price metadata now surface this state
  as `boot_pending` instead of silently treating the first quote as fully
  confirmed.
- Added regression coverage for:
  - schema support for `live_price_warmup_started_at`
  - first live quote keeping `place_order` blocked
  - first public live quote still keeping bot scans blocked
  - second stable quote releasing the boot-ready gate

## 2026.05.07-153

- Completed a real end-to-end functional audit instead of static inspection
  only. The existing smoke surface now re-checks public/auth/admin/security
  flows, PointsChain/economy/trading, `internal_test` routed trading, Cloud
  Drive, remote download guardrails, ComfyUI guardrails, video share/playback,
  snapshot restore, and runtime reset/reconnect.
- Fixed a fresh-import `SyntaxError` in `services/trading/schema_ddl.py` caused
  by a nested triple-quote example in the module docstring, and added direct
  `py_compile` regression coverage.
- `ensure_security_support_schema()` now explicitly creates `csrf_tokens`,
  preventing `/api/csrf-token` from failing on a fresh schema/bootstrap path.
- `OPTIONS` requests now bypass SMv2 context lookup so unknown-path preflight
  probes fail cleanly instead of throwing a server-side context error.
- Snapshot restore now stages runtime-secret files before moving them into the
  runtime tree, avoiding collisions with a repo-root `runtime` sentinel.
- Video share update/revoke now commits before the real-audit write path, and
  shared-video fetch now times out explicitly instead of hanging on a forever
  loading state.
- `internal_test` shadow-order writes now persist `tester_user_id` correctly,
  and tester-token creation now rejects malformed, timezone-aware, or already
  expired expiry timestamps with operator-facing guidance.
- `security/run_functional_smoke.sh` now generates internal-test tester-token
  expiry using local wall time, and fails clearly when free-port probing is
  blocked by a restricted environment instead of silently continuing with a
  blank port.

## 2026.05.06-147

- `services/points_chain/` has been split into a real
  `services/points_chain/` package with medium-grain boundaries: shared
  currency/schema/hash helpers and `ChainModeViolation` live in `schema.py`,
  while the full `PointsLedgerService` implementation now lives in
  `service.py`.
- Existing `from services.points_chain import ...` imports keep working through
  the package `__init__`, including compatibility for tests that monkeypatch
  `services.points_chain.time.time`.
- Source-based regression checks now inspect the canonical package files
  directly, so the root-level `services/points_chain.py` shim is no longer
  needed.
- `ServerModeService` no longer sidesteps a repo-root `runtime` blocker by
  silently creating `.runtime/`. If no explicit runtime base dir is available,
  a non-directory `runtime` path now fails closed; when an `IntegrityGuard`
  instance provides an app base directory, server-mode audit/HMAC files are
  routed under that app-local runtime tree instead.

## 2026.05.06-146

- `services/snapshots/` has been split into a real `services/snapshots/`
  package with medium-grain boundaries: shared schema/hash/signature helpers in
  `schema.py`, snapshot/archive/restore flow in `service.py`, and Server Mode
  v2 profile/checkpoint/audit flow in `server_mode.py`.
- Existing `from services.snapshots import ...` call sites keep working through
  the package `__init__`, and source-based regression checks now point at the
  canonical package files instead of a root-level shim.
- Snapshot and Server Mode helpers now tolerate a conflicting `runtime` file in
  the repo root by falling back to `.runtime/` for auto-generated local HMAC
  keys, removing an implicit path-shape assumption that broke
  `ServerModeService(snapshot_service=None)` test environments.

## 2026.05.06-145

- Trading workflow benchmark generation now preserves a stable frontend
  data shape: the default `1h` benchmark run writes to
  `workflows/trading_bot/benchmarks/workflow_template_benchmarks.json`, while
  non-canonical interval or relative-threshold variants write to suffixed
  auxiliary files. The frontend reads the canonical report through
  `/api/trading/workflow-template-benchmarks` instead of a static `public/data`
  asset.
- The shipped workflow benchmark asset now carries explicit `interval` and
  `use_relative_thresholds` metadata so the trading UI can label benchmark
  data correctly and tests can validate the asset shape directly.
- Added dedicated regression coverage for backtest-capacity projection,
  first-boot capacity probe recording, and the canonical workflow benchmark
  asset schema instead of relying only on broad trading/backtest integration
  tests.

## 2026.05.06-144

- `server_encrypted` Cloud Drive uploads no longer write plaintext to any
  temporary disk file before scanning. The upload path now exposes plaintext to
  scanners through an in-memory Linux `memfd` path and only writes ciphertext
  to the final storage location, closing the remaining plaintext-at-rest window
  during upload scanning.
- Trading backtest auto-fetch routes now keep the overall `backtest_max_candles`
  cap separate from the per-request provider batch limit, and they fall back to
  the legacy default cap when lightweight/test trading service stubs do not
  implement `get_max_backtest_candles()`.

## 2026.05.06-143

- API routes now fail with a consistent JSON envelope instead of Flask's
  default HTML 5xx page when an unhandled exception escapes `/api/...`, while
  non-API requests keep a minimal plain-text 500 fallback.
- Cloud Drive's security model docs now spell out the trust boundary between
  `standard_plain`, `server_encrypted`, and strict `e2ee` storage so users can
  see exactly when the server/root can read plaintext and when they cannot.
- Server-encrypted Cloud Drive uploads now scan a dedicated temporary plaintext
  file and only write ciphertext to the final storage path, closing the old
  window where the permanent storage location could briefly contain plaintext.
- Snapshot restore now fails closed if the server cannot enable maintenance
  mode before the restore begins, instead of silently continuing with a dirty
  runtime state.
- PointsChain wallet rebuild is now transaction-safe even when called without
  an outer transaction, preventing a crash between `DELETE` and re-insert from
  leaving all wallet rows empty.
- ComfyUI workflow import now rejects oversized workflow JSON, excessive node
  counts, and overly deep nesting to reduce denial-of-service risk from giant
  crafted workflows.
- `websocket-client` is now declared in the minimal runtime requirements,
  matching the live trading websocket provider code path used by Binance/Coinbase
  streaming. `requirements.txt` remains the full compatibility aggregate.

## 2026.05.06-142

- `test_shadow_wallets` is now aligned with the points-only trading shadow path
  instead of the older `soft_* / hard_*` split. Fresh snapshot schemas create
  `balance_points`, `frozen_points`, `total_points_earned`, and
  `total_points_spent`, while migrations fold any legacy soft/hard values into
  those canonical fields so internal-test trading, funding settlement, and
  chain-backed margin opens stop failing on missing shadow-wallet columns.
- The Server Mode v2 smoke harnesses now point back at the actual tutorial
  bundle under `docs/examples/server_mode_v2/` instead of an empty
  `scripts/server_mode_v2/` directory, so `security/server_mode_v2_token_smoke.py`
  and `security/server_mode_v2_full_smoke.py` run the same scripts the docs and
  tests reference.
- `docs/examples/server_mode_v2/06_full_feature_smv2.sh` now passes
  `target_username` when rotating an `internal_test` login token, matching the
  current server-side requirement for single-account-bound internal-test login
  tokens.
- `docs/examples/server_mode_v2/05_stress_smv2.sh` no longer burns the full
  burst after the first blocked tester-token response; it now exits the rate
  limit probe as soon as the expected block behavior is proven and shortens the per-request
  timeout, eliminating the prior full-smoke timeout on script 05.

## 2026.05.05-141

- Trading fused-price trust semantics are now less sensitive in normal market
  conditions. Partial order-book coverage or auto-excluding a few unhealthy
  provider rows no longer automatically forces the frontend into
  `reference 價格降級` / `risk_grade_usable=false` as long as enough healthy
  providers still produce a valid risk-grade price.
- The trading page now keeps a green state for `warning-only` price fusion
  diagnostics and explicitly says that some providers were excluded while the
  current risk-grade price remains usable.
- Yellow/low-trust trading warnings are now reserved for actual degraded
  conditions such as stale/fallback provider input, conservative mode, cached
  or manual price paths, or other real high-risk price-health failures.

## 2026.05.05-140

- Audit chain / Integrity Guard 對正常維運的敏感度已收斂：root 被動查看
  `/api/admin/health`、`/api/admin/health/audit-chain`、`/api/admin/audit` 時，
  若 audit chain 斷裂，系統現在只會回傳 `critical`、`operator_action_required`
  與 `auto_lockdown_applied=false`，不再因單純查狀態就自動切進 maintenance mode。
- Integrity Guard `strict mode` 在重啟 / 更新後若看到 high-risk findings，
  啟動流程現在會記 audit warning 並繼續提供服務；真正的 `GO_LIVE` /
  pre-production entry 仍會因這些 findings 被擋住，直到 root review 完成。

## 2026.05.05-139

- Trading UI warning text is now explicit when only `reference price` remains:
  the frontend says `目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用`
  instead of implying the whole market has no price.
- Production report upload is now a verified path instead of a loose JSON
  intake: uploads must include `raw_report`, `sha256` `report_hash`,
  `hmac_sha256` `signature`, and `key_version`, and the server recomputes the
  hash plus verifies the signature before the report can satisfy production
  gate requirements.
- `internal_test` login tokens are no longer shared across multiple accounts.
  Root must bind each issued token to a single target account, and only that
  account can use it on `/api/login` while the server is in `internal_test`
  mode.
- The root launch-check upload helper now explains signed-report requirements
  directly in the UI, and failed production-report verification surfaces a
  concrete reason instead of a generic upload failure.
- Cloud Drive PDF preview now uses an iframe/new-tab fallback path that works
  under the site's CSP (`object-src 'none'`), so strict E2EE and
  server-encrypted PDFs no longer fail because the browser blocks
  `object/embed`.
- Strict E2EE video pages no longer prompt for decryption immediately on page
  load; users must explicitly press `開始 E2EE 播放` before fragment lookup,
  password prompt, and browser-side decrypt begin.
- The audit / PointsChain recovery buttons are now wired to the correct chains:
  the audit page repairs audit/integrity chains, while the PointsChain recovery
  card owns the `一鍵處理 PointsChain 異常` action and its own status line.
- Tester-token APIs now expose `GET /api/tester/shadow-role` and
  `GET /api/tester/shadow-wallet` in addition to the existing POST mutation
  routes, so the documented read paths no longer return `404`.

## 2026.05.05-138

- Root 的 ComfyUI 模型匯入區現在新增 `放大模型 / Upscaler` 類型，下載 Civitai 模型或直接上傳本地模型檔時，都可以正確落到預設的 `ComfyUI/models/upscale_models/`。
- 同一個匯入區也新增「下載到哪個路徑」欄位，可填 `ComfyUI/models/` 底下的相對路徑；若留空則依模型類型自動選用預設資料夾。後端會拒絕 absolute path、`..` 與任何跳出 `ComfyUI/models/` 的路徑。
- 補上回歸：前端已接上 `upscale` 類型與路徑提示，Civitai download / model upload 都可保存 `relative_dir`，而路徑穿越會被拒絕。

## 2026.05.05-137

- `上線前檢查` 的 playbook / tests 捷徑不再直接跳 repo-relative `docs/...` 而導致 `NOT FOUND`。root 現在可透過新 API `GET /api/root/launch-check/doc?path=docs/...` 在站內直接閱讀 production gate playbook / 測試文件。
- 每一張 production gate report 卡現在都有 `上傳報告` 入口，可直接貼上 JSON 或選擇 `.json` 檔後送往 `/api/root/production-report/upload`；上傳成功後會即時重整 B 區狀態。
- 新增回歸涵蓋：launch-check 文件檢視只允許 `docs/` 內安全路徑、path traversal 會被拒絕，且前端 upload/doc panel 的關鍵元素與事件綁定存在。

## 2026.05.05-136

- 安全中心的 root 上線前測試面板現在不再只靠一個混合任務列表。滲透、越權 / 權限濫用、全功能、壓力四種測試都各自有獨立卡片、獨立進度條、最近任務狀態與詳細 log，操作上不再需要從混雜 job list 猜哪段輸出屬於哪種測試。
- 新增 root-only `POST /api/root/security-tests/privilege`，直接驅動 `security/functional_permission_pentest.py`，可從安全中心啟動越權 / permission-abuse 測試；若需要，也可顯式帶 `--destructive` 跑高風險 guard。
- root 安全測試面板的前端綁定也同步補齊：四種測試都會顯示人性化狀態、progress 與 log，而且 `loadSecurityTestJobs()` 會把最新 job 正確分流到對應卡片，而不是互相覆蓋。

## 2026.05.05-135

- `open_margin_position()` now routes margin position inserts through the
  resolved `margin_positions` table for the active Server Mode v2 context
  instead of hardcoding `trading_margin_positions`.
- Internal-test margin opens now populate `tester_user_id` when writing shadow
  rows, so `test_shadow_margin_positions` inserts follow the shadow schema
  shape instead of failing or silently drifting back toward production-only
  assumptions.
- Internal-test margin collateral and fee ledger writes now pass the active
  trading context into `_ledger(...)`, ensuring chain-backed shadow margin opens
  write to `test_shadow_ledger` / `test_shadow_wallets` instead of production
  `points_ledger` / `wallets`.
- Added regressions proving:
  - shadow margin opens create rows only in `test_shadow_margin_positions`,
  - chain-backed shadow margin opens leave production `points_ledger` untouched,
  - shadow ledger rows record the expected tester namespace.

## 2026.05.05-134

- Server Mode v2 的 `上線前檢查` 不再把「已先切成 production」或「已先手動套 production 等級安全設定」誤當成 preflight 前置條件。A 區現在只保留真正的切換前 blocker（鏈 / 完整性 / readiness / anomaly / reports），而 production profile 的 HTTPS、audit chain、Integrity Guard、browser-only 等會明確標成 `切換時自動套用`。
- root 若在 `dev_ready`、`test`、`internal_test` 等非 production 模式先做上線前檢查，現在不會再因為目前尚未套用 production posture 而被一排紅燈誤導；真正的 `GO_LIVE` 切換仍由 mode switch 路徑與 production gate 共同把關。

## 2026.05.05-133

- Trading market registry 的 seed drift 現在不再是隱性風險。`trading_markets_registry` 新增 `registry_source` 與 `seed_version`，root 後台與 API 也會回傳 `catalog_seed_version`、`seed_sync_status`、`seed_sync_reasons`、`seed_sync_message`，明確標示某個市場是 `catalog_seed` 還是 `custom`，以及是否已偏離目前 code catalog。
- 這一刀沒有把 DB 悄悄蓋回 catalog。runtime 仍以 DB registry 為 source of truth；若 seeded 市場被 root 調整過，後台只會顯示 `drifted`，讓 drift 可見、可審計，而不是自動回寫。
- migration / bootstrap 也同步升到 schema version `30`，並新增 regression：seeded 市場會回 `seed_version` / `current`，root 自建市場會回 `custom`，修改 seeded 市場後會顯示 `drifted`。

## 2026.05.05-132

- Root 的 GitHub 伺服器更新流程現在會在成功 fast-forward 後，先依本次 `git diff` 變動到的受保護檔案重建 Integrity Guard baseline，再做後續 integrity scan。這樣更新後不會因為「剛套用的新版本檔案」立刻全部變成 pending findings。
- 這次 baseline refresh 只接受本次更新涉及的檔案，不會粗暴清空所有 pending findings；若 repo 內另有與本次更新無關的異常，後續 integrity scan 仍會把它們保留下來。
- 新增 regression 驗證：`rebaseline_paths(...)` 只會接受指定檔案、其他 finding 仍維持 pending；server update route 也明確要求 baseline refresh，而不是單純 rescan。

## 2026.05.05-131

- Shared unlisted video pages no longer get stuck on a generic `讀取中...` state when the playback step discovers that a share password is still required. The browser now treats `password_required` / `password_invalid` / `password_locked` responses from any shared-video API step as a signal to reopen the unlock form instead of leaving the page looking frozen.
- The shared-video page also now updates its loading copy from a static `讀取中...` placeholder to concrete states such as `正在讀取分享資訊...` and `此分享影音需要先解鎖`, so E2EE shared playback failures are easier to distinguish from password-gated shares.

## 2026.05.05-130

- Trading provider fallback discipline 的第一刀已收斂成「價格信任等級」而不是全面改寫交易規則：`test_live_price_provider` 現在會被標成 `confidence=low`、`synthetic_test_provider=true`，並在 `reference / risk-grade` context 中明確標示 `risk_grade_usable=false`。
- `manual_root`、最後健康快取、以及 degraded / stale / fallback provider input 都會明確回傳 `risk_grade_usable=false` 與對應 warning；cached / degraded 高風險價格仍會被後端 hard block，而 synthetic test provider 只保留給單測與注入測試，不可由 root 設定成正式 `price_source`。
- 前端交易頁與 root 市場管理診斷已同步顯示 `風控可用 yes/no`，並在市價單 / 融資風險估算中同時檢查 `high_risk_blocked || risk_grade_usable === false`，避免把 synthetic、manual 或 cached 價格靜默當成 production risk-grade 使用。

## 2026.05.05-129

- Server Mode v2 的 Trading Phase 5b G-5 已把 funding publish / settlement world split 打通：funding snapshot 現在會依 `funding_channel_key(market, ctx)` 發佈到 mode-aware channel，production 與 `internal_test` 不再共用 funding state。
- 新增 `publish_funding_rate_snapshot(...)`、`get_funding_rate_snapshot(...)` 與 `settle_funding_adjustment(...)` 這組 canonical funding path；若 settlement 想拿 production snapshot 去結算 shadow world（或反過來），會先觸發 `assert_same_world(...)` 拒絕，而不是錯寫 wallet / ledger。
- `internal_test` funding settlement 現在只會落到 `test_shadow_wallets` / `test_shadow_ledger`，即使 shadow funding feature flag 被打開，也不會污染 production `points_ledger`、wallet balance 或 chain block 計數。

## 2026.05.05-128

- Server Mode v2 的 Trading Phase 5b G-4 已把 liquidation source/sink 明確 mode-lock：liquidation source 現在經由 `liquidation_target_table(ctx)` 指向 `trading_margin_positions` 或 `test_shadow_margin_positions`，settlement sink 則經由 `liquidation_settle_table(ctx)` 指向 production 或 shadow wallet world。
- `close_margin_position(... force_liquidation=True)` 與 `scan_margin_liquidations()` 現在都會先做 same-world guard；production liquidation 繼續沿用原流程，但 `internal_test` liquidation 會在任何 reserve / ledger / chain side effect 之前明確拒絕，避免 shadow liquidation 寫到 production wallet、points ledger 或 chain。
- 這輪也補了 `test_shadow_margin_positions` schema 與 regression：internal_test 不會拿 production `position_uuid` 來強平，手工插入的 shadow margin position 也只會收到「shadow liquidation 尚未支援」的明確錯誤，而不會留下半套 production mutation。

## 2026.05.05-127

- Server Mode v2 的 Trading Phase 5b G-3 已把 in-memory matching engine orderbook 真的改成依 `matching_orderbook_key(market, ctx)` 分 namespace，而不是只靠 `market_symbol` 當 key；同一個 `BTC/POINTS` 在 production、`test`、以及不同 `internal_test tester_id` 下都會落到不同的 matching book。
- `match_open_limit_orders()` 現在先依 routed world hydrate 對應 namespace 的 open limit orders，再從該 world 的 in-memory book 取 order UUID 進行撮合；`cancel_order()`、`_execute_order()` 與 trial-credit reclaim 取消單也會同步清掉各自 namespace 內的幽靈單。
- 這輪的關鍵防線是：shadow tester 7 的 open limit order 不會再被 tester 8 或 production matcher 看見，避免「同一張 test_shadow_orders 表內不同 tester 共用 orderbook」的 cross-world/cross-tester 撮合污染。

## 2026.05.05-126

- Server Mode v2 的 Trading Phase 5b G-2 已把交易引擎內與 `orders / positions / points_ledger / wallets` 相關的主要 SQL 路徑收斂成 runtime routing：`user_dashboard`、grid bot scan、root simulated reset、verification / safe-mode replay helpers 現在都會依 mode 解析到 production 或 shadow 表，而不是再直接讀寫固定的 production 表名。
- `test_shadow_wallets / test_shadow_orders / test_shadow_positions / test_shadow_ledger` 的 shadow schema 已補齊 production 路徑需要的核心欄位，讓 internal_test world 可以承接交易凍結、trial / chain split、ledger metadata 與 safe-mode verification，而不再只是一組過於簡化的示意表。
- 這輪是架構強化而不是新 UI：目標是讓 SMv2 internal_test 的交易資料路徑更接近真正的 dual-world routing，並確保 production wallet / ledger 不會因 shadow-mode 交易引擎讀寫而被污染。

## 2026.05.05-125

- ComfyUI 新增 `Workflow 工作台`：可把目前表單匯出成經過安全清洗的 workflow JSON，再匯入成 private/public preset、保存描述與可見性，之後可一鍵套用、一鍵重跑與匯出已保存的 preset JSON。
- workflow JSON 進入系統前現在會做伺服器端安全驗證：拒絕壞 JSON、absolute path、shell / exec / command 類節點、外部 URL 與可疑敏感欄位；缺少 checkpoint / LoRA / ControlNet / workflow node 時，執行 preset 會明確回 `409`，不再靜默 fallback。
- root 可把自己的 workflow preset 發布為 official preset，private preset 仍只允許擁有者存取；workflow run 會保存 seed / CFG / steps / LoRA / ControlNet 等完整參數，方便之後比對與重跑。

## 2026.05.05-124

- 交易市場不再只靠程式內 hardcode：新增 root-only trading market registry，可在後台新增 / 編輯 / 停用市場，調整 `precision / lot size / tick size`，並維護各交易所 provider mapping 與排序。
- `provider mapping` 與 `risk-grade` 啟用現在有明確 probe 與 audit：root 修改市場或 provider mapping 會留下 before/after audit log；若 depth provider 不足或 probe 未達標，市場不可啟用 `risk-grade` 用途。
- disabled market 只會阻擋新下單，不會破壞既有歷史、持倉與報表；下單與融資路徑也會立即套用市場 precision、lot size、tick size 與新開關條件。

## 2026.05.05-123

- strict E2EE 影音新增 `E2EE Streaming v2` 基礎：瀏覽器可使用 encrypted chunk manifest、逐段密文下載、Web Worker 解密與 `MediaSource` 播放；若沒有 v2 manifest、裝置不支援 Worker / MediaSource / WebCrypto，會明確退回舊版完整解密播放，而不是假裝成功或誤走 HLS。
- 新增 `/api/videos/<id>/e2ee-stream-v2/manifest`、`/api/videos/<id>/e2ee-stream-v2/chunks/<chunk_index>` 與對應 shared token 路由；這些端點永遠只回密文 chunk，不接收 `raw_file_key`、`e2ee_password`、`vk`，也沿用分享 token 的過期、撤銷與最大觀看次數保護。
- 影音前端現在可在 strict E2EE 路徑下區分 `browser_e2ee_stream_v2` 與 `browser_e2ee_full_fallback`，共享頁也會明確顯示「讀取分享授權 / 下載加密影音 / 瀏覽器端解密」等階段提示，並在分享授權無效或被竄改時顯示人性化錯誤。

## 2026.05.05-139

- Trading UI warning text is now explicit when only `reference price` remains:
  the frontend says `目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用`
  instead of implying the whole market has no price.
- Production report upload is now a verified path instead of a loose JSON
  intake: uploads must include `raw_report`, `sha256` `report_hash`,
  `hmac_sha256` `signature`, and `key_version`, and the server recomputes the
  hash plus verifies the signature before the report can satisfy production
  gate requirements.
- `internal_test` login tokens are no longer shared across multiple accounts.
  Root must bind each issued token to a single target account, and only that
  account can use it on `/api/login` while the server is in `internal_test`
  mode.
- The root launch-check upload helper now explains signed-report requirements
  directly in the UI, and failed production-report verification surfaces a
  concrete reason instead of a generic upload failure.
- Cloud Drive PDF preview now uses an iframe/new-tab fallback path that works
  under the site's CSP (`object-src 'none'`), so strict E2EE and
  server-encrypted PDFs no longer fail because the browser blocks
  `object/embed`.
- Strict E2EE video pages no longer prompt for decryption immediately on page
  load; users must explicitly press `開始 E2EE 播放` before fragment lookup,
  password prompt, and browser-side decrypt begin.
- The audit / PointsChain recovery buttons are now wired to the correct chains:
  the audit page repairs audit/integrity chains, while the PointsChain recovery
  card owns the `一鍵處理 PointsChain 異常` action and its own status line.
- Tester-token APIs now expose `GET /api/tester/shadow-role` and
  `GET /api/tester/shadow-wallet` in addition to the existing POST mutation
  routes, so the documented read paths no longer return `404`.

## 2026.05.05-122

- `deploy.sh` now supports `--with-civitai-key '<CIVITAI_API_KEY>'`, so first-time deployments can seed root-only Civitai search/download access without manually editing `.env`.
- `python3 server.py --doctor` now reports deployment environment readiness, and the canonical offline root recovery entrypoint is `python3 scripts/admin/root_recovery.py`.
- Deployment docs and quickstart guides now explain that these checks are advisory capability hints rather than hard blockers for normal deployment.

## 2026.05.05-121

- Cloud Drive preview now treats archives and PDFs more like a normal file manager: archive preview renders a structured file/folder list, plain and `server_encrypted` PDFs prefer the browser's native PDF viewer path, and strict `e2ee` PDFs render through browser-side decrypt plus `object/embed` with a new-tab fallback.
- E2EE file preview now reuses the most recently successful passphrase within the current login session before prompting again, reducing repeated password dialogs when opening multiple E2EE files with the same secret.
- Shared strict E2EE video pages now show explicit progress phases (`share auth`, `ciphertext download`, `browser decrypt`) instead of looking frozen on a generic loading state, and the health indicator UI now hides the text label while the server remains green/healthy.

## 2026.05.05-120

- Expanded the Server Mode v2 example bundle under `docs/examples/server_mode_v2/` with four new runnable scripts: `04_pentest_smv2.sh`, `05_stress_smv2.sh`, `06_full_feature_smv2.sh`, and `07_privilege_escalation_smv2.sh`.
- Added `security/server_mode_v2_full_smoke.py`, an isolated runtime harness that runs the full six-script SMv2 tutorial bundle (`01`, `02`, `04`, `05`, `06`, `07`) and then asserts that shadow-table activity did not leak into production wallet / ledger tables.
- Synced README, Traditional Chinese README, developer guide, QA map, pentest guide, and the examples README so the new SMv2 tutorial bundle and full smoke harness are documented as the canonical live-http coverage route.

## 2026.05.05-119

- Expanded the security validation script suite instead of only relying on product tests: `functional_permission_pentest.py` now covers root-only ComfyUI / Civitai search, inspect, model upload, and download-job endpoints across anonymous, user, manager, and root roles.
- `trading_stress_pentest.py` now forces a conservative fused-price state and verifies that degraded `risk-grade price` input blocks high-risk market orders and financing opens rather than silently leaking degraded data into trading.
- `video_module_pentest.py` now covers owner-side unlisted share-link regeneration while confirming manager mutation remains blocked, strict E2EE shared-video envelope boundaries, and revoked share-link blocking; `run_functional_smoke.sh` also confirms that the offline `scripts/admin/root_recovery.py` CLI remains available.

## 2026.05.05-118

- `root` 已正式脫離一般 Web 忘記密碼流程：`/api/password-reset/request` 與 `/api/password-reset/confirm` 對 root 帳號都會拒絕，避免把最高權限帳號降級成一般 email token / review reset 模式。
- 新增離線 `scripts/admin/root_recovery.py`，可在實體 runtime 上直接重設 root 臨時密碼、撤銷既有 session、清掉 CSRF token，並要求下次登入立刻修改密碼。
- README、Admin Guide、CLI Playbook、Troubleshooting、API Reference 與 QA 文件已同步改成以 offline root recovery CLI 為正式補救路徑。

## 2026.05.05-117

- Added root-only Civitai search/filter support on the local ComfyUI model-import panel: keyword search, base-model filter, checkpoint / LoRA / embedding / ControlNet type filter, and Safe/NSFW filtering now hit the official Civitai model search API instead of requiring users to paste a page URL up front.
- Search results now summarize latest-version metadata before download, including version name, file size, hash hints, compatible/base models, and an explicit “帶入下載區” step; downloads also require a second confirmation dialog before writing into the local ComfyUI `models/` tree.
- Added human-readable handling for missing Civitai API keys and interrupted downloads, extended functional smoke to probe the new search endpoint’s API-key guard, and updated API / QA / admin / developer docs to match the new root-only workflow.

## 2026.05.05-116

- Fixed live ComfyUI `inpaint` / `outpaint` workflow validation against current `VAEEncodeForInpaint` by explicitly setting `grow_mask_by`, so real jobs no longer fail with `Required input is missing: grow_mask_by`.
- Added root model import source mode switching: the local ComfyUI panel can now either inspect/download from a Civitai URL or upload a local model file directly into the appropriate `models/` folder with extension validation and audit logging.
- Added `scripts/comfyui_feature_probe.py` plus regression coverage so operators can live-smoke `status`, `models`, `txt2img`, `img2img`, `inpaint`, `outpaint`, `upscale`, ControlNet availability, and history rerun without hand-building each request.

## 2026.05.05-115

- ComfyUI generation now supports `img2img`, `inpaint`, `outpaint`, ControlNet-assisted workflows, upscale-model selection, and generation history replay as first-class UI/API features instead of only plain txt2img.
- `GET /api/comfyui/models` now exposes capability metadata for generation modes, ControlNet families/models/preprocessors, and upscale models; `POST /api/comfyui/generate` accepts multipart source/mask/control images and rejects missing models, invalid image formats, missing workflow nodes, or out-of-range ControlNet strength with human-readable errors.
- Added `/api/comfyui/history`, `/api/comfyui/history/<history_id>/rerun`, and `/api/comfyui/image-preview` so saved inputs can be restored, rerun, and previewed without silently re-uploading hidden state; the mobile form and release docs were updated to match.

## 2026.05.05-114

- Trading provider input now prefers websocket ticker/depth feeds for Binance, OKX, Coinbase, and Kraken, but keeps websocket strictly as provider input instead of replacing `reference price` / `risk-grade price` semantics.
- `GET /api/trading/live-price` and root `GET /api/root/trading/price-fusion-status` now expose canonical transport state (`connected`, `fallback`, `stale`, `degraded`, `confidence`, `provider_count`, `last_update_at`, `exclusion_reason`, `transport_state`) so UI, smoke checks, and risk controls can audit degraded or fallback states explicitly.
- Fixed a quality-filtered single-source fallback bug in fused-price diagnostics and added dedicated regression coverage for websocket updates, disconnect fallback, malformed provider payload rejection, and blocking risk-grade price when only degraded single-source data remains.

## 2026.05.05-113

- Trading price semantics are now explicit across the site instead of treating every number as a generic market price: `reference price` is for display, charting, and general valuation, while `risk-grade price` is reserved for financing, liquidation, margin maintenance, unrealized PnL, bot risk checks, and trading limits.
- `GET /api/trading/live-price` now returns canonical `price_type`, `source`, `confidence`, `stale`, `degraded`, and `provider_count` fields plus `reference_price_context` / `risk_grade_price_context`; `GET /api/trading/reference-prices` now returns the same canonical reference-price context metadata.
- The trading UI now labels current price, spot valuation, spot PnL, margin risk, and order-entry estimates with their actual price usage, and high-risk operations now show human-readable "risk-grade price unavailable" blocking messages instead of silently relying on ambiguous fallback pricing.

## 2026.05.05-112

- The video watch page now includes an E2EE share-management panel for unlisted videos, exposing share state, remaining views, password status, expiry, max views, copy/regenerate/revoke controls, and the explicit warning that fragment loss is unrecoverable.
- Share-link management now stays consistent with the documented permissions: manager/root can update or revoke unlisted share links, while strict E2EE regeneration still requires a fresh browser-side share envelope from the publisher's original password.
- Added richer regression coverage for share state payloads, manager-side share-link updates, fragment-loss/tamper messaging, and the mobile layout of the new management controls.

## 2026.05.05-111

- Added same-origin `hls.js` playback fallback for prepared HLS media, so desktop Chrome / Firefox / Edge can play HLS reliably without breaking Safari native HLS.
- Video playback APIs now expose `player_strategy`, `stream_warning`, and `hls_js_url`, and the UI now surfaces human-readable HLS/direct/E2EE playback states instead of silently guessing.
- Shared video pages now use the same HLS fallback rules as the main video page; strict E2EE shares still stay browser-side, while HLS.js failures fall back to direct stream with explicit error messaging.
- Added release-level regression coverage for local `hls.js` loading, shared-page fallback wiring, and HLS/E2EE playback hints.

## 2026.05.05-110

- Added [ENCRYPTION_RUNTIME_BOUNDARY.md](ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md) as the canonical operator/engineer trust-boundary document for `standard_plain`, `server_encrypted`, strict `e2ee`, and E2EE shared-video envelopes.
- Added [EXTERNAL_API_COMMAND_MATRIX.md](EXTERNAL_API_COMMAND_MATRIX.md) to inventory the upstream exchange, Civitai, and ComfyUI commands currently used by the project, plus nearby capabilities not yet wired.
- Added a regression proving that a runtime engineer can decrypt `server_encrypted` data with the runtime file key, but cannot decrypt strict `e2ee` data from runtime state alone.

## 2026.05.05-109

- Unlisted E2EE videos can now be shared without downgrading strict E2EE into server-side HLS: the owner enters the original E2EE password once at publish time, the browser re-wraps the file key into a share envelope, and viewers use the complete link fragment plus an optional second-layer share password for browser-side playback.
- Added video share-link management APIs for owners/managers: revoke or regenerate share links, plus optional expiry time and maximum view count controls.
- Shared video routes now reject forbidden secret fields (`raw_file_key`, `e2ee_password`, `vk`), enforce password retry lockouts, honor expiry / max views, and count access consistently across metadata/playback routes.

## 2026.05.05-108

- Video streaming Phase C-1 now auto-prepares HLS derivatives for eligible public/unlisted media and `server_encrypted` uploads on publish, while keeping publish success intact if derivative packaging fails.
- Video watch pages now show human-readable stream status and let owners or managers re-run HLS preparation directly from the UI.
- The video publish form now explains when HLS derivatives are attempted automatically.

## 2026.05.05-107

- Added three more points-quoted trading markets to the centralized market catalog: `XRP/USDT`, `BNB/USDT`, and `PAXG/USDT` display pairs backed by internal `XRP/POINTS`, `BNB/POINTS`, and `PAXG/POINTS` symbols.
- Added Phase C-1 media streaming foundation: HLS derivative schema/service, `prepare-stream` and `stream-status` media routes, `playback` decision API, and protected HLS manifest/segment routes for prepared plain or `server_encrypted` video.
- Video frontend now prefers prepared HLS playback when available and falls back to the existing direct `/stream` route when no derivative exists or the browser lacks native HLS support.

## 2026.05.04-106

- Cloud Drive audio and video preview now use the native `/preview/content` stream URL instead of fetching a blob first, so browsers can handle streaming media previews more reliably.
- Clarified the attachment-storage wording: chat / DM / announcement attachments only write into `/attachments/` when those attachment actions are actually used; this is a storage path convention, not a separate built-in module.
- Added `docs/video/VIDEO_STREAMING_ARCHITECTURE.md` as the canonical Phase C design for HLS / segmented media streaming, including the split between `standard_plain`, `server_encrypted`, and strict `e2ee` media behavior.

## 2026.05.04-105

- Added an in-page explanation beside `設定 -> 交易所參數 -> 價格來源與融合比例`, so root can see exactly how `auto_depth` works: front `10` order-book levels, midpoint `±1%` band, and `depth_score = min(bid_notional, ask_notional)` before the system normalizes weights to `100%`.

## 2026.05.04-104

- Added `docs/API_REFERENCE.md` as the canonical implemented API route map, so developers no longer need to piece together current endpoints from `For_developer.md`, trading docs, and scattered QA notes.
- Added `docs/CLI_ADMIN_PLAYBOOK.md` as the official `curl` / shell playbook for root, admin, and developer site operations in isolated runtimes.
- Updated `README.md`, `docs/README.md`, `docs/README.zh-TW.md`, `docs/For_developer.md`, and `docs/11_QA_TESTING.md` to point to these new dedicated API / CLI documents.

## 2026.05.04-103

- The account/admin area no longer hardcodes several toolbars with inline
  desktop-only flex layouts, so the existing mobile responsive rules can
  actually collapse those control rows into usable single-column stacks on
  narrow screens.
- The admin users table now sits inside a dedicated horizontal scroll wrapper,
  making large account-management tables usable on phones instead of forcing
  the full page width to overflow.

## 2026.05.04-102

- The root trading settings UI now renders manual fusion weights as compact
  per-provider chips instead of a large full-width grid, reducing empty space
  in `設定 -> 交易所參數 -> 價格來源與融合比例`.
- Each provider weight input now sits inline beside its exchange label with a
  trailing `%` marker, while the helper text clarifies that values do not need
  to sum to exactly `100` because the backend normalizes them automatically.

## 2026.05.04-101

- `security/stress_test.py` now supports a duration-based flood mode in
  addition to fixed request-count mode, including per-worker burst sizing and a
  burst interval to simulate short HTTP flood spikes against authorized
  loopback or owned staging targets.
- Root security-test jobs can now launch the same duration-based stress mode
  with `duration_seconds`, `max_requests`, `burst_size`, and
  `burst_interval_ms`, while keeping the existing count-based mode compatible.

## 2026.05.04-100

- The pre-push workflow now auto-cleans repo-local Python caches and a
  mistakenly generated repo-root `runtime/` before running the blocking
  validation suite.
- `scripts/pre_push_checks.py --clean` now removes both safe cache artifacts
  and a repo-root `runtime/` directory, while still refusing to touch tracked
  files or protected runtime/report paths.

## 2026.05.04-099

- Server mode audit export artifacts no longer spill into repo-root `security/audit_exports/`; they now write under `runtime/reports/server_mode_audit/` with the rest of runtime-generated files.
- `.gitignore` no longer masks `security/audit_exports/`, so any future regression that writes audit exports back into the source tree will show up immediately in `git status`.
- Snapshot / server-mode regression coverage now asserts the runtime audit export path directly.

## 2026.05.04-098

- Runtime DB, logs, storage, chat data, generated secrets, TLS cert/key, and integrity manifest now default under `runtime/` instead of scattering across the repo root.
- `HACKME_RUNTIME_DIR` still works for isolated runs, but the relative default layout is now `runtime/database`, `runtime/logs`, `runtime/storage`, `runtime/cert.pem`, `runtime/.chain_seed`, and related files.
- `btc_trade_bridge` now follows the same runtime root for its default DB and chain seed lookup, so it no longer drifts back to repo-root `database.db` or `.chain_seed`.
- Snapshot runtime-secret handling now understands the `runtime/` prefix and keeps restore/reset logic aligned with the new runtime layout.

## 2026.05.04-097

- Margin / lending position detail now shows `損益平衡價` alongside `逐倉估算強平價`.
- Break-even price now includes `開倉費 + 累積利息 + 預估平倉手續費`, so it reflects the real exit threshold instead of raw entry price only.
- Frontend live margin risk now recomputes interest, next billing time, break-even price, and liquidation price on the same `2` second rhythm as live price refresh, so hourly interest accrual is reflected without waiting for a full dashboard reload.

## 2026.05.04-096

## Highlights

- `GET /api/trading/live-price` now reports `refresh_interval_ms = 2000`,
  matching the current 2-second trading page polling interval instead of
  advertising the old 1-second cadence.
- This keeps live-price API metadata aligned with the frontend trading wallet
  and PnL refresh loop, so diagnostics no longer claim a faster refresh than
  the UI actually uses.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_reference_prices.py tests/test_frontend_economy.py tests/test_release_policy.py`
- `PYTHONPATH=. python3 -m pytest -q tests`
- isolated live API validation script from the final open-issues review artifacts
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-095

## Highlights

- Cloud Drive folder browsing now supports the common double-click-to-open
  interaction, so users no longer have to rely only on the right-side `開啟`
  button to enter a folder.
- The explicit `開啟` button remains as a fallback, and the double-click target
  excludes action buttons so download/delete controls do not accidentally
  navigate.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_drive_preview.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-094

## Highlights

- Community announcements now support in-place editing for manager/root users.
  Admins can revise title, content, and pinned state directly instead of
  deleting and re-posting the announcement.
- The announcement editor now switches cleanly between create mode and edit
  mode, including different submit text and form reset on cancel.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_community_permissions.py tests/test_frontend_community_layout.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-093

## Highlights

- The trading reference chart now offers a broader built-in indicator set:
  `MA10`, `MA30`, `EMA50`, `RSI14`, and `KD(9,3,3)` were added on top of the
  existing MA / EMA / Bollinger overlays.
- `RSI14` and `KD` now render in a dedicated oscillator subpanel, so the
  trading page can show trend overlays and overbought/oversold signals without
  squashing everything onto the same price axis.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-092

## Highlights

- Chat stickers now use emoji-style quick buttons and render sent stickers as
  real emoji glyphs instead of text labels such as `微笑` or `感謝`.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_chat.py tests/test_release_policy.py`
- `git diff --check`

## 2026.05.04-091

## Highlights

- Trading market metadata is now centralized in `services/trading_markets.py`,
  so internal symbols, display aliases, provider IDs, default seeded markets,
  and BTC_trade support all come from one catalog instead of multiple hardcoded
  maps.
- Trading live-price, reference-price, backtest, market ordering, wallet spot
  sections, and root price-fusion market selection now consume the same market
  definitions, reducing the work needed to add future points-quoted assets such
  as `SOL` or `GOLD`.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_markets.py tests/test_trading_reference_prices.py tests/test_trading_engine.py tests/test_frontend_economy.py`
- `git diff --check`

## 2026.05.04-090

## Highlights

- Cloud Drive audio previews now normalize blob MIME from preview metadata, so
  music files can still inline-preview even when the browser first receives a
  generic blob type.
- Publishing a video from an existing Cloud Drive media file now supports an
  uploaded custom cover image instead of silently ignoring the chosen cover.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_video_publish.py tests/test_cloud_drive_attachments.py -k 'audio_preview_content_supports_streamable_music or accepts_cover_upload_for_existing_cloud_media or video_upload_endpoint_accepts_audio_and_streams_it or video_upload_endpoint_stores_server_encrypted_video_and_streams_plaintext'`
- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_drive_preview.py tests/test_frontend_videos.py tests/test_release_policy.py`
- `git diff --check`

## 2026.05.04-089

## Highlights

- Large Cloud Drive uploads are no longer hard-blocked at `50 MB` before they
  reach the real per-user quota and max-file policy checks.
- The Flask request-body cap is now controlled by
  `HTML_LEARNING_MAX_CONTENT_MB` with a default of `1024 MB`, and API callers
  now get a structured `413 request_too_large` JSON payload instead of a bare
  status code.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_security_defaults.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-088

## Highlights

- Uploaded chat, DM, and announcement attachments now land in the Cloud Drive
  `/attachments/` folder instead of cluttering the drive root.
- The stored display name remains the original filename while the underlying
  storage path gets a unique attachment-prefixed name to avoid path collisions.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_drive_preview.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-087

## Highlights

- Chat attachment UX is now inline with the message composer instead of hiding
  upload and existing-file actions inside a separate `聊天室附件` card.
- Picking a file now immediately adds it to the pending send list, while room
  scoped `聊天室共用附件` only appears when the current room actually has shared
  attachments.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_chat.py tests/test_frontend_drive_preview.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-086

## Highlights

- ComfyUI LoRA / Embedding interaction is now reversible in the AI page. Removing
  a selected LoRA removes its no-longer-needed trigger words, choosing `不使用
  LoRA` and pressing `加入` clears the current LoRA list, and clicking an already
  inserted Embedding removes it again.
- Embeddings whose filename contains `neg` or `negative` now default to the
  negative prompt instead of the positive prompt.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-085

## Highlights

- ComfyUI now blocks unsupported LoRA base-model families before generation.
  Only `SDXL`, `Pony`, `Illustrious`, and `Noob` LoRAs remain selectable in the
  AI page. `SD1.5`, `Flux`, and unknown-metadata LoRAs are shown as unavailable
  and the backend rejects crafted requests that try to bypass the UI.
- Root-downloaded Civitai LoRA sidecars now persist `base_model` metadata so
  later page loads can enforce the same compatibility rule consistently.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-084

## Highlights

- ComfyUI generation now uses a 30-minute default wait budget end-to-end
  instead of timing out earlier on the frontend progress poll or the backend
  generation route. Long model loads or retried queue waits no longer fail just
  because the default cap was too short.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-083

## Highlights

- The notification center no longer shows two different `read all` actions for
  the same API call. The panel keeps the single header-level `全部已讀`
  button and removes the duplicate in-list `一鍵全部已讀` action.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_notifications.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-082

## Highlights

- Margin-buy collateral validation copy is now humanized. Instead of only
  showing a mechanical `最高 N 點`, the UI now distinguishes:
  - collateral below the minimum requirement
  - a valid financing range
  - collateral that already exceeds the full notional and therefore should use
    normal spot buying instead of margin
- The warning now explicitly explains that margin buy must still borrow at
  least `1` point, so users understand why `保證金 >= 名目金額` no longer counts
  as financing.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-081

## Highlights

- Spot wallet detail rows now separate `持有成本` from `損益平均價格`.
  `持有成本` shows the acquisition cost including the estimated buy-side fee,
  plus a per-unit cost view. `損益平均價格` shows the fee-aware break-even
  exit price after also accounting for the estimated sell-side fee.
- The unrealized PnL copy in spot wallet rows now explicitly says it already
  includes the estimated sell-side fee, so users no longer have to guess why
  the break-even price is above the displayed average cost.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-080

## Highlights

- The lightweight `GET /api/trading/live-price` poll still runs every two
  seconds, but now it also refreshes the Points wallet trading PnL cards on
  the same cadence instead of waiting for the slower full dashboard reload.
- Spot position value / unrealized PnL, root virtual total, and margin risk /
  equity / unrealized PnL now recompute from the latest in-memory live market
  price, so wallet-side trading numbers no longer stay stale while the current
  price card keeps moving.
- Live-price polling now runs on both the `trading` page and the `economy`
  wallet page. It updates only the active wallet markets plus the currently
  selected trading market, keeping the refresh lightweight without forcing a
  full dashboard fetch every two seconds.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `git diff --check`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-079

## Highlights

- Grid Bot creation now uses a backend-owned fee preview instead of a
  frontend-only spread guess. `POST /api/trading/grid/preview` calculates the
  worst-case grid spacing, break-even spread, per-grid gross profit, fee, and
  net profit with `Decimal`, then returns a red / yellow / green risk light.
- Grid preview red-lights now block creation, while thin-profit yellow-lights
  require an extra confirmation. This prevents the old UI failure mode where a
  strategy looked profitable because it only showed raw spread and ignored
  fees.
- The trading page keeps the existing capital / inventory estimate, but now
  shows fee-aware copy such as `最不利一格毛利`, `最不利一格手續費`,
  `最不利一格扣費後淨利`, and `損益兩平間距`.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_grid_fee_model.py tests/test_grid_preview_api.py tests/test_grid_fee_ui.py`
- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py -k 'grid' tests/test_frontend_economy.py tests/test_release_policy.py`
- `git diff --check`

## 2026.05.04-078

## Highlights

- The default Cloud Drive purchase plan is now `1GB / 7 days` instead of
  `1GB / 30 days`.
- Existing databases are normalized on startup so the legacy
  `cloud_storage_1gb_7d` catalog row keeps the same key but gets the new
  `item_name`, `duration_days`, and label, avoiding mixed `30 天 / 7 天`
  displays between fresh and old runtimes.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_upload_security.py tests/test_cloud_drive_attachments.py tests/test_release_policy.py`
- `git diff --check`

## 2026.05.04-077

## Highlights

- The trading page `live-price` polling cadence is now `2` seconds instead of
  `1`, reducing exchange API load while keeping the current-price card visibly
  alive.
- Buy/sell order estimates stay in lockstep with that same `2`-second
  live-price refresh, so the quoted notional/fee preview no longer lags behind
  the displayed market price.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `git diff --check`

## 2026.05.04-076

## Highlights

- The feature-flag settings page now ships with two root-friendly global
  presets:
  - `全開`: replace the whole feature matrix with every module enabled
  - `最低維運`: replace the whole feature matrix with the minimum operational
    baseline (`accounts`, `audit`, `system health`, `server modes`,
    `snapshot / restore`)
- Existing domain bundles such as account governance, community, drive, AI, and
  trading stay additive; they still only turn on the related module family
  instead of wiping the rest of the matrix.
- The feature-page helper text, deployer docs, admin guide, QA checklist, and
  troubleshooting notes now explain the difference between additive bundles and
  full-matrix presets so root operators do not accidentally think `最低維運`
  is a small tweak.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_drive_preview.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-075

## Highlights

- Trading fee and borrowing controls are now aligned to the new defaults:
  - spot fee `0.10%`
  - grid fee = spot fee with `25%` discount
  - `BTC / ETH = 8% APR`
  - `USDT / POINTS = 10% APR`
  - hourly billing with `minimum 1 hour`
- Root trading settings can now adjust those rates directly from the dedicated
  `交易所` page instead of relying on the older daily-interest mental model.
- Borrow positions now expose `累積利息`, `已實扣`, and `下一次計息` metadata in the
  trading UI, so users can see both accrued interest and the next billing time.
- The backend now accumulates per-user trading volume / fee statistics for
  future VIP logic, and root reports expose aggregate `volume_summary`.
- Grid deterministic QA baselines were re-synced after the new fee defaults, so
  the engine, pytest suite, and `security/trading_exchange_validation.py` all
  agree on the updated result.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py tests/test_frontend_economy.py tests/test_trading_reference_prices.py tests/test_release_policy.py`
- `PYTHONPATH=. python3 security/trading_exchange_validation.py --out /tmp/trading_exchange_validation_fee_apr_followup`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-074

## Highlights

- Root trading settings are now split out of the overloaded `計費` page into a
  dedicated `交易所` settings tab.
- The trading settings UI is reorganized into focused groups:
  - basic trading / borrowing / liquidation controls
  - price source and fusion diagnostics
  - bot auto-scan and audit dashboard
  - BTC_trade integration
  - per-market overrides
- Existing field ids and backend payload formats stay intact, so the change is
  a UI / IA cleanup rather than a breaking settings-schema migration.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-073

## Highlights

- The trading page `目前價格` card now refreshes once per second through a
  lightweight `GET /api/trading/live-price` route instead of waiting for the
  heavier 5-second dashboard refresh.
- Price direction is now visualized directly in the card: up ticks turn green,
  down ticks turn red, and degraded fallback / cached sources show a yellow
  warning badge.
- The `live-price` response now returns `price_health`, `fallback_reason`,
  `excluded_sources`, and `defaulted_market`, so the frontend can explain why a
  price is degraded instead of silently treating it as healthy.
- Cached fallback prices no longer truncate fractional values via `int(...)`;
  the fallback path now preserves decimal precision so `0.12345678` does not
  become `0`, and `123.99` does not become `123`.
- `GET /api/trading/live-price` is documented as a safe-read route that still
  refreshes the cached `trading_markets.manual_price_points / price_source`
  fields in SQLite for downstream order-entry and dashboard consistency.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py tests/test_trading_reference_prices.py tests/test_frontend_economy.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.04-072

## Highlights

- Root trading settings now expose a `BTC_trade 一鍵啟動預測` button after the
  repo path is configured. The start flow first checks whether the BTC_trade
  data file is stale and whether model artifacts are older than the latest
  data, then only reruns `update_data.py` / `retrain_models.py` when needed,
  and finally launches `hourly_check.py`.
- Long BTC_trade model training no longer gets treated as an immediate timeout
  failure. The start flow now runs as a background job, and the root panel
  polls job status until it either sees a fresh `runtime/report_log_4h.jsonl`
  or explicitly reports that the latest prediction is still within the valid
  freshness window.
- The root `檢查 BTC_trade` status text now includes a compact summary of
  data/model/prediction freshness instead of only saying whether the report is
  available.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_reference_prices.py -k 'btc_trade or start_status or start_returns_background_job or artifact_freshness'`
- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py -k 'btc_trade or reference_polling'`
- `python3 -m py_compile services/btc_trade_bridge.py routes/trading.py`

## 2026.05.04-071

## Highlights

- The trading page no longer lets single-source reference-price polling
  overwrite the fused/live execution reference price shown in the order card.
  Reference candles now stay in the chart lane only, while the visible
  `目前價格` and order estimate keep using the real market price returned by the
  trading dashboard.
- This closes the UI mismatch where users could see the displayed trading price
  jump between very different values even though the actual execution
  reference price had not changed the same way.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py`
- `git diff --check`

## 2026.05.04-070

## Highlights

- Trading backtests no longer silently replace a user-supplied short candle set
  with live public-market history. The route now requires an explicit
  `auto_fetch_reference_candles=true` opt-in before it downloads reference
  candles, so isolated QA and hand-built scenarios stay isolated by default.
- The backtest engine now guards obviously abnormal jump candles and flat
  Bollinger ranges. Extreme outlier candles are skipped with explicit warnings
  instead of booking fake profits, and `std=0` flat sequences no longer
  trigger `below_lower` / `above_upper` Bollinger conditions.
- Root now has a dedicated trading-bot audit dashboard. Bots remain `未稽核`
  until they either produce at least one trade or stay enabled for 24 hours;
  after that the scheduler records green/yellow/red audit runs, surfaces recent
  findings, and lists trading bug reports in the same root-only panel.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py -k "audit or backtest or bollinger or outlier or 20000"`
- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_reference_prices.py tests/test_frontend_economy.py tests/test_bug_reports.py tests/test_release_policy.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-069

## Highlights

- Trading backtest date pickers no longer expect users to understand the
  `20,000`-candle cap. When a user picks a start or end datetime, the UI now
  immediately explains how far the other side can be extended at the current
  timeframe and clamps the input range accordingly.

## 2026.05.04-068

## Highlights

- Root trading settings now include a root-only live fusion dashboard. It can
  show the currently effective provider ratios, excluded exchanges, degraded
  states, and whether the fused price has fallen back into conservative
  single-source mode.
- Fused-price diagnostics are now explicit instead of silent. Failed exchange
  order books are exposed as excluded providers, `manual_weights` with all
  zeros is flagged as invalid and shown as an `auto_depth` fallback, and
  order-book total failure is surfaced as `價格來源降級` instead of pretending
  it is still a normal fused price.
- Price-fusion QA now covers default mode, auto-depth weighting, provider
  exclusion, manual-weight equal weighting, all-zero manual fallback, and the
  single-source ticker fallback chain.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py -k "price_fusion or live_price_fusion or root_trading_settings_default_to_fused_weighted_auto_depth"`
- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_reference_prices.py -k "price_fusion or fused_price"`
- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_economy.py -k "root_trading or trading_exchange_is_separate_from_wallet_page"`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-067

## Highlights

- Trading backtests no longer force users to manually split any range above a
  single execution batch. The backend now accepts up to `20,000` candles per
  run and internally continues long windows in contiguous `10,000`-candle
  segments, so large windows keep one result set while still staying inside a
  bounded resource cap.
- The browser no longer tries to carry segmented backtest state itself. It now
  sends one request, lets the backend preserve DCA intervals, workflow state,
  and grid state across internal batches, and clearly tells the user when a
  run was segmented automatically.
- Backtest download metadata now reports both the overall candle cap and the
  per-batch execution cap, so deployers and QA can tell whether a run was
  blocked by the total limit or simply split into multiple backend chunks.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py tests/test_trading_reference_prices.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.04-066

## Highlights

- Margin interest now keeps fractional carry in micropoints instead of rounding
  every small accrual straight up to the next full POINT. Small-principal
  positions now accumulate residual interest until it crosses a whole point,
  which removes the old `50 @ 1% / day -> 1 point after 1 day` overcharge.
- Historical backtests now allow up to `10,000` candles end-to-end, so
  full-year `BTC/USDT 1h` windows like `2024-01-01 ~ 2024-12-31` are no longer
  blocked by the old `5000`-candle ceiling.
- The funding-pool pressure multiplier now respects an explicit root value of
  `0` instead of silently falling back to the default multiplier.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_release_policy.py tests/test_smoke_suite_regressions.py tests/test_pentest_script.py tests/test_functional_permission_pentest.py tests/test_trading_engine.py tests/test_trading_reference_prices.py tests/test_frontend_economy.py`
- `python3 security/trading_exchange_validation.py --out /tmp/trading_exchange_validation_issue_followup`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-065

## Highlights

- Trading now defaults to a fused live price instead of a single fixed public
  ticker source. Root can keep automatic depth-based weights or switch to
  manual per-exchange weights across Binance, OKX, Coinbase, Kraken, Gemini,
  and Bitstamp; if one API fails, the remaining healthy exchanges are
  re-normalized automatically.
- DCA bots now accept `max_runs = -1` as an unlimited schedule. The backend
  stores this as a sentinel, the frontend renders it as `不限制`, and the
  `增加次數` flow now no-ops cleanly for unlimited bots.
- The deterministic trading validation script was resynced with the current
  grid engine result (`1072` instead of the stale `1065`), and the trading QA
  report set gained a follow-up note that records the code changes and retest
  results.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_trading_engine.py tests/test_trading_reference_prices.py tests/test_frontend_economy.py`
- `python3 security/trading_exchange_validation.py --out /tmp/trading_exchange_validation_followup`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-064

## Highlights

- The personal appearance override reset action was moved to the main user-edit
  footer so ordinary users can find it without hunting inside the collapsed
  appearance controls.
- The reset copy now explains that it returns the account to root's global
  default appearance and still requires the final `儲存` action before writing
  to the profile.
- Appearance docs and QA guidance were updated so deployers know where the
  reset action lives and how to verify that the override is actually cleared.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_personalization.py tests/test_frontend_drive_preview.py tests/test_user_profile_appearance.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-063

## Highlights

- QA tooling defaults were aligned. `tests/smoke_suite.py` now uses the same
  smoke credentials as `run_functional_smoke.sh` and
  `functional_permission_pentest`, so the default runbook no longer breaks on
  mismatched rotated passwords.
- The Python smoke suite now snapshots and restores feature flags after it
  temporarily enables chat/community/games-related modules. This prevents the
  suite from leaving `feature_economy_enabled` or sibling flags in a mutated
  state for later checks in the same runtime.
- `security/run_pentest.sh` now gives `whole-site-production-gate` a higher
  timeout floor automatically, so the wrapper's generic `180s` limit no longer
  kills that gate before the underlying Python checker finishes.
- Trading fee calculation for integer POINT ledgers now uses `Decimal` plus
  round-half-up instead of always `ceil`-biasing small orders upward. This
  removes the strongest systematic overcharge behavior on small spot trades.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_smoke_suite_regressions.py tests/test_pentest_script.py tests/test_trading_engine.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-062

## Highlights

- Root settings and session UX were tightened after QA. The shared settings
  success banner now auto-clears instead of lingering indefinitely, and idle
  logout warnings no longer reuse the same banner area.
- Feature-gated APIs no longer stop at a generic `此功能目前已由 root 關閉`.
  The response payload now names the blocked feature, missing parent features,
  and already-enabled dependent modules that will be affected together.
- Local/remote ComfyUI, user appearance, storage/albums, and related frontend
  navigation fixes were grouped into a dedicated code split, while regression
  coverage and legacy wrapper cleanup were split into separate commits.

## Validation

- `python3 scripts/pre_push_checks.py --ci`
- `git diff --check`

## 2026.05.03-061

## Highlights

- The root settings success banner no longer lingers indefinitely. A normal
  green `設定已儲存` message now auto-clears after a short delay, while
  incomplete feature-dependency warnings stay visible as a separate warning
  state.
- The idle logout countdown warning no longer reuses the root settings status
  area, so operators do not see unrelated countdown notices overwriting or
  mixing with settings-save feedback.
- Feature-gated `503` responses are no longer generic-only. The payload now
  includes the blocked feature label plus missing parent features or currently
  enabled dependent modules that will also be affected, so root can tell what
  to open together instead of only seeing `此功能目前已由 root 關閉`.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_auth_timeout.py tests/test_feature_flags.py tests/test_functional_permission_pentest.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-060

## Highlights

- Root settings now hide token fields when the matching mode is not active.
  `Turnstile site key` only appears when registration CAPTCHA is set to
  `turnstile`, instead of staying visible in `none / math / image` mode.
- The ComfyUI settings copy now states more explicitly that remote API mode is
  generation-only. In remote mode, the root-only local model-download path and
  `Civitai API Key` stay hidden because the app cannot download models into a
  remote ComfyUI host through the normal API.
- Admin, feature-overview, troubleshooting, and QA docs were updated so a new
  deployer can tell whether a missing token field is expected mode behavior or
  an actual UI bug.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_captcha.py tests/test_comfyui_integration.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-059

## Highlights

- ComfyUI long-running tasks now suspend the frontend idle auto-logout
  countdown more consistently. This no longer applies only to generation:
  local startup polling and root's local Civitai model downloads now also keep
  the session alive while the task is still running.
- Static regression tests were expanded so future frontend changes must keep
  the `ComfyUI 產圖中` / `ComfyUI 啟動中` / `ComfyUI 模型下載中` idle-suspend hooks.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_auth_timeout.py tests/test_comfyui_integration.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-058

## Highlights

- Site appearance control is now split cleanly between global defaults and
  personal overrides. Root still owns the global theme, while logged-in users
  can save a personal theme from `修改資料 -> 個人外觀`.
- The appearance editor now exposes more than just colors: users and root can
  adjust font family, background style, panel style, sidebar width, layout,
  density, radius, font scale, and content width.
- The old `feature_personalization_enabled` switch is now effectively the
  "allow personal appearance overrides" control. It defaults to on, lives next
  to root's appearance settings, and shows a clear disabled message to users if
  root turns it off.
- Root's settings page now clears the stale `設定已儲存` banner as soon as
  another field is edited, so operators no longer keep seeing an outdated
  success state while making new unsaved changes.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_frontend_personalization.py tests/test_user_profile_appearance.py tests/test_frontend_chat.py tests/test_frontend_economy.py tests/test_frontend_governance.py tests/test_frontend_drive_preview.py tests/test_mobile_responsive_layout.py tests/test_comfyui_integration.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-057

## Highlights

- The ComfyUI page now shows the active connection mode more explicitly. The
  panel header includes a visible mode badge plus a short explanatory line, so
  users can tell at a glance whether they are in local mode or cloud/remote API
  mode.
- The mode explanation now clarifies what each mode means operationally:
  local mode allows root-controlled local start/stop and local model download,
  while cloud/remote mode is generation-only and does not expose local model
  management.
- Troubleshooting and feature-overview docs were updated so operators know to
  use the visible mode badge as the first check when ComfyUI behavior looks
  different from expectations.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py`

## 2026.05.03-056

## Highlights

- LoRA trigger words are now persisted after root downloads a LoRA through the
  local-mode Civitai panel. The server writes a small sidecar metadata file next
  to the downloaded LoRA so the trigger-word mapping survives page refreshes and
  later sessions.
- `/api/comfyui/models` now returns `lora_details` alongside the plain LoRA
  name list. The frontend uses that metadata to auto-append any missing trigger
  words into the positive prompt when a user adds a known LoRA.
- The auto-insert behavior is intentionally conservative: it only applies to
  LoRAs with known saved metadata and only appends missing terms, so repeated
  add/remove actions do not keep duplicating the same trigger words.
- Admin/operator, feature-overview, troubleshooting, and QA docs were updated
  so this behavior and its limits are documented from both deployer and root
  perspectives.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py`

## 2026.05.03-055

## Highlights

- The ComfyUI generation form now supports both `Embedding` and `VAE`
  parameters instead of only listing them conceptually. Users can click an
  embedding shortcut button to insert it into the positive prompt, and can
  switch between the checkpoint builtin VAE or an installed standalone VAE.
- The backend translates the UI's `<embeddings:name>` helper token into actual
  ComfyUI embedding prompt syntax before queueing the workflow, and custom VAE
  selection now inserts a real `VAELoader` node into the generated workflow.
- Root's local-mode Civitai download panel no longer offers outdated
  `Hypernetwork` or currently unsupported `ControlNet` downloads. The panel now
  focuses on the types this UI can actually use: checkpoint, LoRA, embedding,
  and VAE.
- Civitai inspect/download responses now surface official `trainedWords`, so
  root can see a model version's trigger words before downloading a LoRA and in
  the post-download result message.
- Documentation and QA guidance were updated so deployers and root operators
  can see the new ComfyUI limits and validation points without digging through
  source first.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py`

## 2026.05.03-054

## Highlights

- Removed the unfinished ComfyUI acceleration / credits UI that was added under
  the wrong assumption that Comfy Cloud paid credits could be surfaced and used
  through the existing page flow.
- The AI generation panel is back to the usable core: main ComfyUI connection
  mode, local start/stop controls, async progress, billing confirmation, and
  result save/share/discard behavior.
- Root's Civitai model download tools remain available, but they now live in a
  separate collapsed panel at the bottom of the AI page so generation and model
  management are clearly separated.
- The AI page now states the active mode explicitly (`local` vs `remote`) and
  hides Civitai/model-download controls when the root setting is in remote
  mode.
- Each selected LoRA now exposes separate `model` and `clip` strength controls,
  and the frontend pauses idle auto-logout while generation is running.
- Personal appearance settings are no longer root-only: authenticated users can
  keep their own appearance override while root still owns the global default
  theme.
- Server settings now keep only the active ComfyUI connection settings plus the
  Civitai API key; the temporary root acceleration URL field is removed.
- Documentation was reorganized from a deployer-first perspective: README is
  now brief, new numbered entry guides were added under `docs/`, and the older
  large guides were downgraded to deep-reference status instead of being
  deleted.
- Regression coverage was updated for the new frontend layout/scripts and the
  simplified ComfyUI settings surface.

## Validation

- `PYTHONPATH=. python3 -m pytest -q tests/test_comfyui_integration.py tests/test_frontend_chat.py tests/test_frontend_economy.py tests/test_frontend_governance.py`
- `python3 scripts/pre_push_checks.py --ci`

## 2026.05.03-051

## Highlights

- ComfyUI generation now supports asynchronous job progress. The web page polls
  `/api/comfyui/jobs/<job_id>`, shows queue/node progress, and keeps the user on
  the AI page while local-mode startup is still in progress.
- Root can inspect a Civitai model page URL, choose a version/file, and
  download checkpoints or LoRA files into the configured local ComfyUI project.
  Root can also stop the shared local ComfyUI process from the page.
- Account governance is stricter and more auditable: rejecting a pending
  registration deletes the application account, normal user deletion becomes
  soft-delete with history preservation, deleted users are hidden from default
  admin lists, and member-rights changes create governance notices plus appeal
  restore context when appropriate.
- Trading now exposes first-class grid bot routes/UI plus bot max-run
  extension. Grid bots support底倉 checks before creation, manual scans that
  place counter-orders after fills, and backtest selection alongside DCA and
  workflow bots.
- Server-encrypted Cloud Drive / Video media no longer fails with a generic 500
  after a server file-key rotation. Previews return an explanatory placeholder
  where possible, and raw content/stream APIs return `decrypt_unavailable`.
- The blocking pre-push gate is modularized under `scripts/prepush/`, adds
  cleanup helpers, and now has dedicated regression coverage for release-sync,
  governance/account, and trading updates.

## Validation

- `python3 scripts/pre_push_checks.py --ci`
- `PYTHONPATH=. python3 -m pytest -q tests/test_prepush_v2.py tests/test_frontend_account_admin.py tests/test_comfyui_integration.py tests/test_account_sessions.py tests/test_sanction_notices.py tests/test_trading_engine.py tests/test_video_publish.py tests/test_security_issue_regressions.py`

## 2026.05.02-050

## Highlights

- ComfyUI root settings now support local and remote connection modes. Local
  mode keeps startup explicit: users press the AI-page start button before
  generation, and already-running local ComfyUI instances can be reused by
  other users.
- Added `scripts/comfyui/comfyui_run_in_linux.template.sh` as a reusable Linux
  startup template. It checks for an existing virtual environment, creates one
  only when needed, installs dependencies idempotently, and avoids embedding
  workstation-specific paths.
- ComfyUI generation ownership is tracked per user. Save, discard, share, and
  interrupt actions only operate on that user's generated image references;
  user interrupts avoid stopping other users' active backend jobs.
- Cloud Drive and album behavior is improved for E2EE session preview,
  document creation, media previews, queued remote downloads, and generated
  ComfyUI output albums.
- Video Platform publishing now works through the existing Cloud Drive storage
  layer for direct uploads and server-encrypted media without exposing storage
  paths.
- Documentation was cleaned to describe ComfyUI local/remote operation and to
  remove local machine path examples.

## Highlights

- Whole-site production gate is now available through
  `security/whole_site_production_gate.py` and
  `security/run_pentest.sh --only whole-site-production-gate`.
- Latest local gate evidence before the Video Platform module passed against
  `http://127.0.0.1:5000`:
  12/12 modules PASS, `critical_findings=0`, `high_findings=0`,
  `medium_findings=0`, `production_readiness=YES`.
- Latest evidence files:
  `security/reports/20260502T150309Z/raw/whole_site_production_gate_20260502_230524.json`
  and
  `security/reports/20260502T150309Z/raw/whole_site_production_gate_20260502_230524.md`.
- The gate aggregates Server Mode v2, auth/session, RBAC, snapshot/restore,
  PointsChain/economy, Cloud Drive, Video Platform, trading, forum/community/reporting,
  integrity, audit/logs, stress/reliability, pytest, `py_compile`, generated
  report policy, and `git diff --check`.
- Latest-password lookup now uses the monotonic `user_passwords.id` order
  instead of textual `created_at` ordering, avoiding stale-password selection
  when timestamp formats differ.
- Feature-disabled API gates now return unauthenticated requests as `401`
  before reporting feature-disabled `503`, so permission tests see the real
  authorization boundary.
- `functional_permission_pentest.py` now accepts `PENTEST_USER_PASSWORD` in
  addition to the legacy `PENTEST_TEST_PASSWORD`.
- `trading_stress_pentest.py` no longer rotates root's password by default;
  production-gate targets must use already-initialized test credentials or pass
  `--root-new-password` explicitly.

## Operator Notes

- Keep the whole-site gate evidence together with the Server Mode v2
  adversarial, Red Team L2, and live HTTP reports. The whole-site gate is the
  aggregate production decision; the Server Mode v2 reports remain its
  control-plane evidence.
- Server Mode v2 production_ready is narrower than whole-site
  production_ready. The whole-site gate must be run before production sign-off.
- Off-host append-only log replication / filesystem-level immutable storage is
  still a deployment-environment control; the local gate records it as an
  unresolved deployment risk unless verified separately.
- Runtime logs, generated reports, SQLite databases, pycache, and local keys are
  generated artifacts. They should remain ignored and must not be committed.
