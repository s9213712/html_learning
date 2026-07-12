# API Reference

一句話說明：這份文件是目前 **已實作** 的 `hackme_web` API 路徑總表，專門給開發者、整合腳本與 CLI 操作使用。

## 設計目的

原本 API 說明分散在：

- [For_developer.md](For_developer.md)
- [TRADING.md](trading/TRADING.md)
- [TRADING_BOT_AUDIT.md](trading/TRADING_BOT_AUDIT.md)
- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [SECURITY.md](SECURITY.md)

這份文件把 **目前可用的正式 API** 收斂成單一入口。

不在這份文件內的內容：

- `docs/AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md` 這種「已拍板但尚未實作」規格
- 細到每個 JSON 欄位的完整 schema

如果某支 API 已有專屬深層文件，這份文件會優先連過去，而不是重複抄第二份規格。

## 使用方法

### 認證與 CSRF

- 公開讀取：
  - `GET /api/version`
  - `GET /api/site-config`
  - `GET /api/csrf-token`
- 所有寫入 API 預設都需要：
  - session cookie
  - `X-CSRF-Token`
- 登入後的 authenticated CSRF token 會保留短期最近值，避免多分頁或併發請求互相造成
  `invalid_authenticated` 誤報；未登入 / 登入流程 token 仍為 single-use。
- 本地 HTTPS 開發通常要用：

```bash
curl -k -sS https://127.0.0.1:5000/api/version
```

### 角色約定

| 角色 | 說明 |
|---|---|
| `anonymous` | 未登入 |
| `logged-in` | 任意已登入使用者 |
| `manager` | admin / manager |
| `root` | super_admin / root-only |

## API 總覽

### Public / Session

來源：`routes/public.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/` | anonymous | 首頁 |
| GET | `/videos` | anonymous | 影音入口 |
| GET | `/api/csrf-token` | anonymous | 取得目前 session 的 CSRF token |
| GET | `/api/site-config` | anonymous | 前台 site config |
| GET | `/api/version` | anonymous | 版本資訊 |
| GET | `/api/password-strength` | anonymous | 密碼強度提示 |
| GET | `/api/captcha/challenge` | anonymous | CAPTCHA challenge |
| POST | `/api/register` | anonymous | 註冊 |
| POST | `/api/login` | anonymous | 登入 |
| POST | `/api/logout` | logged-in | 登出 |
| GET | `/api/session/idle-timeout` | logged-in | 閒置登出設定 |
| GET | `/api/me` | logged-in | 目前登入者資訊 |
| PUT | `/api/me/appearance` | logged-in | 個人外觀 |
| POST | `/api/password-reset/request` | anonymous | 發起重設密碼（`root` 不適用；請改用離線 recovery CLI） |
| POST | `/api/password-reset/confirm` | anonymous | 確認重設密碼（`root` token 會被拒絕） |
| POST | `/api/email-verification/request` | logged-in | 發送驗證郵件 |
| POST | `/api/email-verification/confirm` | anonymous | 確認驗證 |

### Account / Users / Sessions

來源：`routes/users.py`, `routes/moderation.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/account/sessions` | logged-in | 查看自己的 session |
| DELETE | `/api/account/sessions/<session_id>` | logged-in | 登出單一 session |
| POST | `/api/account/sessions/logout-all` | logged-in | 全部登出 |
| GET | `/api/account/reputation/summary` | logged-in | 信譽摘要 |
| GET | `/api/account/reputation/history` | logged-in | 信譽歷史 |
| GET | `/api/admin/users` | manager | 使用者列表 |
| POST | `/api/admin/users` | manager | 新增帳號 |
| GET | `/api/admin/users/<user_id>` | manager | 讀單一使用者 |
| PUT | `/api/admin/users/<user_id>` | manager | 編輯使用者 |
| DELETE | `/api/admin/users/<user_id>` | manager | 停用 / 軟刪除 |
| POST | `/api/admin/users/<user_id>/review-registration` | manager | 審核註冊 |
| POST | `/api/admin/users/<user_id>/block` | manager | 封鎖帳號 |
| POST | `/api/admin/users/<user_id>/promote` | manager | 升權 |
| POST | `/api/admin/users/<user_id>/demote` | manager | 降權 |
| POST | `/api/admin/users/<user_id>/violation` | manager | 記違規 |
| POST | `/api/admin/users/<user_id>/reset-violations` | manager | 清違規 |
| GET | `/api/admin/password-reset-requests` | manager | 密碼重設審核列表 |
| POST | `/api/admin/password-reset-requests/<request_id>/approve` | manager | 通過重設 |
| POST | `/api/admin/password-reset-requests/<request_id>/reject` | manager | 駁回重設 |
| GET | `/api/admin/member-level-rules` | manager | 會員等級規則 |
| PUT | `/api/admin/member-level-rules/<level>` | manager | 更新會員等級規則 |
| GET | `/api/admin/moderation-actions` | manager | 治理動作列表 |
| GET | `/api/admin/mod-notes/<user_id>` | manager | 管理備註 |
| GET | `/api/admin/moderation/proposals` | manager | 治理提案列表 |
| GET | `/api/admin/moderation/proposals/<proposal_id>` | manager | 單一提案 |
| POST | `/api/admin/moderation/proposals/<proposal_id>/vote` | manager | 提案投票 |
| POST | `/api/admin/moderation/proposals/<proposal_id>/execute` | manager | 執行提案 |
| POST | `/api/root/moderation/proposals/<proposal_id>/override` | root | root override |

### Chat / Friends / Audit Export

來源：`routes/chat.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET/POST | `/api/chat/rooms` | logged-in | 列表 / 建立聊天室 |
| POST | `/api/chat/rooms/<room_id>/join` | logged-in | 加入聊天室 |
| GET/DELETE | `/api/chat/rooms/<room_id>` | logged-in | 讀 / 離開 / 管理聊天室 |
| POST | `/api/chat/rooms/<room_id>/invites` | logged-in | 邀請好友 |
| GET | `/api/chat/rooms/<room_id>/export` | logged-in | 匯出聊天室 |
| GET/POST | `/api/chat/rooms/<room_id>/messages` | logged-in | 訊息列表 / 發送訊息 |
| POST | `/api/chat/messages/<message_id>/report` | logged-in | 檢舉訊息 |
| DELETE | `/api/chat/messages/<message_id>` | logged-in | 刪除訊息 |
| GET | `/api/chat/friends` | logged-in | 朋友列表 |
| GET/POST | `/api/chat/friends/requests` | logged-in | 好友邀請列表 / 發送邀請 |
| POST | `/api/chat/friends/requests/<request_id>/<decision>` | logged-in | 接受 / 拒絕邀請 |
| DELETE | `/api/chat/friends/<friend_user_id>` | logged-in | 移除好友 |
| GET | `/api/audit` | logged-in | 個人可見 audit |

聊天匿名與指定對象補充：

- `POST /api/chat/rooms` 可帶 `allow_anonymous`；只有非一對一 / 非 private PM 的聊天室可啟用。
- `POST /api/chat/rooms` 與 `POST /api/chat/rooms/<room_id>/join` 可帶 `anonymous` / `anonymous_enabled`，前提是聊天室允許匿名。
- 官方聊天群對一般使用者固定匿名展示；root / manager 仍可為治理目的看到原發言者。
- 一般使用者建立 PM / private group 時，後端會檢查對方是否為好友；root / manager 為管理目的有 PM 例外。

全站個人主頁 / 好友 API：`routes/chat.py` 的好友 API 仍是聊天頁相容入口；一般好友管理入口已移到主側欄的「個人面板」。

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/users/<user_id>/profile` | logged-in | 查看個人主頁 |
| GET | `/api/users/me/profile` | logged-in | 查看自己的個人面板資料與好友代碼 |
| PUT | `/api/users/me/profile` | logged-in | 更新自己的顯示名稱、簡介、簽名、所在地、網站與公開範圍 |
| POST | `/api/users/me/friend-code/rotate` | logged-in | 重新產生好友代碼 |
| GET | `/api/users/target-options?context=pm` | logged-in | 指定對象候選清單；一般使用者依 context 只回好友，root / manager 在站務 context 可看全站 |
| GET | `/api/friends` | logged-in | 好友列表；root / manager 置頂 |
| GET | `/api/friends/requests` | logged-in | 收到與送出的好友申請 |
| POST | `/api/friends/request` | logged-in | 透過個人主頁送出好友申請 |
| POST | `/api/friends/requests/<request_id>/accept` | logged-in | 同意好友申請 |
| POST | `/api/friends/requests/<request_id>/reject` | logged-in | 拒絕好友申請 |
| POST | `/api/friends/add-by-code` | logged-in | 用好友代碼直接建立好友 |
| DELETE | `/api/friends/<user_id>` | logged-in | 移除好友 |

完整需求見 [USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md)。

### Community / Forum / Announcements

來源：`routes/community.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET/POST | `/api/community/announcements` | logged-in / manager | 列表 / 發布公告 |
| PUT/DELETE | `/api/community/announcements/<announcement_id>` | manager | 編輯 / 刪除公告 |
| GET/POST | `/api/community/categories` | logged-in / manager | 分類列表 / 建立分類 |
| PUT/DELETE | `/api/community/categories/<category_id>` | manager | 編輯 / 刪除分類 |
| GET/POST | `/api/community/boards` | logged-in / manager | 看板列表 / 建立看板 |
| GET | `/api/community/boards/reviews` | manager | 看板審核列表 |
| POST | `/api/community/boards/<board_id>/review` | manager | 看板審核 |
| GET/PUT/DELETE | `/api/community/boards/<board_id>` | logged-in / manager | 讀 / 編輯 / 刪除看板 |
| GET/POST | `/api/community/boards/<board_id>/moderators` | manager | 看板管理員列表 / 新增 |
| DELETE | `/api/community/boards/<board_id>/moderators/<user_id>` | manager | 移除看板管理員 |
| GET/POST | `/api/community/boards/<board_id>/threads` | logged-in | 文章列表 / 發文 |
| GET | `/api/community/threads/reviews` | manager | 文章審核 |
| POST | `/api/community/threads/<thread_id>/review` | manager | 文章審核處理 |
| GET/PUT/DELETE | `/api/community/threads/<thread_id>` | logged-in | 讀 / 編輯 / 刪除文章 |
| POST | `/api/community/threads/<thread_id>/reaction` | logged-in | 文章反應 |
| POST | `/api/community/threads/<thread_id>/reward` | logged-in | 打賞文章 |
| GET/POST | `/api/community/threads/<thread_id>/posts` | logged-in | 回覆列表 / 回覆 |
| POST | `/api/community/posts/<post_id>/reaction` | logged-in | 回覆反應 |
| POST | `/api/community/posts/<post_id>/pin` | manager | 置頂回覆 |
| POST | `/api/community/posts/<post_id>/penalty` | manager | 回覆處分 |
| PUT/DELETE | `/api/community/posts/<post_id>` | logged-in / manager | 編輯 / 刪除回覆 |
| POST | `/api/community/threads/<thread_id>/lock` | manager | 鎖文 |
| POST | `/api/community/threads/<thread_id>/sticky` | manager | 置頂文章 |
| POST | `/api/community/threads/<thread_id>/curate` | manager | 精選文章 |

### Notifications / Reports / Appeals

來源：`routes/reports_notifications.py`, `routes/appeals.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/notifications` | logged-in | 通知列表 |
| POST | `/api/notifications/<notification_id>/read` | logged-in | 單筆已讀 |
| POST | `/api/notifications/<notification_id>/dismiss` | logged-in | 隱藏通知並寫入 `dismissed_at` |
| POST | `/api/notifications/read-all` | logged-in | 全部已讀 |
| POST | `/api/admin/notifications/send` | manager | 發送通知 |
| POST | `/api/reports` | logged-in | 一般檢舉 |
| GET | `/api/admin/reports` | manager | 檢舉列表 |
| POST | `/api/admin/reports/<report_id>/claim` | manager | 認領檢舉 |
| POST | `/api/admin/reports/<report_id>/resolve` | manager | 結案檢舉 |
| GET/POST | `/api/appeals` | logged-in | 申覆列表 / 建立申覆 |
| GET | `/api/admin/appeals` | manager | 申覆列表 |
| POST | `/api/admin/appeals/<appeal_id>/review` | manager | 審核申覆 |

### Platform Centers

來源：`routes/jobs.py`, `routes/share_management.py`, `routes/trading.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/jobs` | logged-in | 目前使用者任務列表，支援 `status` / `limit` |
| GET | `/api/admin/jobs` | root | 全站任務列表 |
| GET | `/api/jobs/<job_uuid>` | owner / root | 單一任務 |
| GET | `/api/jobs/<job_uuid>/events` | owner / root | 任務事件 |
| POST | `/api/jobs/<job_uuid>/cancel` | owner / root | 要求取消任務 |
| POST | `/api/jobs/<job_uuid>/retry` | owner / root | 重試 failed / cancelled / retry_wait 任務 |
| GET | `/api/cloud-drive/refs` | context member | context 附件列表，支援 `limit` / `offset` |
| GET | `/api/shares` | logged-in | 分享連結列表，manager 可用 `all=1` |
| PUT | `/api/shares/<type>/<id>` | owner / manager | 編輯 file / album / video 分享選項，例如密碼、到期、次數、指定用戶、是否允許預覽 |
| POST | `/api/shares/<type>/<id>/revoke` | owner / manager | 撤銷 file / album / video 分享 |
| GET | `/api/shares/<type>/<id>/access-events` | owner / manager | 分享建立、存取、到期、撤銷事件 |
| GET | `/api/trading/asset-overview` | logged-in | 交易資產總覽 |
| GET | `/api/admin/trading/asset-overview` | manager | 全站交易資產 / 低信心價格摘要 |

### Cloud Drive / Files / Remote Download

來源：`routes/files.py`

這組 API 很多，建議搭配 [04_USER_GUIDE.md](04_USER_GUIDE.md) 與 [WEB.md](WEB.md) 一起看。

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/files/quota` | logged-in | quota 摘要 |
| GET | `/api/files/security-policy` | logged-in | 檔案安全規則 |
| GET | `/api/files/privacy-modes` | logged-in | 隱私模式選項 |
| POST | `/api/files/upload` | logged-in | 舊上傳入口 |
| POST | `/api/cloud-drive/upload` | logged-in | 主上傳入口 |
| GET/POST | `/api/cloud-drive/files` | logged-in | 檔案列表 / 建立文字檔 |
| GET | `/api/storage/files` | logged-in | storage file listing |
| POST | `/api/storage/files/attach-existing` | logged-in | 附加既有檔案 |
| POST | `/api/storage/folders` | logged-in | 建資料夾 |
| POST | `/api/storage/folders/trash` | logged-in | 資料夾丟垃圾桶 |
| POST | `/api/storage/folders/move` | logged-in | 移動資料夾 |
| POST | `/api/storage/folders/album` | logged-in | 資料夾轉相簿 |
| POST | `/api/storage/files/<storage_file_id>/organize` | logged-in | 整理檔案 |
| GET | `/api/storage/files/<storage_file_id>/download` | logged-in | 下載 |
| GET/POST | `/api/storage/trash` | logged-in | 垃圾桶列表 / 操作 |
| GET/POST | `/api/storage/albums` | logged-in | 相簿列表 / 建立相簿 |
| POST | `/api/storage/albums/batch-share` | logged-in | 以 1-100 個 storage file ID 原子建立 unlisted 分享相簿；任一檔案失敗會整批回滾 |
| POST | `/api/storage/albums/smart-organize` | logged-in | 智慧整理相簿 |
| GET/PUT/DELETE | `/api/storage/albums/<album_id>` | logged-in | 單一相簿 |
| GET/POST | `/api/storage/share-links` | logged-in | 建立分享 |
| POST | `/api/storage/share-links/<link_id>/revoke` | logged-in | 撤銷分享 |
| GET | `/api/storage/shared/<token>/download` | anonymous | 共享下載 |
| GET | `/api/storage/shared/<token>` | anonymous | 共享檔案 landing metadata |
| GET | `/api/storage/shared/<token>/preview` | anonymous | 共享檔案預覽 metadata；需分享設定允許 preview |
| GET | `/api/storage/shared/<token>/preview/content` | anonymous | 共享檔案瀏覽器預覽內容；strict E2EE 只回密文或 metadata |
| GET | `/api/storage/shared/albums/<token>` | anonymous | 共享相簿 |
| GET | `/api/storage/shared/albums/<token>/files/<file_id>/download` | anonymous | 共享相簿檔案下載 |
| PUT | `/api/shares/album/<share_id>` | logged-in | 在分享管理更新相簿分享密碼、到期時間、最大存取次數 |
| GET | `/api/cloud-drive/remote-download/capabilities` | logged-in | 遠端下載能力，例如 aria2 / BT 可用狀態 |
| GET/POST | `/api/cloud-drive/remote-download/tasks` | logged-in | 遠端下載任務 |
| GET/DELETE | `/api/cloud-drive/remote-download/tasks/<task_id>` | logged-in | 單一遠端下載任務 |
| POST | `/api/cloud-drive/remote-download/tasks/<task_id>/pause` | logged-in | 暫停遠端下載任務 |
| POST | `/api/cloud-drive/remote-download/tasks/<task_id>/resume` | logged-in | 恢復遠端下載任務 |
| POST | `/api/cloud-drive/remote-download/tasks/<task_id>/cancel` | logged-in | 取消遠端下載任務 |
| POST | `/api/cloud-drive/remote-download/torrent-tasks` | logged-in | 上傳 `.torrent` 建立 BT 任務 |
| POST | `/api/cloud-drive/resumable-upload/start` | logged-in | 建立 resumable/chunk upload session |
| GET | `/api/cloud-drive/resumable-upload/sessions` | logged-in | 列出自己的未完成分段上傳 session |
| GET | `/api/cloud-drive/resumable-upload/<session_id>/status` | logged-in | 分段上傳 session 狀態 |
| POST | `/api/cloud-drive/resumable-upload/<session_id>/chunks/<chunk_index>` | logged-in | 上傳指定 chunk |
| POST | `/api/cloud-drive/resumable-upload/<session_id>/complete` | logged-in | 完成分段上傳並保存到雲端硬碟 |
| DELETE | `/api/cloud-drive/resumable-upload/<session_id>` | logged-in | 取消分段上傳 session |
| GET/POST | `/api/cloud-drive/refs` | logged-in | 雲端引用 |
| DELETE | `/api/cloud-drive/refs/<ref_id>` | logged-in | 刪除引用 |
| GET | `/api/cloud-drive/files/<file_id>/preview` | logged-in | 預覽 metadata |
| GET | `/api/cloud-drive/files/<file_id>/preview/content` | logged-in | 預覽內容 |
| GET | `/api/cloud-drive/files/<file_id>/text` | logged-in | 文字內容 |
| GET | `/api/cloud-drive/files/<file_id>/download` | logged-in | 下載檔案 |
| GET | `/api/cloud-drive/files/<file_id>/e2ee-key` | logged-in | E2EE key |
| POST | `/api/files/<file_id>/share` | logged-in | 建立分享 |
| POST | `/api/files/<file_id>/share/revoke` | logged-in | 撤銷分享 |
| POST | `/api/cloud-drive/announcement-attachment-requests` | logged-in | 公告附件請求 |
| POST | `/api/root/announcement-attachment-requests/<request_id>/review` | root | 審核公告附件 |
| GET | `/api/admin/storage/summary` | manager | storage 總覽 |
| GET | `/api/admin/storage/users` | manager | 使用者 storage 列表 |
| GET | `/api/root/storage/users` | root | root storage 列表 |
| GET | `/api/root/storage/users/<user_id>` | root | 單一使用者 storage |
| PUT/DELETE | `/api/root/storage/users/<user_id>/quota-override` | root | quota override |

### Videos

來源：`routes/videos.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| POST | `/api/videos/publish` | logged-in | 發布影音 |
| POST | `/api/videos/upload` | logged-in | 上傳影音 |
| GET | `/api/videos?sort=new\|hot\|trending&q=keyword&page=1` | anonymous | 影音列表與搜尋 |
| GET | `/api/videos/<video_id>` | logged-in / manager | 單一影音 |
| PUT | `/api/videos/<video_id>/share-link` | logged-in / manager | 更新或重建分享連結 |
| DELETE | `/api/videos/<video_id>/share-link` | logged-in / manager | 撤銷分享連結 |
| POST | `/api/media/<file_id>/prepare-stream` | logged-in / manager | 建立 HLS 衍生檔 |
| POST | `/api/media/<file_id>/e2ee-stream-v2` | logged-in / manager | 上傳 strict E2EE Streaming v2 manifest 與密文 bundle |
| GET | `/api/media/<file_id>/stream-status` | logged-in / manager | 查影音衍生狀態 |
| GET | `/api/videos/<video_id>/playback` | anonymous | 取得 direct / HLS 播放決策 |
| GET | `/api/videos/<video_id>/e2ee-stream-v2/manifest` | logged-in / manager | 取得 strict E2EE Streaming v2 manifest |
| GET | `/api/videos/<video_id>/e2ee-stream-v2/chunks/<chunk_index>` | logged-in / manager | 取得 strict E2EE Streaming v2 密文 chunk |
| GET | `/api/videos/<video_id>/stream` | anonymous | 串流 |
| GET | `/api/videos/<video_id>/hls/master.m3u8` | anonymous | HLS master manifest |
| GET | `/api/videos/<video_id>/hls/<variant>/playlist.m3u8` | anonymous | HLS variant manifest |
| GET | `/api/videos/<video_id>/hls/<variant>/<segment>` | anonymous | HLS segment / init |
| GET | `/api/videos/<video_id>/cover` | anonymous | 封面 |
| POST | `/api/videos/<video_id>/view` | anonymous | 記觀看 |
| POST | `/api/videos/<video_id>/like` | logged-in | 按讚 |
| GET | `/api/videos/<video_id>/comments` | anonymous | 評論列表 |
| POST | `/api/videos/<video_id>/comment` | logged-in | 留言 |
| POST | `/api/videos/<video_id>/tip` | logged-in | 打賞 |
| GET | `/shared/videos/<token>` | anonymous | 分享頁 HTML |
| POST | `/api/videos/shared/<token>/unlock` | anonymous | 驗證第二層分享密碼 |
| GET | `/api/videos/shared/<token>` | anonymous | 分享影音 metadata |
| GET | `/api/videos/shared/<token>/playback` | anonymous | 分享影音 playback 決策 |
| GET | `/api/videos/shared/<token>/stream` | anonymous | 分享影音 direct stream |
| GET | `/api/videos/shared/<token>/cover` | anonymous | 分享影音封面 |
| GET | `/api/videos/shared/<token>/e2ee-key` | anonymous | E2EE 分享 envelope metadata |
| GET | `/api/videos/shared/<token>/ciphertext` | anonymous | E2EE 分享密文內容 |
| GET | `/api/videos/shared/<token>/e2ee-stream-v2/manifest` | anonymous | 分享頁 strict E2EE Streaming v2 manifest |
| GET | `/api/videos/shared/<token>/e2ee-stream-v2/chunks/<chunk_index>` | anonymous | 分享頁 strict E2EE Streaming v2 密文 chunk |
| GET | `/api/videos/shared/<token>/hls/master.m3u8` | anonymous | 分享影音 HLS master |
| GET | `/api/videos/shared/<token>/hls/<variant>/playlist.m3u8` | anonymous | 分享影音 HLS variant |
| GET | `/api/videos/shared/<token>/hls/<variant>/<segment>` | anonymous | 分享影音 HLS segment |

播放決策補充：

- `/api/videos/<video_id>/playback` 與 `/api/videos/shared/<token>/playback`
  現在會回：
  - `mode`: `direct` / `hls` / `e2ee_direct` / `e2ee_stream_v2`
  - `player_strategy`: `direct_only` / `native_hls_or_hlsjs` / `browser_e2ee_full_fallback` / `browser_e2ee_stream_v2`
  - `stream_warning`
  - `hls_js_url`
- strict E2EE Streaming v2 補充：
  - manifest / chunk endpoint 永遠只回密文，不回明文、不接收 `raw_file_key`、`e2ee_password`、`vk`
  - 若沒有 v2 manifest，API 會明確回 `available=false` 與 fallback 指示，前端應退回舊版完整解密播放
- `/api/videos/<video_id>` 若影片為 `unlisted` 且目前 actor 可管理分享，`video.share_link`
  會回：
  - `state`
  - `state_message`
  - `remaining_views`
  - `password_locked_until`
  - `requires_fragment_key`
- `PUT /api/videos/<video_id>/share-link`
  - 接受：
    - `share_password`
    - `share_expires_at`
    - `share_max_views`
    - `share_wrapped_file_key_envelope`
    - `regenerate`
  - 拒絕：
    - `raw_file_key`
    - `e2ee_password`
    - `vk`
    - `share_key`
    - `share_key_bytes`
- Safari 走原生 HLS；桌機 Chrome / Firefox / Edge 由前端載入同源
  `hls.js` 後播放 `master_url`；strict `e2ee` 只走瀏覽器端解密播放。

### Games

來源：`routes/games.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/games/catalog` | logged-in | 遊戲列表 |
| GET | `/api/games/users` | logged-in | 遊戲玩家摘要 |
| GET/POST | `/api/games/chess/invites` | logged-in | 西洋棋邀請 |
| POST | `/api/games/chess/invites/<invite_id>/<action>` | logged-in | 接受 / 拒絕邀請 |
| POST | `/api/games/chess/practice` | logged-in | 練習局 |
| GET/POST | `/api/games/chess/matches` | logged-in | 對局列表 / 建立 |
| GET/DELETE | `/api/games/chess/matches/<match_id>` | logged-in | 對局詳情 |
| POST | `/api/games/chess/matches/<match_id>/move` | logged-in | 落子 |
| POST | `/api/games/chess/matches/<match_id>/resign` | logged-in | 認輸 |
| GET | `/api/games/chess/leaderboard` | logged-in | 排行榜 |
| POST | `/api/games/<game_key>/ai-move` | logged-in | 黑白棋 / 圍棋 / 五子棋 AI 著手；圍棋可選 `katago` 難度 |
| GET | `/api/games/<game_key>/solo-leaderboard` | logged-in | 單機排行榜 |
| POST | `/api/games/<game_key>/solo-scores` | logged-in | 單機分數 |
| POST | `/api/root/games/chess/weekly-rewards/award` | root | 發周獎勵 |

`/api/games/<game_key>/ai-move` 目前只接受 `reversi`、`go`、`gomoku`。圍棋 `katago` 難度會使用本機 KataGo analysis engine；可先執行 `python3 scripts/games/setup_katago.py` 自動下載 binary、模型並在 `$HACKME_RUNTIME_DIR/katago/` 產生設定。
完整 payload、response、前端調用地圖與 benchmark 教學見
[games/references/BOARD_AI_BENCHMARK.md](games/references/BOARD_AI_BENCHMARK.md)。

### Economy / PointsChain

`feature_economy_enabled` controls the basic points ledger, catalog, wallet summary, CSV export, and normal service-fee spending. `feature_points_chain_enabled` controls the heavier PointsChain layer: wallet identities, wallet-to-wallet transactions, Explorer, finality/proved status, block sealing, root chain report, and official Treasury governance execution. Restorable PointsChain ledger backups are intentionally disabled. When the chain flag is off, those chain endpoints return `503` with `code=points_chain_disabled`; the basic points endpoints remain available.

來源：`routes/economy.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/points/wallet` | logged-in | 積分錢包 |
| GET | `/api/points/wallet/onboarding` | logged-in | 查錢包 onboarding；回傳多錢包列表、付款錢包餘額、註冊禮與 `initial_grant` 狀態 |
| POST | `/api/points/wallet/onboarding` | logged-in | 建立或綁定站內託管錢包（pc0，官方託管）、冷錢包、匯入冷錢包；legacy/API `multisig` 只會成為 `user_multisig_preview` 收款/觀察錢包，不能轉出 |
| GET | `/api/admin/points/governance/treasury-signer-center` | manager+ | 官方財庫多簽 signer center：官方錢包、signer roles/weight、threshold、pending treasury proposals、可簽署項目與 readiness |
| DELETE | `/api/points/wallet/onboarding` | logged-in | 刪除目前或指定冷錢包身份；不刪 append-only 帳本 |
| GET | `/api/points/ledger` | logged-in | 個人 ledger |
| GET | `/api/points/explorer/search?q=<query>` | public safe GET | PointsChain explorer 查交易 hash、Ledger UUID、錢包地址或區塊 |
| GET | `/api/points/explorer/tx/<ledger_ref>` | public safe GET | 查單筆鏈上交易 |
| GET | `/api/points/explorer/wallet/<address>` | public safe GET | 查錢包餘額與近期交易 |
| GET | `/api/points/explorer/block/<block_ref>` | public safe GET | 查區塊狀態與交易列表 |
| POST | `/api/points/explorer/accelerate` | logged-in | 送出模擬鏈交易加速費；費用走 append-only ledger debit 並進 BURN。設定驅動自動發放交易免鏈上費用；root 人工操作官方錢包不算自動發放 |
| GET | `/api/points/transactions` | logged-in | 目前登入者近期 wallet-to-wallet 轉入 / 轉出交易；root 會看到全站近期 pending/confirmed 交易與官方 Treasury 發點；包含 `transaction_hash`、pending/confirmed/failed、Proved 進度與 pending 統計，不含 username 標籤 |
| POST | `/api/points/transactions/submit` | logged-in | 用戶以自己的錢包地址送出交易；`pc0 -> pc0` 為站內託管帳本即時轉帳、免鏈上 fee；`pc0 -> burn` 寫入 system burn sink；`pc0 -> pc1` 走 withdrawal bridge lock 並等待 Proved；`pc1/cold -> pc0` 會被拒絕並引導使用平台入金地址 |
| GET | `/api/points/wallet/export.csv` | logged-in | 匯出錢包 |
| GET | `/api/points/catalog` | logged-in | 點數商品目錄 |
| GET/PUT | `/api/root/economy/catalog` | root | root 調整商品目錄 |
| GET | `/api/points/rules` | logged-in | 點數規則 |
| POST | `/api/points/spend` | logged-in | 站內服務費支付；預設使用 `pc0` 站內託管錢包即時 internal debit，`network_fee_points=0`、`service_fee_points=amount`，收入進官方 Treasury 可 replay 對帳。自管冷錢包直接服務付款目前拒絕，需先入金到 pc0 或等待正式 cold-chain approval rail |
| GET | `/api/points/ledger/<ledger_uuid>/proof` | logged-in | ledger proof |
| GET | `/api/admin/points/wallets/<user_id>` | manager | 已停用，回 410；不得用 user_id 查用戶錢包 |
| POST | `/api/root/points/wallets/<user_id>/sanction` | root | 已停用，回 410；不得由 root 直接處分用戶錢包 |
| GET | `/api/admin/points/ledger` | manager | 已停用，回 410；請改用公開 Explorer 以 hash、地址或區塊查詢 |
| POST | `/api/admin/points/adjust` | manager | 已停用，回 410；不得手動加減用戶積分 |
| POST | `/api/root/points/official-wallet/grant` | root | 建立官方 Treasury 撥款治理提案；只接受 `destination_wallet_address`，不接受 username/user_id。提案通過、timelock 與多簽條件完成後才執行；若目的地是已登記 pc0 official hot/system fund，執行時為 internal settlement，不等待 20/20 Proved |
| GET | `/api/admin/points/pending-rewards` | manager | 已停用，回 410；官方發點需走官方錢包送單或治理核准規則 |
| POST | `/api/admin/points/pending-rewards/<pending_reward_id>/review` | manager | 已停用，回 410；後台待審加點流程移除 |
| POST | `/api/root/points/chain/seal` | root | async seal job：2 秒內回 `202 + job_id`，結果寫入 latest snapshot |
| GET/POST | `/api/root/points/chain/verify`, `/api/root/points/chain/verify/jobs` | root | async bounded verify job；`/latest` 讀快照 |
| GET | `/api/root/points/chain/recovery` | root | safe mode / forensic / branch-governance recovery status |
| POST | `/api/root/points/chain/recovery/auto-handle` | root | async verify/recovery-guidance job；never restores a backup；`/latest` 讀快照 |
| POST | `/api/root/points/chain/backups` | root | 已停用，回 410；PointsChain 不建立可還原 ledger backup |
| POST | `/api/root/points/chain/recovery/approve` | root | 已停用，回 410；不得以備份覆寫 append-only ledger |
| GET/POST | `/api/root/points/report`, `/api/root/points/report/jobs` | root | snapshot-first points 報表；refresh/start job 才背景重建，含封塊、審計、異常與近期未封交易 hash |
| GET/POST | `/api/root/points/financial-invariants`, `/api/root/points/financial-invariants/jobs` | root | async financial invariant audit；`/latest` 讀快照 |
| GET | `/api/root/points/audit` | root | points audit |
| POST | `/api/root/points/ledger/<ledger_uuid>/rollback` | root | 已停用，回 410；修正必須追加新的鏈上補償交易 |
| GET/POST | `/api/admin/points/economy/stats`, `/api/admin/points/economy/stats/jobs` | manager | async economy stats；`/latest` 讀 Phase 1A replay-derived fund summary 快照 |
| GET/POST | `/api/admin/points/operations/snapshot`, `/api/admin/points/operations/snapshot/jobs` | manager | bounded 營運控制 snapshot：服務扣點連接、初始分配、私鏈 queue、交易所基金、緊急治理與風險摘要 |

> 注意：`docs/AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md` 是 Phase 3 規格，不代表現在已可呼叫。
> Phase 1B 的 `/api/admin/economy/transfers/*`、ledger、replay-status、rebuild-derived-balances API 尚未開放；目前只提供既有 stats read model。

### Trading / Bots / Margin

來源：`routes/trading.py`
深層規格請看 [TRADING.md](trading/TRADING.md) 與 [TRADING_BOT_AUDIT.md](trading/TRADING_BOT_AUDIT.md)。

目前預設 seed 市場包含：

- user-facing display symbols: `BTC/USDT`, `ETH/USDT`, `XRP/USDT`,
  `BNB/USDT`, `PAXG/USDT`
- legacy internal registry symbols may still use `*/POINTS` for DB/API
  compatibility. They must not leak into normal price widgets, errors, or user
  market labels; the UI treats `1 POINT = 1 USDT` for display and calculation.

正式啟用中的市場、precision、lot size、tick size、provider mapping 與
`allow_risk_grade_usage` 則由 root 後台的 trading market registry 決定。

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/trading/markets` | logged-in | 市場列表 |
| GET | `/api/trading/dashboard` | logged-in | 交易頁摘要（含 reference / risk-grade contexts） |
| GET | `/api/trading/live-price` | logged-in | 即時 reference 價格 / price health / price contexts / provider transport state |
| GET | `/api/trading/history/export.csv` | logged-in | 匯出交易歷史 |
| GET | `/api/trading/btc-signal` | logged-in | BTC_trade signal |
| GET | `/api/trading/workflow-templates` | logged-in | workflow 模板 |
| POST | `/api/trading/workflow-templates/custom` | logged-in | 儲存自訂模板（寫入 `runtime/workflows/custom/<username>/`） |
| GET | `/api/trading/reference-prices` | logged-in | K 線 / 指標資料 / reference price context |
| GET/POST | `/api/trading/orders` | logged-in | 訂單列表 / 下現貨單 |
| POST | `/api/trading/orders/<order_uuid>/cancel` | logged-in | 取消現貨單 |
| GET/POST | `/api/trading/bots` | logged-in | DCA / workflow bot 列表 / 建立 |
| POST | `/api/trading/bots/backtest` | logged-in | 回測 |
| GET/PUT/DELETE | `/api/trading/bots/<bot_uuid>` | logged-in | 單一 bot |
| POST | `/api/trading/bots/<bot_uuid>/increase-runs` | logged-in | 增加 runs |
| POST | `/api/trading/bots/<bot_uuid>/budget` | logged-in | 調整 workflow bot 預算；不得低於已凍結、未結費用與風控保留 |
| POST | `/api/trading/bots/scan` | logged-in | 手動掃 bot |
| POST | `/api/trading/grid/preview` | logged-in | Grid 預估毛利/淨利/風險燈號 |
| GET/POST | `/api/trading/grid-bots` | logged-in | Grid bot 列表 / 建立 |
| POST | `/api/trading/grid-bots/<bot_uuid>/toggle` | logged-in | 啟停 grid bot |
| DELETE | `/api/trading/grid-bots/<bot_uuid>` | logged-in | 刪除 grid bot |
| POST | `/api/trading/grid-bots/scan` | logged-in | 手動掃 grid |
| POST | `/api/trading/margin/open` | logged-in | 開借貸單 |
| POST | `/api/trading/margin/<position_uuid>/close` | logged-in | 平倉 |
| POST | `/api/trading/margin/<position_uuid>/collateral` | logged-in | 補保證金 |
| POST | `/api/trading/margin/<position_uuid>/collateral/withdraw` | logged-in | 取回可釋放保證金；後端重新計算維持率 |
| GET | `/api/admin/trading/report` | root | 交易報表快照；無快照時回 503 |
| GET | `/api/root/trading/sitewide/pools` | root | root 交易所基金 / 借貸流動性唯讀快照；無快照時回 503 |
| GET | `/api/root/trading/sitewide/user-positions` | root | root 積分錢包全用戶倉位唯讀快照；無快照時回 503 |
| GET/PUT | `/api/root/trading/settings` | root | root 交易設定 |
| GET | `/api/admin/trading/markets` | root | 交易市場 registry 列表 |
| POST | `/api/admin/trading/markets` | root | 新增交易市場 |
| PUT | `/api/admin/trading/markets/<market_id>` | root | 編輯交易市場 |
| DELETE | `/api/admin/trading/markets/<market_id>/disable` | root | 停用交易市場（保留歷史） |
| POST | `/api/admin/trading/markets/<market_id>/probe` | root | probe 市場 provider 狀態 |
| GET | `/api/admin/trading/markets/<market_id>/providers` | root | 讀 market provider mappings |
| POST | `/api/admin/trading/markets/<market_id>/providers` | root | 新增 provider mapping |
| PUT | `/api/admin/trading/markets/<market_id>/providers/<mapping_id>` | root | 編輯 provider mapping |
| DELETE | `/api/admin/trading/markets/<market_id>/providers/<mapping_id>` | root | 停用 provider mapping |

`GET /api/admin/trading/markets` 現在也會回：
- `registry_source`：`catalog_seed` 或 `custom`
- `seed_version`
- `catalog_seed_version`
- `seed_sync_status`：`current` / `drifted` / `custom` / `orphaned_seed`
- `seed_sync_reasons`

這些欄位的目的不是自動覆蓋 DB，而是讓 root 看見：某個 seeded 市場是否已偏離
目前 code catalog。執行期仍以 DB registry 為 source of truth。
| GET | `/api/root/trading/price-fusion-status` | root | 融合價格診斷 / provider transport state |
| GET | `/api/root/trading/bot-audit/dashboard` | root | bot audit dashboard |
| POST | `/api/root/trading/bot-audit/run` | root | 立即跑 bot audit |
| GET | `/api/root/trading/btc-trade/check` | root | 檢查 BTC_trade |
| POST | `/api/root/trading/btc-trade/setup` | root | clone / update / install |
| POST | `/api/root/trading/btc-trade/start` | root | 一鍵啟動預測 |
| GET | `/api/root/trading/btc-trade/start-status` | root | 背景 job 狀態 |
| GET | `/api/root/trading/background/status` | root | 交易背景 worker 狀態、job、lease、近期 run |
| POST | `/api/root/trading/background/run-once` | root | enqueue 指定 job 單次執行並立即回 202，需 `RUN_TRADING_JOB_ONCE` 確認 |
| POST | `/api/root/trading/background/pause` | root | 暫停全部或指定交易背景 job |
| POST | `/api/root/trading/background/resume` | root | 恢復全部或指定交易背景 job |
| POST | `/api/root/trading/liquidations/scan` | root | 手動掃清算 |
| POST | `/api/root/trading/orders/match` | root | 手動撮合 |
| GET/PUT | `/api/root/trading/markets/<symbol>` | root | root 市場設定 / 手動價 |
| POST | `/api/root/trading/reserve/allocate` | root | QA / 維運用交易所流動性配置入口；正式撥補仍應走治理與官方錢包規則 |
| POST | `/api/root/trading/simulated-balance/reset` | root | 重置 root 模擬資金 |
| GET | `/api/root/trading/contracts` | root | root 衍生品模擬列表；路徑名稱保留相容性 |
| POST | `/api/root/trading/contracts/<position_uuid>/close` | root | root 衍生品模擬平倉；路徑名稱保留相容性 |
| GET | `/api/root/trading/verify` | root | 讀 latest trading verify snapshot；缺 snapshot 時回 `202 + job_id` 啟動 async verify |
| POST | `/api/root/trading/verify/jobs` | root | 啟動 async trading verify job |
| GET | `/api/root/trading/verify/latest` | root | 讀 latest trading verify snapshot |

### ComfyUI

來源：`routes/comfyui.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/comfyui/status` | logged-in | ComfyUI 狀態 |
| POST | `/api/comfyui/start` | logged-in | 啟動本地 ComfyUI |
| GET | `/api/comfyui/models` | logged-in | 模型 / LoRA / embedding / VAE / generation mode / ControlNet / upscale model 列表 |
| POST | `/api/comfyui/billing-quote` | logged-in | 扣點預估 |
| POST | `/api/comfyui/generate` | logged-in | 建立背景產圖 job，立即回傳 `job_id` |
| GET | `/api/comfyui/jobs/<job_id>` | logged-in | job 狀態、進度與完成結果；後端無回應時會標記 `backend_unresponsive` |
| GET | `/api/comfyui/history` | logged-in | 取得近期生成歷史 |
| POST | `/api/comfyui/history/<history_id>/rerun` | logged-in | 依歷史設定重跑 |
| GET | `/api/comfyui/workflows` | logged-in | 取得可讀 workflow preset 列表 |
| POST | `/api/comfyui/workflows/import` | logged-in | 匯入 workflow JSON 為新 preset |
| POST | `/api/comfyui/workflows/export-current` | logged-in | 把目前表單匯出為 sanitized workflow JSON |
| GET | `/api/comfyui/workflows/<preset_id>` | logged-in | 讀取單一 workflow preset |
| PUT | `/api/comfyui/workflows/<preset_id>` | logged-in | 更新自己可編輯的 workflow preset |
| DELETE | `/api/comfyui/workflows/<preset_id>` | logged-in | 刪除自己可編輯的 workflow preset |
| POST | `/api/comfyui/workflows/<preset_id>/run` | logged-in | 執行 workflow preset |
| POST | `/api/comfyui/workflows/<preset_id>/export` | logged-in | 匯出已保存的 workflow preset JSON |
| POST | `/api/comfyui/image-preview` | logged-in | 預覽已儲存來源圖 / 遮罩圖 / 控制圖 |
| GET/POST | `/api/comfyui/diffusers/inspect` | logged-in | 檢查 Hugging Face Diffusers repo；GET 是安全讀取，不需 CSRF |
| POST | `/api/comfyui/interrupt` | logged-in | 中斷生成 |
| POST | `/api/comfyui/save` | logged-in | 儲存結果 |
| POST | `/api/comfyui/discard` | logged-in | 丟棄結果 |
| POST | `/api/comfyui/share` | logged-in | 分享結果 |
| POST | `/api/root/comfyui/test-connection` | root | 測試連線 |
| POST | `/api/root/comfyui/stop` | root | 停止本地 ComfyUI |
| POST | `/api/admin/comfyui/workflows/<preset_id>/publish-official` | root | 發布官方 workflow preset |
| POST | `/api/root/comfyui/civitai/search` | root | 用關鍵字 / base model / 類型 / NSFW 篩選搜尋 Civitai 模型 |
| POST | `/api/root/comfyui/civitai/inspect` | root | 讀 Civitai metadata |
| POST | `/api/root/comfyui/civitai/download` | root | 下載模型 |
| POST | `/api/root/comfyui/model-upload` | root | 直接上傳模型檔到本站設定的 ComfyUI models 目錄 |
| GET | `/api/root/comfyui/download-jobs/<job_id>` | root | 下載 job 狀態 |

ComfyUI 模型匯入補充：

- `/api/root/comfyui/civitai/download` 與 `/api/root/comfyui/model-upload` 都支援可選的
  `relative_dir`，代表 `ComfyUI/models/` 底下的相對路徑。
- 若 `relative_dir` 留空，後端會依模型類型自動落到預設資料夾，例如
  `checkpoints/`、`loras/`、`controlnet/`、`upscale_models/`。
- `relative_dir` 不可使用 absolute path、`..` 或跳出 `ComfyUI/models/` 的路徑。

Workflow preset 補充：

- 匯入/更新 workflow JSON 會先驗證：
  - JSON 格式正確
  - 不含 absolute path
  - 不含 `shell / exec / script / command` 類節點
  - 不含外部 URL
- 缺少模型 / LoRA / ControlNet / workflow node 時，執行 preset 會明確回 `409` 與依賴錯誤，不會靜默 fallback。
- `private` preset 只能擁有者查看/更新/刪除；`official` preset 只能由 root 發布。

### Security Center / Server Mode / Snapshots / Root Ops

來源：`routes/system_admin.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| GET | `/api/admin/health` | manager | 健康度 |
| GET | `/api/admin/environment` | manager | 環境資訊 |
| GET | `/api/admin/health/readiness` | manager | readiness |
| GET | `/api/admin/health/anomaly` | manager | anomaly |
| GET | `/api/admin/health/audit-chain` | manager | audit chain 健康 |
| GET | `/api/admin/health/db-integrity` | manager | DB integrity |
| GET | `/api/admin/security-center` | manager | 安全中心摘要 |
| GET | `/api/admin/server-output` | manager | server output |
| GET/PUT | `/api/admin/settings` | manager | 系統設定 |
| GET/PUT | `/api/admin/features` | manager | feature flags |
| GET/PUT | `/api/admin/access-controls` | manager | ACL / whitelist / lockdown |
| POST | `/api/admin/access-controls/maintenance-bypass-token` | manager | 維護繞過 token |
| POST | `/api/admin/access-controls/internal-test-token` | manager | 產生綁定單一帳號的 internal-test login token |
| GET/POST | `/api/admin/snapshots` | root | 列快照 / 建快照 |
| GET/POST | `/api/admin/snapshots/daily` | root | 日常快照設定 |
| POST | `/api/admin/system-reset` | manager | 系統重置 |
| GET/DELETE | `/api/admin/snapshots/<snapshot_id>` | root | 單一快照 |
| GET | `/api/admin/snapshots/<snapshot_id>/download` | root | 下載快照 |
| POST | `/api/admin/snapshots/upload-restore` | root | 上傳快照恢復 |
| POST | `/api/admin/snapshots/<snapshot_id>/restore` | root | 恢復快照 |
| GET/POST | `/api/admin/server-mode` | manager/root | 讀 mode / 切相容入口 |
| POST | `/api/admin/server-mode/exit-superweak` | manager | 離開 superweak |
| GET | `/api/root/server-mode` | root | root mode 狀態 |
| POST | `/api/root/server-mode/checkpoint` | root | 建 checkpoint |
| POST | `/api/root/server-mode/restore-check` | root | restore 前檢查 |
| POST | `/api/root/server-mode/switch` | root | 切 mode |
| GET | `/api/root/server-mode/requirements` | root | mode requirement |
| GET | `/api/root/server-mode/logs` | root | mode logs |
| GET | `/api/server-mode/logs/verify` | root | verify logs |
| GET | `/api/root/integrity/status` | root | integrity 狀態 |
| POST | `/api/root/integrity/rescan` | root | 重掃 integrity |
| GET | `/api/root/integrity/findings` | root | findings |
| GET | `/api/root/integrity/findings/<finding_id>` | root | 單一 finding |
| POST | `/api/root/integrity/findings/bulk-review` | root | 批次處理 findings |
| POST | `/api/root/integrity/findings/<finding_id>/approve` | root | 核准 finding |
| POST | `/api/root/integrity/findings/<finding_id>/reject` | root | 拒絕 finding |
| POST | `/api/root/integrity/findings/<finding_id>/ignore` | root | 忽略 finding |
| GET | `/api/root/integrity/report` | root | integrity 報告 |
| POST | `/api/admin/integrity/repair` | manager | 相容 repair 入口 |
| POST | `/api/admin/restart` | manager | 重啟站台 |
| GET | `/api/admin/platform-stats` | manager | 平台統計 |
| GET/POST | `/api/root/security-tests` | root | security jobs 列表 |
| GET | `/api/root/security-tests/<job_id>` | root | 單一 security job |
| POST | `/api/root/security-tests/pentest` | root | 啟動 pentest |
| POST | `/api/root/security-tests/privilege` | root | 啟動 privilege / permission-abuse test |
| POST | `/api/root/security-tests/functional` | root | 啟動 functional test |
| POST | `/api/root/security-tests/stress` | root | 啟動 stress test |
| GET/PUT | `/api/admin/security-center/thresholds` | manager | security thresholds |
| GET/PUT | `/api/admin/security-center/controls` | manager | security controls |
| GET | `/api/root/launch-check/doc?path=docs/...` | root | 站內檢視 launch-check playbook / tests 文件 |
| GET | `/api/root/server-update/status` | root | 更新狀態 |
| POST | `/api/root/server-update/preview` | root | 更新預覽 |
| POST | `/api/root/server-update/apply` | root | 套用更新 |
| GET | `/api/admin/security-center/profiles` | manager | 安全 profile |
| POST | `/api/root/production-report/upload` | root | 上傳可驗證的 production report（需 raw_report + sha256 hash + signature） |
| GET | `/api/root/production-report/status` | root | production report 狀態 |
| POST | `/api/root/production/enter` | root | 進 production |
| POST | `/api/root/tester-token/create` | root | 建 tester token |
| POST | `/api/root/tester-token/revoke` | root | 撤 tester token |
| GET | `/api/root/tester-token/list` | root | tester token 列表 |
| GET | `/api/tester/shadow-state` | tester | shadow state |
| GET | `/api/tester/shadow-role` | tester | 讀取目前 shadow role |
| POST | `/api/tester/shadow-role` | tester | 修改自己的 shadow role |
| GET | `/api/tester/shadow-wallet` | tester | 讀取目前 shadow wallet |
| POST | `/api/tester/shadow-wallet` | tester | 修改自己的 shadow wallet |
| POST | `/api/root/incident/enter` | root | 進 incident lockdown |
| GET | `/api/root/incident/status` | root | incident 狀態 |
| POST | `/api/root/incident/resolve` | root | 解除 incident |

- `POST /api/admin/access-controls/internal-test-token` 不再是 shared singleton login token：root 產生時必須指定 `target_user_id` 或 `target_username`，之後只有該帳號可在 `internal_test` mode 的 `/api/login` 使用這顆 token。
- `POST /api/root/production-report/upload` 現在要求：
  - `raw_report`
  - `report_hash = sha256(canonical_json(raw_report))`
  - `signature = hmac_sha256:<hex>`
  - `key_version`
  伺服器會重算 hash 並驗簽；未通過者不會計入 production gate。
- `/api/admin/security-center` 會回 `resource_usage`，供系統資源看板顯示
  CPU、GPU、VRAM、RAM 半弧形 gauges；後端對資源採樣有短暫快取與鎖，避免頻繁刷新重複啟動 GPU probe。

### Bug Reports

來源：`routes/bug_reports.py`

| Method | Path | 角色 | 用途 |
|---|---|---|---|
| POST | `/api/bug-reports` | logged-in | 提交 bug |
| GET | `/api/admin/bug-reports` | manager | bug report 列表 |
| POST | `/api/admin/bug-reports/<report_id>/review` | manager | 審核 bug report |

## 失敗情境與提示

- `200 OK` 不代表輸入一定原樣存入。若你在驗證設定、ACL、交易或風控，請同步查回讀 API 或 DB evidence。
- `docs/AGENTS/research/BLOCKCHAIN/*` 有些是已拍板但未實作規格，不可直接當成現行 API。
- `live-price` 不是純 read-only，它會同步刷新後端快取價格狀態，請不要把它當成完全無副作用的 health probe。
- `live-price` 目前回傳 canonical `price_type`、`source`、`confidence`、`stale`、`degraded`、`provider_count`，並附 `reference_price_context` / `risk_grade_price_context`；UI 展示價與風控價不可再混用。
- `live-price` 與 root `price-fusion-status` 現在也回傳 canonical `connected`、`fallback`、`last_update_at`、`exclusion_reason` 與完整 `transport_state`。Binance / OKX / Coinbase / Kraken 的 websocket 只是 provider input，不可直接把單一 WS provider 當成風控價格。
- `reference-prices` 明確屬於 `reference price` 類型，只能作為展示 / K 線 / 一般估值參考，不可直接拿來推導保證金、強平或 bot 風控。
- trading market registry 是 canonical 市場開關來源：market 被 disable 後不可再新下單，但既有歷史 / 持倉 / 報表仍必須可查；provider mapping 未通過 probe 時，不可啟用 `risk-grade` 用途。
- trading market registry 的 `seed_version` / `seed_sync_status` 只做 seed drift 可視化，不會默默把 root 已調整過的市場定義蓋回 code catalog。

## 測試方式

- 用 [CLI_ADMIN_PLAYBOOK.md](CLI_ADMIN_PLAYBOOK.md) 的 `curl` 範例逐條驗證
- 搭配 [11_QA_TESTING.md](11_QA_TESTING.md) 跑 functional smoke / pentest / trading validation
- 若改到交易 / 權限 / settings，至少補回歸：

```bash
python3 scripts/prepush/pre_push_checks.py --ci
scripts/testing/pytest_in_tmp.sh -q tests
```

## 相關文件連結

- [CLI_ADMIN_PLAYBOOK.md](CLI_ADMIN_PLAYBOOK.md)
- [For_developer.md](For_developer.md)
- [TRADING.md](trading/TRADING.md)
- [TRADING_BOT_AUDIT.md](trading/TRADING_BOT_AUDIT.md)
- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [SECURITY.md](SECURITY.md)
