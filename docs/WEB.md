# WEB

`WEB.md` describes the user-facing web application. API details and deployment
defaults live in [For_developer.md](For_developer.md).

If you are onboarding a deployer or an end user, start with
[04_USER_GUIDE.md](04_USER_GUIDE.md) and
[05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md) first. This file is the
page-by-page deep reference.

Before any production release, use
[security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md). The
checklist treats completed pentesting and completed full functional smoke
testing as blocking release requirements.

## UI Shell

After login, the app uses a full-viewport sidebar layout.

- The sidebar is flush with the browser's left edge.
- The middle menu area scrolls independently when there are too many modules.
- The footer stays fixed at the bottom and shows account information, points,
  violation deductions, effective permission level, server connection status,
  and release ID.
- The sidebar collapse state is saved in `localStorage`.
- Top-right compact icon buttons handle profile editing, notifications, bug
  reports, logout, and the idle logout countdown.
- Root/server management pages pause their live server-output, request-capacity,
  and resource-board polling when the browser tab is hidden, then resume only
  the active management page when the operator returns. Secondary health-page
  charts are loaded during browser idle time so opening the health center does
  not burst all admin reads at once.
- Mobile layouts keep admin, health, system resource, and wide table sections
  inside horizontal scroll or single-column panels so root operators can inspect
  server state from a phone without page-wide overflow.

## Main Pages

### Chat

Users can create or join chat rooms, send messages, refresh room state, attach
cloud-drive files, and report inappropriate messages. Chat actions are still
checked against member-level permissions.

The create-room controls are intentionally button-opened so the full form does
not permanently occupy the chat page. Official rooms anonymize regular users
for regular viewers and hide member counts from non-root/non-manager accounts;
root appears as `root`, managers appear as numbered official managers, and
root/manager views can still see the original sender for moderation. Normal
group rooms can optionally allow anonymous participation; joining users then
choose whether to speak anonymously. PM rooms do not support anonymity.

Chat polling is bounded: normal refreshes request only messages after the last
seen id and periodically fall back to a full refresh so edits/recalls still
settle. The room list aggregates member counts in one query instead of counting
per room, and delta message polls skip room member-count metadata until the next
full refresh.

### Direct Messages

The DM page provides one-to-one station-mail style messaging, unread tracking,
message soft-delete, and user blocking.

### Personal Profiles and Friends

The main sidebar includes a personal profile panel for public profile data,
friend requests, and friend-code joins. The remaining social-layer requirement
is to make every visible user identity link to a personal profile. Entry points
include comment owners, forum authors, video owners, leaderboards, game records,
chat rows, and notifications.

Profiles should show public user information, friendship status, and the next
available action: send a friend request, accept/reject an incoming request, or
open friend-only actions when the relationship is already accepted.

The current chat friend table and APIs are the compatibility base. The first
profile/friends phase now uses the same `user_friends` relationship store from
the main sidebar "Profile" panel instead of creating a second relationship
store. Friend-only interactions must be checked server-side:

- non-friends cannot send PMs or create private group chats
- game invites and direct strict-E2EE file-key sharing still need the same
  backend friend-gate before they are considered complete
- friend-code joins create an accepted relationship without a second approval
- root and manager can view all profiles and may PM non-friends for management
  purposes, but they still appear in friend lists and must be sorted to the top
  with a clear official/admin marker

The formal requirement is tracked in
[USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md).

### Announcements

Announcements are separated from the forum page. Authorized users can publish
announcements, pin important items, and submit attachment requests that require
root review. Announcement list reads return an ETag; the web client revalidates
with `If-None-Match` and can reuse the current list on `304 Not Modified`.

### Forum

The forum is board-first:

- users enter a board list first
- then open a board to see thread lists
- then open a thread to read posts and replies

The default community model includes board categories, board requests, board
review, thread review, reactions, pinned/locked threads, and moderation tools
for board maintainers.

Forum list endpoints are bounded and count-heavy views use aggregated queries.
Thread detail replies default to a capped page and return `posts_total`,
`posts_limit`, and `posts_has_more` so large threads do not hydrate every reply
in one request. The web client loads additional reply pages on demand.

### Cloud Drive

Cloud Drive supports:

- local uploads with visible progress
- resumable/chunk uploads with task-center recovery after reload; users must
  reselect the same local file because browser file handles are not persisted
- privacy-mode selection with human-readable labels
- a separate capacity-management subpage with water-level usage display, quota,
  scan-policy status, and capacity upgrade controls
- image, media, PDF, text, and archive previews when policy allows
- text-file editing for safe owner-owned text files
- file delete and download actions
- logical folders, move/organize flow, trash, restore, and purge
- album creation and album viewing
- share links for storage files, including browser preview when enabled
- remote direct-link / BT downloads with speed, progress, availability hints,
  and pause/resume/cancel controls in the task center

Privacy / encryption modes are deliberately explicit because not every mode is
end-to-end encrypted:

| Mode | Best for | Server can read plaintext | Scan / preview | Main risk |
|---|---|---:|---|---|
| `standard_plain` | Normal files, attachments, album display, shared files | Yes | Best scan/preview/share compatibility | Stored as plaintext on the server filesystem |
| `server_encrypted` | Reducing disk/backup exposure while keeping server features | Yes, by temporary server-side decrypt | Server decrypts for scan, preview, and plaintext download | Not E2EE; server/root can decrypt |
| `e2ee` | Highly private storage | No | Browser-side decrypt preview after the user enters the E2EE password; server can only inspect ciphertext/metadata | Losing the user-chosen E2EE file password makes the file unrecoverable |

Use `standard_plain` when users need normal cloud-drive behavior. Use
`server_encrypted` when protecting at-rest storage is useful but server-side
scan/preview/download support is still required. Use E2EE when confidentiality
is more important than server-side scanning and recovery support. E2EE files can
still be previewed by the browser after the user enters the file password.
For the runtime trust boundary between `server_encrypted` and strict `e2ee`,
see [ENCRYPTION_RUNTIME_BOUNDARY.md](ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md).

E2EE uses a user-entered file encryption password in the browser. The browser
derives a wrapping key with PBKDF2-SHA256 and uses it to encrypt the per-file
key before upload. The server stores only ciphertext, encrypted metadata, salt,
nonce, and the wrapped file key; it does not receive the plaintext password or a
decryptable master key. A user can decrypt on another computer by entering the
same E2EE password, but the server cannot reset or recover forgotten E2EE
passwords. During preview, the password is cached only in browser memory for
the current logged-in page session and is cleared on logout or browser close.

Remote downloads are integrated into Cloud Drive:

- HTTP/HTTPS direct links
- magnet links
- uploaded `.torrent` files

BT/magnet/`.torrent` downloads can use Transmission RPC or aria2c on the server;
production should prefer Transmission RPC with aria2c fallback. See
[13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md) for deployer settings.
Downloaded files are saved through the same quota, scan, privacy-mode, and logical-folder
pipeline as normal uploads. BT transfers run in an external worker process by
default; the Flask server only tracks progress and stores the completed file.
The task center lazy-loads and polls real progress only while the user is on
the task-center page. It exposes current speed and control actions without
running a global always-on frontend poll. When more than one remote task is
queued, the scheduler can prefer higher-availability BT work instead of
starting every low-quality task at once.
`timeout_seconds` is treated as an idle timeout for BT, so active downloads are
not stopped just because they run longer than the initial timeout window.

Share Management lives under the Management area and is the canonical editor
for file, album, and video share links. Copy-link actions should show a visible
"copied" confirmation below the button. For strict E2EE shares, users must copy
the complete URL including the `#` fragment key; the server cannot recover a
missing fragment.

Root can configure per-member-level Cloud Drive transfer controls under
`伺服器設定 -> 雲端硬碟 -> 階級傳輸限速`:

- upload speed in KB/s
- download speed in KB/s
- priority from 0 to 100

`0` disables that transfer direction for the level. Root is not throttled.
Download throttling is applied while streaming files from Flask unless the file
is eligible for X-Accel/sendfile offload. Upload limits still reject disabled
tiers, but app-side sleep shaping is disabled by default because the HTTP
request body has already reached Flask by the time route code runs. For strict
network-layer upload QoS, deploy an nginx or reverse-proxy limit in front of
the app; use `HACKME_CLOUD_DRIVE_UPLOAD_SLEEP_SHAPER=1` only for legacy shaping
tests.

### Albums

Albums have their own page for gallery-style browsing. Albums are backed by the
storage file manager and do not duplicate physical cloud-drive files. Album
photo grids should behave like a continuous photo stream: no permanent preview
/ download buttons on every photo, slight hover enlargement, full-page preview
on click, and left/right navigation between neighboring photos.

Use the album `分享` button to generate a `不列出，持連結可看` album share
and jump into Share Management. Album detail and preview panels do not show the
raw share URL or password controls. Share Management is the only front-end
place to copy the album URL, set or clear the optional password, configure an
expiry time, set a maximum access count, revoke the link, or inspect access
events. Album share passwords are stored as hashes and must be shared out of
band with the recipient.

### Video Platform

The `影音` page publishes videos already stored in Cloud Drive. It does not
upload to a separate filesystem.

The current page behavior described here is Video Platform v1 with HLS and
strict E2EE streaming extensions. The formal HLS / segmented streaming design
for large media is documented in
[VIDEO_STREAMING_ARCHITECTURE.md](video/VIDEO_STREAMING_ARCHITECTURE.md).

- owner selects one of their own Cloud Drive video files
- publish controls open after the user presses the publish action; they should
  not be reintroduced as a permanently visible card
- visibility can be public, unlisted, or private
- playback uses `/api/videos/<id>/stream`, not a raw storage path
- prepared HLS videos appear only after the derivative is ready; while
  processing, the uploader gets a processing notice and completion notification
- the video list supports search
- "My videos" share actions should hand off to Share Management so share
  options can be edited in one place
- users can like, comment, and tip
- tips are recorded through PointsChain

E2EE files are intentionally not publishable as normal server-streamed / HLS
videos because the server cannot safely preview or stream plaintext without
receiving decryptable material. They can, however, be published as `持連結可看`
shared videos: the owner enters the original E2EE password once at publish
time, the browser unwraps the file key locally, and the browser re-wraps that
file key into a share envelope that still keeps the server blind to the raw key
and original password. For normal web-player HLS, use `standard_plain` or
`server_encrypted`.

If a `server_encrypted` file was written with an older server file key that is
no longer available, the video page now fails safely: the cover endpoint shows
an explanatory placeholder and stream/content endpoints return a structured
`decrypt_unavailable` error instead of a generic server error.
The strict E2EE publish/share boundary is also documented in
[ENCRYPTION_RUNTIME_BOUNDARY.md](ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md).

### ComfyUI

The AI image page checks whether the configured ComfyUI API is reachable. Users
can select a model, VAE, LoRA entries, prompts, and generation parameters, then
save the generated image into Cloud Drive, share it to the ComfyUI forum board,
or discard the preview. Generation now runs through an async job path so the
page can keep showing queue/node progress until the image is complete. The page
also shows the current mode explicitly (`local` vs `remote / cloud API`) so
users do not need to infer it from start/stop buttons. The mode is shown both
as a badge and as a short explanation line near the panel title.
The default generation wait budget is now 30 minutes on both the frontend
progress poll and the backend route, so large models or slow local GPUs do not
fail early just because the UI timeout was shorter than the actual job.
Model-list reads and Hugging Face Diffusers repo preflight checks are
short-cached and request-deduped in the browser. Manual refresh, local startup,
model download, and model upload explicitly bypass the cache so root still sees
newly installed assets immediately.

Root can configure ComfyUI in two modes from server settings:

- Remote mode: set a full API URL such as `http://127.0.0.1:8192`. Discarding a
  preview only clears the web preview because a remote ComfyUI API does not offer
  a safe file-delete endpoint. The root-only Civitai key and model-import tools
  stay visible in remote mode, but imports write to the server-configured
  ComfyUI models directory; sync or mount that path yourself if the remote host
  is a different machine.
- Local mode: set a local ComfyUI folder and startup script. The image page shows
  a `Start ComfyUI` button; the server only runs the startup script when a user
  presses that button. If another user already started the shared ComfyUI backend,
  later users can use it directly. Root also sees a `Stop ComfyUI` button when
  the shared local process is already available.
- Diffusers in-process mode is disabled by default because it loads model
  weights inside the Flask server process and can consume large RAM/VRAM/CPU.
  Use local or remote ComfyUI as the deployment path. Only set
  `HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS=1` in a controlled single-user
  experiment where main-process resource risk is acceptable.
- In the root settings page, token-like fields are grouped by the setting they
  control: the `Turnstile site key` only appears when CAPTCHA mode is
  `turnstile`, while `Civitai API Key` and `Hugging Face Token` live under the
  AI image settings.
- The AI page keeps the main ComfyUI generation form focused on generation
  only. Root's Civitai model-download tools now live in a separate collapsed
  panel at the bottom of the page so downloading models does not crowd the
  generation controls.
- Each selected LoRA now exposes separate `Model` and `CLIP` strength fields in
  the page. The frontend stores those values in the local draft, and the
  backend still re-validates them into the allowed range before they reach the
  ComfyUI workflow. If that LoRA was downloaded through the root Civitai panel
  and its official `trainedWords` were recorded, adding the LoRA will also
  auto-append any missing trigger words into the positive prompt. The current
  UI only allows LoRAs whose recorded `base_model` is one of `SDXL`, `Pony`,
  `Illustrious`, or `Noob`; `SD1.5`, `Flux`, and unknown-metadata LoRAs are
  rendered as unavailable and the backend rejects direct requests too. Removing
  a selected LoRA now also removes its trigger words unless another selected
  LoRA still needs the same term; selecting `不使用 LoRA` and pressing `加入`
  clears the selected LoRA list.
- The page also exposes a VAE selector. Keeping `use checkpoint builtin VAE`
  uses the model's bundled VAE; selecting another VAE inserts a `VAELoader`
  node into the generated workflow.
- Available Embeddings are loaded from the ComfyUI API and rendered as clickable
  shortcut buttons. Clicking one inserts `<embeddings:name>` into the positive
  prompt; the backend translates that shortcut into ComfyUI's actual embedding
  prompt syntax before queueing the workflow. Clicking the same Embedding again
  removes it. If the Embedding name contains `neg` / `negative`, the shortcut
  targets the negative prompt by default.
- While a long ComfyUI task is running, the frontend idle auto-logout countdown
  is paused so the session does not expire mid-task. That currently includes
  local startup polling, async generation, and root's local-model download job.
- ComfyUI generation is always a background job. The main request returns a
  `job_id` immediately, and backend status / interrupt / generation calls use
  bounded timeouts so a slow model load does not make Flask wait synchronously.
  See [COMFYUI_PERFORMANCE_HARDENING.md](comfyui/COMFYUI_PERFORMANCE_HARDENING.md).

The reference Linux/WSL startup script template is
`scripts/comfyui/comfyui_run_in_linux.template.sh`. Copy it into a ComfyUI portable
folder as `run_in_linux.sh`, then set that folder and script name in root
settings. Do not document or commit workstation-specific absolute paths; use
deployment-local paths such as `/opt/comfyui-portable` in examples. The script
reuses an existing virtual environment when available, creates one only if
needed, installs dependencies with `--install-only`, and supports `--doctor` for
environment checks.

ComfyUI model downloads are root-only. Checkpoints are saved under
`ComfyUI/models/checkpoints`, and LoRA files are saved under
`ComfyUI/models/loras` inside the configured local ComfyUI project. The same
panel can also download Embedding and VAE files into the matching local ComfyUI
model folders. ControlNet and Hypernetwork downloads are intentionally not
offered in this UI because the current generation form does not expose matching
runtime controls. Local mode can delete generated output files when a user
discards a preview; ownership is checked so users can only save or delete their
own generated image references. Root can paste a Civitai page URL, inspect
model versions/files, see the version's official `trainedWords` / trigger
words, and download a selected checkpoint, LoRA, Embedding, or VAE into the
configured local project from that bottom collapsed panel; when a LoRA is
downloaded this way, the server stores a small sidecar metadata file next to
that LoRA so future page loads can auto-insert the same trigger words when the
LoRA is selected again. An optional Civitai API key can be stored in server
settings for authenticated downloads.
Interrupt requests are guarded because ComfyUI interrupt is global: normal users
only interrupt the backend when no other user's generation is active, while root
can force a global interrupt.

### Games and Experiments

The Games page keeps browser game runtimes lazy-loaded. Solo game score submits
use a compact API response so the server does not rebuild the leaderboard in
the submit request and then rebuild it again during the frontend refresh. Chess
leaderboards, visible match lists, invites, and solo ranking queries have
dedicated indexes so larger play histories remain bounded.

The Experiments page remains browser-only: it does not create backend jobs,
tables, or polling traffic. It now chooses a lighter particle/DPR profile on
low-core devices or when the browser reports reduced-motion preference, and it
clamps large animation-frame gaps after tab suspension so resuming the page
does not trigger a large physics jump.

### Appeals

Users can view violation history and submit appeals. The same page also shows
appealable member-governance notices such as role/status/points-rights changes.
Root/manager review is handled from the management pages.

### Account Management

Managers and root can review users, approve registrations, manage roles, inspect
violations, review appeals/reports, and handle governance workflows according to
their authority. Rejecting a pending registration removes that application
account entirely. Deleting an existing account is a soft-delete: the account is
hidden from default admin lists, access is revoked, attached storage is trashed,
but audit/trading/comment history remains preserved for review.

Registration creates the user's official hot wallet and signup gift when the
points layer is available. If points initialization is temporarily unavailable,
the account is still submitted as pending and marked for deferred signup-gift
reissue; after approval, the first successful login retries the gift once and
clears the marker when it succeeds. Normal existing accounts are not eligible
for login-time signup-gift creation unless that deferred marker is present.

Large account lists are treated as management-plane reads: the admin user list
is paginated, indexed by common role/status/level filters, and online-session
status is computed only for the users visible on the current page.

Authenticated users can open `我的資料` to keep a personal appearance
override without changing the root-managed global default theme. That personal
override now covers font family, background style, panel style, sidebar width,
colors, layout, density, radius, font scale, and content width. Root still owns
the global default theme, and can separately decide whether personal appearance
overrides are allowed at all.
Member-rights changes send a governance notice to the affected user and link to
Appeals when the action is appealable.

### Security Center

Root-facing security and operations pages are grouped under Security Center:

- security overview
- audit log
- server log
- health checks
- server health dashboard with grouped service status, work queue, storage,
  readiness/anomaly, and audit-chain findings
- integrity guard; pending warnings are visible for 24 hours, then auto-approved
- server modes
- access controls
- security mechanism toggles
- threshold management
- custom security profiles
- snapshot / restore / reset controls
- system environment summary
- system resource board with cached CPU / GPU / VRAM / RAM gauges

The PointsChain operations panel includes a root-only one-click abnormal-chain
handler. It is intended for safe-mode recovery: the button starts an async
management-plane job, verifies the chain outside the request path, returns a
forensic / branch / emergency-governance recovery plan when needed, and never
restores a ledger backup or overwrites append-only history.

Root/admin PointsChain reads are snapshot-first. Seal, verify, root report,
financial invariants, economy stats, and the operations snapshot use
management-plane jobs for heavy work and `/latest` snapshot endpoints for fast
UI reads. The operations snapshot is bounded and covers service-fee linkage,
initial distribution, private-chain queues, economy fund watermarks, exchange
fund status, disputes, freezes, and emergency governance counts.

### Points Exchange

The Economy branch includes a first-stage spot exchange. The UI displays
`BTC/USDT`, `ETH/USDT`, `XRP/USDT`, `BNB/USDT`, and `PAXG/USDT`. Some legacy DB
and registry keys may still use `*/POINTS` internally for compatibility, but
normal price widgets, errors, and market labels must use the `*/USDT` display
symbols. Spot trading is open to normal users and root. Normal-user settlement
uses the local PointsChain ledger, while root spot settlement uses a separate
simulated trading balance. POINTS are treated as USDT-equivalent in the trading
UI (`1 POINT = 1 USDT`) so market prices match the public quote unit.
BTC/ETH spot execution defaults to Binance public live prices so small servers
do not fetch multi-exchange order books on every normal refresh. If the primary
API is unavailable, execution falls back to fused weighted price using the
remaining healthy providers, then last-good cache within the configured
staleness window. Root can intentionally switch the primary source to fused
weighted price for larger deployments. The backend applies the market jump
threshold after live history exists and fails closed when no fresh or trusted
cached price is available.

The exchange page also shows public candlestick charts for supported display
markets such as BTC/USDT, ETH/USDT, XRP/USDT, BNB/USDT, and PAXG/USDT. The
chart provider chain is Binance, OKX, Coinbase Exchange, Kraken, Gemini, then
Bitstamp. The default timeframe is 15 minutes, with 1-hour, 4-hour, and daily
options available. The chart is also used by the frontend to refresh
the displayed current price; the backend still fetches its own price again
before execution:

- BTC display markets map to public BTC/USDT or BTC/USD provider symbols.
- ETH display markets map to public ETH/USDT or ETH/USD provider symbols.
- XRP display markets map to public XRP/USDT or XRP/USD provider symbols.
- BNB display markets map to public BNB/USDT provider symbols where available.
- PAXG display markets map to public PAXG/USDT provider symbols where available.
- The fixed display conversion is `1 POINT = 1 USDT`.
- If public providers are unavailable, live-price execution can temporarily use
  the recent last-good price within the configured staleness window. After that
  it fails closed with a clear error.
- Fused weighted price aggregates multiple fresh providers, discards stale /
  outlier prices, and can halt higher-risk trading when provider agreement is
  too weak. Keep it as fallback by default on resource-constrained hosts.
- Open limit orders are scanned by the trading background worker and are
  filled when the current execution price reaches the limit.
- Spot trading bots are owned by the user. The exchange page separates DCA bots,
  grid bots, workflow strategy bots, backtest analysis, and execution records.
  DCA bots convert a fixed POINTS budget into a market buy quantity at scan
  time, and exhausted DCA/workflow bots can add more `max_runs` directly from
  the trading page. Grid bots place multiple buy/sell levels inside a configured
  range and can prompt the user to buy missing spot inventory before creation.
  Strategy bots use readable workflow JSON generated by the standalone
  `/trading-workflow-editor.html` page. The workflow editor is node graph based:
  it stores `nodes` and `edges`, validates input/output ports, supports
  TRUE/FALSE branches, nested AND/OR/NOT logic nodes, cooldown/control nodes,
  branch priority, and sequential action steps. Backtests use the same DCA,
  grid, or workflow configuration and never place orders or mutate ledger
  state. Production bot execution should be driven by the server-side trading
  background engine, with manual scan actions kept as controlled diagnostics.
- Spot positions expose backend-calculated cost basis, current value,
  unrealized PnL, realized PnL, and cumulative fees. Cost basis includes the
  remaining spot cost, an estimated entry fee, and the estimated exit fee at the
  current market price. Realized PnL is recorded on each sell fill and is
  replay-verified by the trading state checker.
- Fee and interest accrual preserve decimal carry until a real settlement
  boundary. Integer POINT rounding occurs on spot sell, bot stop, lending
  settlement, or liquidation, and any positive fractional remainder rounds up.
  Margin open/close fees are based on full notional exposure.
- See [Trading System And Bots](trading/TRADING.md) for the full trading, bot,
  workflow editor, backtest, and validation guide.

When `root` enables BTC_trade in trading settings, the BTC market can also show
a BTC-only signal panel. The feature is disabled by default. On enable, the
server can clone/update the configured GitHub branch and run BTC_trade setup
steps; any failure only hides the panel and shows a root warning. The panel
understands the newer BTC_trade runtime report fields, including strategy
version, fear/greed, portfolio equity, PnL, report text, and next prediction
countdown. The optional bridge script is owned by this project at
`scripts/trading/bridges/btc_signal_bridge.py`.

Trading funds are separated by account type:

- Normal users trade with the POINTS they actually own in their PointsChain
  wallet.
- `root` can use spot and derivatives simulation with a separate simulated trading
  balance. It starts at 10000 POINTS and does not write to PointsChain or mutate
  the root account wallet.
- Root can reset this simulated trading balance back to 10000 POINTS from the
  exchange control panel.
- Futures / derivatives simulation is root-only at this stage. Root can open and
  close simulated long/short positions; non-root users can only use spot.
- Borrow trading is experimental and root-controlled. When enabled, the server
  records margin collateral freezes in PointsChain, shows account-level
  cross-margin equity/maintenance/free-margin status, scans for liquidation,
  and verifies collateral locks during trading state checks. Individual rows
  still show a per-position estimated liquidation price, but actual forced
  liquidation is based on whole-account maintenance.

## Account and Permission Model

Roles:

- `super_admin`: full control plane
- `manager`: operational moderation and user review
- `user`: normal application use

Member levels:

- `newbie`
- `normal`
- `trusted`
- `vip`
- `restricted`
- `suspended`

`root` and bootstrap admin-style accounts are special operational accounts and
are not treated like ordinary user-level accounts.

## Runtime Files

The web app creates runtime data on first boot. These files are intentionally not
tracked by git:

- SQLite database
- logs
- chat transcripts
- upload/storage files
- generated keys
- local TLS certificate/key files
- integrity manifest
- bug reports
- local security reports

For clean deployments, clone the repository, prepare the runtime directories,
and run `python3 server.py --doctor` followed by `python3 server.py`.
For development, prefer `./test_for_develop.sh` so runtime stays under `/tmp`.
