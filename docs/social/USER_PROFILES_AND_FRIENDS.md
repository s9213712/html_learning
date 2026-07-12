# User Profiles and Friends

一句話說明：新增個人主頁與站內好友系統，讓使用者能從公開內容進入對方主頁、申請好友，並在成為好友後啟用私訊與遊戲邀請等互動。

## 狀態

第一階段已落地。現有系統已具備 `user_profiles`、唯一隨機 `friend_code`、`user_friends` 共用關係表、全站 `/api/friends/*` API，以及主側欄「個人面板」入口。`/api/chat/friends` 仍保留為聊天頁相容入口，但好友關係的主要管理位置是個人面板與全站好友 API。

仍待後續完成的部分：留言、貼文、影音、排行榜、遊戲紀錄與通知中的所有使用者名稱 / 頭像尚需逐步接到公開主頁。PM / private group、遊戲邀請與直接 strict-E2EE 檔案金鑰分享均已由後端檢查好友關係；只有 PM / private chat context 有明確的 root / manager 管理例外。

## 設計目的

- 使用者不應只能從聊天頁管理好友；任何顯示使用者身分的位置都應可通往個人主頁。
- PM、遊戲邀請與未來好友互動必須由後端檢查好友關係，不能只靠前端隱藏按鈕。
- root / manager 需要維持站務查看能力，但仍可作為一般好友系統的一部分出現在好友列表。

## 導航位置

- 主側欄順序：聊天 → 個人面板 → 公告 → 討論區 → 雲端硬碟。
- 左下角使用者卡片、頭像與暱稱可點進自己的個人面板。
- 右上角「修改資料」改為進入個人面板的「編輯資料」分頁。
- 好友管理放在個人面板內，不放到 root 的帳號管理。
- 聊天頁可以保留好友 / 私訊快捷入口，但不能成為好友系統唯一入口。
- root / manager 帳號管理頁未來可查看使用者主頁、好友狀態、申請紀錄與 audit；用途是治理與稽核，不是一般好友管理入口。

個人面板分頁：

- 我的主頁。
- 編輯資料。
- 好友。

## 個人主頁

每位使用者都有一個個人主頁。其他使用者可從以下入口進入：

1. 留言者名稱或頭像。
2. 貼文作者名稱或頭像。
3. 影音內容擁有者名稱或頭像。
4. 排行榜、遊戲紀錄、互動通知、聊天室、審核紀錄等顯示使用者資訊的位置。

個人主頁至少顯示：

- 暱稱。
- 頭像。
- 個人簡介。
- 好友狀態。
- 加入好友按鈕或目前申請狀態。
- 可公開的貼文、留言、影音或遊戲紀錄。

若瀏覽者尚未與該使用者成為好友，主頁顯示 `加入好友`。若已送出或收到申請，主頁顯示目前狀態與可採取的動作。

## 加入好友流程

### 透過個人主頁申請

使用者進入其他人的個人主頁後，可按 `加入好友` 送出申請。被申請者可同意或拒絕；同意後雙方正式成為好友。

好友申請狀態：

- `none`：尚未申請。
- `pending_outgoing`：已送出申請，等待對方同意。
- `pending_incoming`：收到對方申請，等待自己同意。
- `accepted`：已成為好友。
- `rejected`：已拒絕。
- `blocked`：已封鎖。若封鎖功能尚未開放，資料模型仍需保留狀態以避免日後 migration。

若雙方同時互相申請，系統可自動轉為 `accepted`，避免出現兩筆互相等待的申請。

### 透過好友代碼直接加入

每位使用者有一組好友代碼。使用者輸入對方好友代碼後，系統直接建立好友關係，不需對方再次同意。

好友代碼要求：

- 全站唯一。
- 不容易被猜測。
- 由系統產生。
- 可重新產生。重新產生後舊代碼失效，以降低外流後被濫用的風險。
- 不應使用連續 id、username 或可逆資料。

好友代碼加入時若雙方已是好友，回應應明確提示已存在；若代碼錯誤，回應應提示查無使用者。

## 好友限定功能

只有雙方成為好友後，才可使用下列互動：

- 互相邀約遊戲。
- 傳送私人訊息 PM。
- 其他未來的好友互動功能。

未成為好友前：

- 不可邀請對方進入遊戲。
- 不可直接傳送 PM。
- 只能透過公開內容進入主頁並送出好友申請。

## root 與 manager 例外

root 與 manager 是特殊權限帳號：

- 可查看所有使用者主頁，不受好友關係限制。
- 可依站務權限使用管理或審核需要的查看能力。
- 可為管理目的私訊非好友用戶；這是 PM / private chat context 的明確例外，不應自動套用到一般雲端硬碟分享、遊戲邀請或其他私人物件分享。
- 仍可擁有好友，也可出現在一般好友列表。
- 在好友列表中若包含 root / manager，需固定排在最上方。

顯示時應提供清楚標記，例如：

- `官方`
- `管理員`
- `Root`
- `Manager`
- `系統管理者`

排序建議：

1. root / manager。
2. 一般好友。
3. 最近互動、名稱或加入時間。

## 權限矩陣

| 瀏覽者身分 | 是否好友 | 可查看主頁 | 可加入好友 | 可邀約遊戲 | 可 PM |
|---|---:|---|---|---|---|
| 一般使用者 | 否 | 可 | 可 | 不可 | 不可 |
| 一般使用者 | 是 | 可 | 不需顯示 | 可 | 可 |
| root / manager | 否 | 可查看全部 | 可 | 依站務權限設定 | 依站務權限設定 |
| root / manager | 是 | 可查看全部 | 不需顯示 | 可 | 可 |

## 資料模型

### `user_profiles`

用於存放個人主頁資料。

目前欄位：

- `user_id`
- `display_name`
- `bio`
- `signature`
- `location`
- `website`
- `friend_code`
- `friend_code_rotated_at`
- `profile_visibility`
- `appearance_json`
- `created_at`
- `updated_at`

`friend_code` 由伺服器使用不可預測的隨機來源產生，並有唯一索引。若使用者還沒有 profile，讀取主頁時會 lazy-create profile；重新產生好友代碼後，舊代碼立即失效。公開查看他人主頁時不回傳好友代碼，只有本人讀自己的個人面板才會看到。

### `user_friends`

現有系統已使用 `user_friends`。後續應優先沿用或安全遷移它，而不是新增一套重複的 `friendships` 表。

現有概念：

- `user_id`
- `friend_user_id`
- `status`
- `requested_by`
- `created_at`
- `updated_at`

要求：

- 查詢時視為雙向關係。
- 不建立兩筆互相重複的 accepted 關係。
- status 至少包含 `pending`、`accepted`、`rejected`、`blocked`。
- 有唯一約束，避免重複申請。
- 有 `CHECK (user_id <> friend_user_id)` 或等效防護，避免加自己。

若日後需要把申請與好友關係拆開，可新增 `friend_requests`，但必須提供 migration 與相容查詢層。

## API 需求

既有 chat friends API 相容保留；全站 API 已落地如下：

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/users/<user_id>/profile` | 查看個人主頁 |
| GET | `/api/users/me/profile` | 查看自己的個人面板資料與好友代碼 |
| PUT | `/api/users/me/profile` | 更新自己的顯示名稱、簡介、簽名、所在地、網站與公開範圍 |
| POST | `/api/users/me/friend-code/rotate` | 重新產生好友代碼 |
| GET | `/api/users/target-options?context=<context>` | 依指定功能 context 回傳可選對象；一般使用者只回合法好友，root / manager 在站務 context 可看全站 |
| GET | `/api/friends` | 好友列表，root / manager 固定置頂 |
| GET | `/api/friends/requests` | 查看收到與送出的好友申請 |
| POST | `/api/friends/request` | 透過個人主頁送出好友申請 |
| POST | `/api/friends/requests/<request_id>/accept` | 同意好友申請 |
| POST | `/api/friends/requests/<request_id>/reject` | 拒絕好友申請 |
| POST | `/api/friends/add-by-code` | 透過好友代碼直接加入好友 |
| DELETE | `/api/friends/<user_id>` | 移除好友 |

所有 mutation API 必須走 CSRF、登入驗證、角色 / member-level 檢查與 audit 記錄。

## 後端權限要求

- 不可加自己為好友。
- 不可重複送出好友申請。
- 已是好友時不可再次申請。
- 雙方同時申請時可自動轉為好友。
- 好友代碼錯誤時回傳明確錯誤，不洩漏敏感資料。
- 被封鎖者不可申請好友、PM 或邀約遊戲。
- root / manager 可查看全部使用者，但好友列表與一般好友系統相容。
- PM / private group 必須在後端檢查 `accepted` 好友關係或 root / manager 管理例外權限；目前已落地。
- 遊戲邀請與直接 strict-E2EE 檔案金鑰分享均在 route / service 邊界檢查 `accepted` 好友關係，非好友直接呼叫 API 也會被拒絕。
- 前端按鈕隱藏只能提升體驗，不能作為安全邊界。

## UI 要求

- 使用者名稱與頭像在留言、貼文、影音、排行榜、遊戲紀錄與通知中都應是可點擊主頁入口。
- 個人主頁要清楚顯示好友狀態，不要讓使用者猜測申請是否成功送出。
- 好友列表中 root / manager 固定置頂，並顯示官方 / 管理者標記。
- 若 PM 或遊戲邀請因非好友被拒絕，UI 應提示先到個人主頁送出好友申請。
- 好友代碼輸入錯誤、已是好友、被封鎖、申請重複時都要有明確訊息，不可靜默失敗。

## QA Gate

必測：

1. 一般使用者可從留言、貼文、影音作者入口打開個人主頁。
2. 非好友可看公開主頁，但不可 PM 或邀請遊戲。
3. 非好友可送出好友申請。
4. 收到申請者可同意，雙方轉為好友。
5. 收到申請者可拒絕，狀態顯示正確。
6. 好友可 PM。
7. 好友可邀請遊戲。
8. 使用好友代碼可直接建立好友。
9. 好友代碼可重新產生，舊代碼失效。
10. 不可加自己為好友。
11. 不可重複申請。
12. 雙方同時申請時不產生重複關係。
13. 被封鎖者不可申請、PM 或邀請遊戲。
14. root / manager 可查看所有主頁。
15. root / manager 若在好友列表中固定置頂並有特殊標記。
16. PM / private group API 直接呼叫也會拒絕非好友，不只前端隱藏按鈕。
17. 遊戲邀請與直接 strict-E2EE 檔案金鑰分享都要持續通過非好友 403、好友成功與 service defense-in-depth 測試。
18. 所有好友 mutation 都有 CSRF、audit 與權限驗證。

## Phase Plan

### Phase 0：Docs and Compatibility

- 已完成：建立本文件。
- 已完成：標記現有 `/api/chat/friends` 為相容入口。
- 已完成：沿用 `user_friends` 作為全站好友表，不新增第二套關係表。

### Phase 1：Profiles and Friend Code

- 已完成：新增 / 升級 `user_profiles`。
- 已完成：產生唯一隨機 `friend_code`。
- 已完成：新增個人主頁讀取 API。
- 已完成：新增好友代碼重新產生 API。

### Phase 2：Friend Request Unification

- 已完成：新增 `/api/friends/*` API。
- 已完成：個人面板好友分頁讀取同一套 `user_friends` 關係。
- 待完成：將 chat friends UI/API 內部也改呼叫同一套 service，減少重複邏輯。
- 已完成：全站 API 支援申請狀態、雙向查詢、反向申請自動接受與好友代碼直加。

### Phase 3：Friend-Gated Interactions

- 已完成：PM / private group 後端檢查好友關係。
- 已完成：root / manager 可為管理目的 PM 非好友，且不影響一般使用者限制。
- 已完成：遊戲邀請後端檢查好友關係。
- 已完成：直接 strict-E2EE 檔案金鑰分享在 route 與 service 兩層檢查好友關係。
- 已完成：指定對象 context 集中處理；PM / private chat 保留管理例外，遊戲邀請與雲端硬碟私人物件分享不繼承該例外。

### Phase 4：Sitewide Profile Entry Points

- 待完成：留言、貼文、影音、排行榜、遊戲紀錄、通知全部接到個人主頁。
- 已完成：主側欄個人面板與手機版基本好友列表 UI。

## 完整需求摘要

新增個人主頁與好友系統。用戶可透過點擊留言、貼文、影音內容的擁有者連結進入該用戶主頁，並在主頁中送出好友申請，等待對方同意後成為好友。用戶也可以使用好友代碼直接加入好友，此方式不需對方同意。只有已成為好友的用戶之間，才可以互相邀約遊戲與傳送私人訊息 PM。root 與 manager 為特殊權限帳號，可查看所有用戶，不受好友關係限制；但仍可擁有好友，並且在好友列表中需固定顯示於最上方。
