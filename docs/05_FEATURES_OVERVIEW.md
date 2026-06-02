# 05 Features Overview

一句話說明：這份文件用部署者與管理者能快速掃描的方式，整理目前所有主要功能、依賴、限制、測試與深層參考入口。

## 設計目的

過去相同主題散在 `WEB.md`、`VIDEO_PLATFORM.md`、`TRADING.md`、
`For_developer.md`、各種安全與 QA 文件裡。這份文件把「功能是什麼、要搭什麼才能完整用、失敗時看哪裡、怎麼驗」集中到同一份。

## 功能矩陣

| 功能 | 主要對象 | 依賴 / 一起開較完整 | 深層文件 |
|---|---|---|---|
| 帳號 / 認證 / Session | 全用戶 | CSRF、權限、通知 | [04_USER_GUIDE.md](04_USER_GUIDE.md), [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [For_developer.md](For_developer.md) |
| 個人外觀 / 站點主題 | 全用戶 + root | root 全站預設、個人外觀覆寫開關 | [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md), [04_USER_GUIDE.md](04_USER_GUIDE.md), [WEB.md](WEB.md) |
| 個人主頁 / 好友系統 | 全用戶 + manager / root | chat friends、PM、遊戲邀請、notifications | [USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md), [WEB.md](WEB.md) |
| 社群 / Chat / 論壇 / 公告 | 一般站點 | reports / notifications / moderation | [WEB.md](WEB.md) |
| Cloud Drive / 相簿 | 全用戶 | attachments、albums、upload security | [WEB.md](WEB.md) |
| Video Platform | 內容站點 | Cloud Drive、PointsChain | [VIDEO_PLATFORM.md](video/VIDEO_PLATFORM.md), [VIDEO_STREAMING_ARCHITECTURE.md](video/VIDEO_STREAMING_ARCHITECTURE.md) |
| ComfyUI | AI 站點 | feature_comfyui、local/remote ComfyUI、Civitai 僅本地模式 | [WEB.md](WEB.md), [For_developer.md](For_developer.md) |
| Appeals / Notices / Governance | 有審核流程的站點 | reports、notifications、identity/member governance | [WEB.md](WEB.md) |
| Security Center / Server Mode | root | audit、integrity、snapshot/restore、health center | [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [SERVER_MODE_V2_PROFILE_MATRIX.md](server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md) |
| PointsChain | 經濟功能 | wallet、ledger、video tips、trading | [07_POINTSCHAIN.md](07_POINTSCHAIN.md) |
| Trading / Bots / Backtest | 交易站點 | PointsChain、economy、price feeds、chart indicators、QA scripts | [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md), [TRADING.md](trading/TRADING.md) |
| Games / Board AI | 遊戲站點 | CSRF、排行榜、三棋 AI benchmark、chess engine pipeline | [games/README.md](games/README.md), [games/references/BOARD_AI_BENCHMARK.md](games/references/BOARD_AI_BENCHMARK.md) |
| Snapshot / Restore / Reset | root / 運維 | server mode、audit、integrity、PointsChain | [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md) |

## 模組詳解

### 帳號 / 認證 / Session

- 一句話說明：提供登入、註冊、CSRF、防暴力登入、密碼變更與角色/會員等級邊界。
- 設計目的：讓所有後續模組都站在同一套可審計的認證與權限模型上。
- 使用方法：先部署、登入、修改 bootstrap 密碼，再按角色使用功能頁；若要啟用 Cloudflare Turnstile，先把 `註冊 CAPTCHA 模式` 切到 `turnstile`，此時設定頁才會顯示 `Turnstile site key`。root 若要快速切功能範圍，可在設定頁功能開關使用 `全開` 或 `最低維運` 套餐。
- 原理：後端以 session + CSRF + RBAC + member level 重新驗證每個高風險請求。
- 失敗情境與提示：`csrf_invalid`、預設密碼強制變更、登入失敗但不暴露使用者是否存在、權限不足 `403`、找不到 `Turnstile site key` 時先確認目前 CAPTCHA 模式是不是 `turnstile`。
- 測試方式：帳號 happy path、錯誤密碼、越權 API、session 失效、idle logout。
- 相關文件連結：[04_USER_GUIDE.md](04_USER_GUIDE.md), [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [For_developer.md](For_developer.md)

### 個人外觀 / 站點主題

- 一句話說明：root 控制全站預設外觀，而每位已登入用戶都可選擇是否覆寫成自己的字體、背景、面板、側邊欄與配色風格。
- 設計目的：讓部署者維持全站品牌與預設可讀性，同時不必把每位使用者都綁死在同一套視覺配置。
- 使用方法：root 到 `Security Center -> Settings -> Appearance` 設定全站預設，並決定是否開啟 `允許使用者覆寫個人外觀`；一般使用者到 `修改資料 -> 個人外觀` 儲存自己的主題。若要放棄個人覆寫，可直接按編輯視窗底部的 `恢復全站預設` 再儲存。
- 原理：前端會先套 root 的全域 site config，再覆蓋登入使用者的個人 appearance settings；後端只接受白名單欄位與有限選項，不信任任意 CSS 或自由字串。
- 失敗情境與提示：root 關閉個人外觀覆寫時，使用者編輯器會顯示停用提示並阻止儲存；若沒按儲存，只是暫時預覽；全站預設永遠不會被一般使用者改到。
- 測試方式：驗證 root 可改全站預設、一般使用者只能改自己的畫面；驗證預覽、儲存、恢復預設、關閉個人覆寫後的停用提示；驗證字體/背景/面板/側邊欄寬度在重新登入後仍正確套用。
- 相關文件連結：[03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md), [04_USER_GUIDE.md](04_USER_GUIDE.md), [11_QA_TESTING.md](11_QA_TESTING.md), [WEB.md](WEB.md)

### 個人主頁 / 好友系統

- 一句話說明：主側欄已有「個人面板」，使用者可管理個人主頁、公開資料、好友申請與隨機好友代碼。
- 設計目的：讓使用者不必只從聊天頁管理好友；指定對象的功能也要有同一套後端目標選擇與好友關係邊界。
- 使用方法：一般使用者可在個人面板送出 / 接受好友申請，也可輸入對方好友代碼直接建立好友。root / manager 可因站務查看全站使用者；若對方也是自己的好友，候選清單需置頂並標示管理者 / 好友狀態。
- 原理：`user_profiles` 保存公開資料與不可預測的唯一 `friend_code`；`user_friends` 是全站好友關係 source of truth；`/api/users/target-options` 依 context 回傳可指定對象。PM / private group 已走後端好友或 root / manager 例外檢查；遊戲邀請與直接 E2EE 檔案金鑰分享仍是待補後端 friend-gated enforcement 的缺口，不可在部署判讀時視為完成。
- 失敗情境與提示：不可加自己、不可重複申請、已是好友不可再次申請、好友代碼錯誤需提示查無使用者、被封鎖者不可申請 / PM / 邀請遊戲，所有拒絕都不可靜默失敗。
- 測試方式：個人面板、好友代碼顯示 / 複製 / 重新產生、申請 / 同意 / 拒絕、好友代碼直加、root / manager 置頂、一般使用者指定對象只看到好友、PM / private group API 後端拒絕非好友、尚未完成的遊戲邀請與直接 E2EE 分享缺口回歸追蹤。
- 相關文件連結：[USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md), [WEB.md](WEB.md), [API_REFERENCE.md](API_REFERENCE.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### 社群 / Chat / 論壇 / 公告

- 一句話說明：提供聊天室、站內信、討論區、公告與內容審核/檢舉流程。
- 設計目的：驗證權限、審核、通知與治理流程在同一站內的互動。
- 使用方法：使用者按 `建立聊天室` 後才展開建房欄位；可建立公開群、指定好友的 private group / PM，或加入允許匿名的一般群。官方聊天群對一般使用者固定匿名，root 顯示 `root`，管理員顯示編號管理員並標示官方；root / manager 可看原發言者。一般群是否允許匿名由建立者決定，加入時可選是否匿名；一對一 PM 不提供匿名。
- 原理：聊天、論壇、檢舉、通知彼此連動，並受角色、member level、好友關係與聊天室匿名設定限制。匿名時顯示名稱與頭像都要走匿名展示，root / manager 的治理檢視才可看原帳號。
- 失敗情境與提示：無權發文、非好友不可 PM / private group、板權限不足、檢舉/公告功能未開啟時會顯示關閉或權限訊息。
- 測試方式：逐角色測發文/回覆/檢舉/公告/管理工具；另測官方群匿名、一般群允許 / 不允許匿名、PM 不匿名、root / manager 原發言者檢視，以及 UI/API/DB 對帳。
- 相關文件連結：[04_USER_GUIDE.md](04_USER_GUIDE.md), [WEB.md](WEB.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### Cloud Drive / 相簿

- 一句話說明：提供上傳、分享、預覽、隱私模式、資料夾、垃圾桶與相簿。
- 設計目的：集中處理檔案、權限、加密模式與媒體功能的共同基礎。
- 使用方法：Cloud Drive 分成檔案管理與容量管理分頁；容量狀態用水位式視覺而不是電池隱喻。上傳可走 resumable/chunk upload；重新整理後任務中心會顯示未完成 session，使用者需重新選擇同一檔案才能繼續補 chunk。BT / direct link 下載也進任務中心，顯示速度、進度、可用度提示，並提供暫停 / 恢復 / 取消。分享連結可下載也可在瀏覽器預覽，複製連結後按鈕下方會顯示已完成複製；分享管理已移到「管理」並可編輯檔案、相簿、影音分享選項。相簿改成連續照片流，hover 可放大，點選進全頁檢視並用左右按鈕切換；相簿頁只保留分享入口，密碼、到期、最大存取次數都在分享管理調整。
- 原理：不同隱私模式決定 server 能否掃描 / 預覽 / 解密，並影響影片等上層模組。strict E2EE 仍以瀏覽器端解密為主；server_encrypted 新上傳一律使用 chunked server-side encryption，下載與媒體 inline content 逐段解密，避免主伺服器一次載入完整檔案；舊的單檔 Fernet 格式只保留讀取相容。
- 失敗情境與提示：配額不足、加密模式不支援某些預覽、權限不足、檔案被 quarantine、重整後未重選原始檔、分享連結缺少 E2EE fragment key；strict E2EE PDF 若內嵌檢視器失敗，畫面會保留新分頁開啟備援；E2EE 檔案則會先嘗試本次 session 最近成功的密碼，不會每次都強制重新輸入。
- 測試方式：一般上傳、分段上傳重整 / 續傳、預覽、分享管理編輯、指定好友分享、BT/direct link 併發與 pause/resume/cancel、刪除、恢復、相簿操作、E2EE / server_encrypted 邊界。
- 相關文件連結：[04_USER_GUIDE.md](04_USER_GUIDE.md), [WEB.md](WEB.md), [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)

### Video Platform

- 一句話說明：把 Cloud Drive 內的影片檔變成可觀看、評論、按讚、打賞的影音頁。
- 設計目的：重用既有 storage/permission/PointsChain，而不是再造第二套媒體系統。
- 使用方法：影音頁支援搜尋；發布影音表單按下發布按鈕後才展開，不常駐佔版面。先上傳影片到 Cloud Drive，再從影音頁發布；也可從「我的影音」按分享按鈕跳到分享管理繼續設定分享選項。不論是直接上傳影音，或從既有 Cloud Drive 影音檔發布，都可附上自訂封面圖。prepared HLS 影片在準備完成前不進一般影音列表；上傳者會看到處理提示，完成後由通知告知。Safari 會走原生 HLS，桌機 Chrome / Firefox / Edge 會用同源 `hls.js`；影音頁不再把 direct 當播放模式，HLS 不可用時改走受併發限制的即時轉封裝，仍不可用才顯示等待 / 失敗狀態。多音軌 / 多字幕影片在 prepared HLS 完成後會顯示音軌、字幕、畫質選單，分享影音與 Cloud Drive 分享預覽也會套用同一套授權。Cloud Drive 預覽可對瀏覽器原生支援的 MP4/WebM 走 direct；影音發布預設走 HLS，冷門影片依 root 門檻裁到 480p 手機保底或清理多餘衍生檔。`持連結可看` 的 strict E2EE 影音則會在同頁顯示分享管理面板，讓擁有者查看分享狀態、剩餘觀看次數、是否有第二層分享密碼、到期日與重新產生 / 撤銷入口；若已建立 `E2EE Streaming v2` manifest，播放端會走密文分段下載與瀏覽器端分段解密，否則退回舊版完整解密。
- 原理：影片 metadata 與互動是 presentation layer，實際檔案仍由 Cloud Drive 提供。
- 失敗情境與提示：strict E2EE 檔案不可作為一般 server-side/HLS 影音發布；若要發布成 `持連結可看` 的 E2EE 影音，擁有者需在瀏覽器端輸入一次原始 E2EE 密碼建立分享授權；若沒有 `E2EE Streaming v2` manifest、裝置不支援 MediaSource / Worker / WebCrypto、或密文 chunk 驗證失敗，系統會明確退回舊版完整解密播放，不會假裝成功；分享頁現在會明確顯示「讀取分享授權 / 下載加密影音 / 瀏覽器端解密」階段，避免大檔案只看起來像卡住；若完整分享連結 fragment 遺失，伺服器無法復原，只能重新產生分享；server_encrypted 若遇舊 key 不可解會回 `decrypt_unavailable`。
- 測試方式：發布面板展開、搜尋、Cloud Drive 影片帶入、HLS 準備完成前不顯示、完成通知、播放、private/unlisted、分享管理跳轉、評論、打賞、權限與解密失敗情境；Premium HLS worker 容量用 `scripts/testing/hls_premium_sizing_probe.py` 驗證 queue、CPU/RSS、衍生檔倍率與 profile / original rendition storage policy。
- 相關文件連結：[VIDEO_PLATFORM.md](video/VIDEO_PLATFORM.md), [VIDEO_STREAMING_ARCHITECTURE.md](video/VIDEO_STREAMING_ARCHITECTURE.md), [VIDEO_STREAMING_SERVICE_TIERS.md](video/VIDEO_STREAMING_SERVICE_TIERS.md), [07_POINTSCHAIN.md](07_POINTSCHAIN.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### ComfyUI

- 一句話說明：提供本地或遠端 ComfyUI 生圖、LoRA 權重與 trigger words、自動 Embedding 快速插入、VAE 選擇、`img2img / inpaint / outpaint / upscale / ControlNet`、歷史重跑、workflow preset 工作台、預覽儲存與 root 專用 Civitai 下載工具。
- 設計目的：把 AI 圖像生成功能整合到既有帳號、Cloud Drive、論壇與權限模型中。
- 使用方法：選模型、VAE、提示詞與 LoRA；頁面標題會明確顯示目前是本地模式還是雲端 / 遠端模式；需要時直接點 Embedding 快捷按鈕把 token 插進正向提示詞，名稱帶 `neg` / `negative` 的 Embedding 會直接進負面提示詞，再按一次可移除；若該 LoRA 是經由 root 的 Civitai 面板下載且有官方 trigger words，加入 LoRA 時會自動把缺少的 trigger words 補進正向提示詞，移除該 LoRA 時也會一併移掉不再需要的 trigger words；目前只允許 `SDXL / Pony / Illustrious / Noob` base model 的 LoRA，`SD1.5 / Flux / 未知 metadata` 會直接顯示不可用；若下拉選 `不使用 LoRA` 再按 `加入`，會清空目前已選 LoRA；進階模式可切換 `img2img / inpaint / outpaint / upscale` 並上傳來源圖，ControlNet 可再上傳控制圖、選類型、強度、start/end 與預處理器；歷史清單則可把來源圖、遮罩圖、ControlNet 設定與提示詞一鍵套回重跑；`ComfyUI Workflow 工作台` 可把目前表單匯出成 workflow JSON，再匯入為 private/public preset、日後一鍵套回/重跑，root 還可把自己的 preset 發布為官方 preset；root 在本地模式下可用底部折疊區先搜尋 Civitai（關鍵字 / base model / 類型 / Safe/NSFW），再讀取版本 / trigger words 並下載 checkpoint、LoRA、embedding、ControlNet、VAE；若切到遠端模式，設定頁與主畫面都不會再顯示本地模型下載 / Civitai API Key。
- 原理：生圖走 async job；主 Flask request 不同步等待模型載入、推論或 interrupt。前端 Embedding 按鈕會插入 `<embeddings:名稱>`，送出前由後端轉成 ComfyUI 可用 syntax；LoRA 的 trigger words 與 `base_model` 會透過下載時保存的 sidecar metadata 回填，前端依此禁用不支援的 LoRA，後端也會再次拒絕直接請求；移除 LoRA 時會比對其他已選 LoRA 是否仍需要同一組 trigger words，避免把共用詞全部刪掉；ControlNet 依類型動態檢查對應 node / model / preprocessor 能力，缺少時會在送單前直接拒絕；workflow 匯入/匯出會先經過 JSON 安全驗證，阻擋 absolute path、shell/exec 節點、外部 URL 與可疑敏感欄位；歷史重跑與 workflow preset run 都會保存完整 seed / CFG / steps / LoRA / ControlNet 上下文；遠端模式只負責生圖，本地模式才有啟停與模型下載管理；ComfyUI 長工作進行中會暫停閒置登出倒數，避免本地啟動、生圖或模型下載做到一半被自動登出。小 VRAM 主機應優先用遠端 ComfyUI 或把常用模型放在 Linux native storage，避免 WSL 掛載碟與 VRAM offload 拖慢主站。
- 失敗情境與提示：後端 unreachable、job 中斷、模型不存在、remote 模式看不到 Civitai 下載工具、手動放進 `models/loras` 但沒有 metadata 的 LoRA 會被視為未知 base model 而不可用、ControlNet 對應模型 / node 缺失、控制圖格式錯誤、Control strength 超出範圍、workflow 缺 node、workflow JSON 格式錯誤、workflow 含 absolute path / shell/exec / 外部 URL、缺少模型 / LoRA / ControlNet 依賴、private preset 越權讀取；若頁面回 `此功能目前已由 root 關閉`，新版錯誤會盡量直接指出被擋的是哪個父功能。
- 測試方式：status、model list、LoRA metadata / trigger words、Embedding/VAE 列表、`img2img / inpaint / outpaint / upscale / ControlNet`、history rerun、workflow import/export、preset CRUD、official preset publish、custom VAE workflow、async progress、save/share/discard、Civitai trigger words、權限與模式切換。
- 相關文件連結：[WEB.md](WEB.md), [For_developer.md](For_developer.md), [COMFYUI_PERFORMANCE_HARDENING.md](comfyui/COMFYUI_PERFORMANCE_HARDENING.md), [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)

### Appeals / Notices / Governance

- 一句話說明：提供違規通知、治理通知、申訴與審核流。
- 設計目的：讓權限、處分與可回溯治理流程可被記錄與追蹤。
- 使用方法：使用者查看通知、提申訴；管理者審核、批准、拒絕。
- 原理：通知、申訴、治理動作會連動權限與審計記錄；通知列含 `severity`、
  `audience`、來源模組與 `dismissed_at`，使用者隱藏通知後會從預設清單與未讀數排除。
- 失敗情境與提示：功能關閉、身分不足、通知不存在、通知已處理或已隱藏；跨使用者 /
  跨 audience 讀取會被拒絕。
- 測試方式：多角色逐步測申訴、通知、審核、審計記錄。
- 相關文件連結：[WEB.md](WEB.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### Platform Center

- 一句話說明：集中顯示背景任務、分享連結、通知入口與交易資產總覽。
- 設計目的：讓使用者與管理者看得到長任務進度、stage、錯誤、分享狀態與經濟總覽，
  避免按了沒反應或 API 失敗靜默消失。
- 使用方法：Job Center 可取消 / 重試任務；Share Link Management 可查看 file /
  album / video 分享、到期、次數、密碼狀態、存取紀錄、撤銷與編輯分享選項；Trading Asset Overview
  會顯示可用點數、鎖定點數、現貨市值、借貸 / 融資倉位權益、累積利息與低信心價格數。
- 原理：一般使用者只看自己的 `/api/jobs` 與 `/api/shares`；manager / root 可讀
  `/api/admin/jobs` 與 `all=1` 分享列表。分段上傳、BT/direct link、HLS 準備、E2EE streaming v2、ComfyUI 與交易背景 enqueue 都應進任務中心或對應背景狀態；任務中心只有在使用者進入頁面時 lazy-load 並輪詢實際進度，不靠全站常駐前端輪詢來維持工作生命週期。交易總覽只做顯示，不取代交易結算；價格信心是風險提示，不再阻擋積分交易。
- 失敗情境與提示：ComfyUI 或外部工作失敗會顯示 `stage`、`stage_detail` 與錯誤訊息；
  Trading Asset Overview API 失敗會在經濟頁顯示錯誤；分享撤銷或到期後，分享頁應顯示
  結束訊息，底層 API 則拒絕繼續取資料。
- 測試方式：`python3 scripts/testing/playwright_platform_health_check.py`，再檢查
  產出的 JSON/Markdown 報告。

### Security Center / Server Mode

- 一句話說明：提供 root 的站點安全儀表板、系統資源看板、server mode、integrity、審計與高風險操作控制面。
- 設計目的：把營運級保護做成可驗證、可切換、可回滾的控制面。
- 使用方法：root 在 Security Center 看健康度、CPU/GPU/VRAM/RAM 半弧形資源看板、套 profile、切 mode、做 integrity / snapshot / restore。
- 原理：mode、checkpoint、audit chain、integrity findings、protected logs 各自維持邊界。
- 失敗情境與提示：缺 confirmation string、production gate 未滿足、incident lockdown、生效功能組未完整啟用；資源看板採短暫快取與鎖避免高頻刷新反覆啟動 GPU probe。
- 測試方式：mode switch、superweak rollback、incident lockdown、log verify、permission pentest、資源看板刷新與小螢幕顯示。
- 相關文件連結：[06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md), [SERVER_MODE_V2_PROFILE_MATRIX.md](server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md)

### PointsChain

- 一句話說明：站內點數、經濟帳本、封塊與驗證系統，是交易與打賞的可信來源。
- 設計目的：讓所有重要金額變動都走同一條可驗證鏈，而不是直接改 wallet balance。
- 使用方法：一般使用者透過正常功能消費；root 可封塊、驗證與啟動 safe mode/forensic/分支治理復原；不得用備份覆寫 ledger。
- 原理：ledger 是 source of truth，wallet 由 ledger replay 重建；Phase 1A 私有鏈經濟層也以 append-only economy events replay 出 MINT、BURN、treasury、PROMO、EXCHANGE fund balances。
- 失敗情境與提示：safe mode、chain verify fail、分支/緊急治理復原需要人工確認、餘額顯示與鏈不一致。
- 測試方式：credit/debit、seal/verify、safe mode/forensic/branch recovery、economy replay / derived-cache verify、影片打賞、交易資金流。
- 相關文件連結：[07_POINTSCHAIN.md](07_POINTSCHAIN.md), [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md), [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md), [RUNTIME_RESET_AND_RECOVERY.md](ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md)

### Games / Board AI

- 一句話說明：遊戲區提供西洋棋、數獨、踩地雷、1A2B、俄羅斯方塊、真實版俄羅斯方塊、宇宙戰機、3D 射擊場，以及同頁本機模組遊戲；黑白棋、圍棋、五子棋已接上基礎 AI 與獨立棋力量化 benchmark。
- 設計目的：讓使用者能在同一遊戲頁切換遊戲，同時讓非西洋棋 AI 的強化有獨立可量化證據，不污染西洋棋 exp3/exp4/exp5 pipeline。
- 使用方法：使用者在遊戲區下拉選遊戲；西洋棋選 `Stockfish（本機）` 難度時才顯示 depth 欄位，後端會把深度限制在 `1` 到 `20`。黑白棋 / 圍棋 / 五子棋可切換 `對電腦` 與 AI 難度。維護者用 `python3 scripts/games/board_ai_benchmark.py` 產生 `runtime/reports/games/board_ai_benchmark_*.json`。圍棋 `katago` 難度可先執行 `python3 scripts/games/setup_katago.py` 自動下載 KataGo、模型並產生 config。
- 原理：三棋前端共用 `public/js/games/board-game-shared.js`，對電腦時呼叫 `POST /api/games/<game_key>/ai-move`，後端由 `services/games/board_ai.py` 回傳 `move/pass/finish`。圍棋 `katago` 先讀環境變數，沒有時自動找 `runtime/katago`。棋力量化由 `services/games/board_arena.py` 執行 round-robin、skill suite、Elo estimate 與非法步統計。
- 失敗情境與提示：若刪除某個本機遊戲模組，該遊戲會從前端 catalog 消失，不影響其他遊戲；若三棋 AI API 回 `不支援的棋類 AI`，先確認 `game_key` 是否為 `reversi/go/gomoku`；若 benchmark 出現 `illegal_moves > 0`，不可把該 candidate 視為可 promotion。
- 測試方式：`pytest -q tests/games/test_board_ai.py tests/games/test_board_arena.py tests/frontend/games/test_frontend_games.py`，再跑 `python3 scripts/games/board_ai_benchmark.py --games gomoku --engines random,easy --rounds 1 --max-plies 6 --output-dir /tmp/hackme_board_ai_benchmark_smoke` 做 CLI smoke；另測 Stockfish unavailable 時不顯示該難度、選到 Stockfish 時才顯示 depth，且 practice payload 正確保存深度。
- 相關文件連結：[games/README.md](games/README.md), [games/references/BOARD_AI_BENCHMARK.md](games/references/BOARD_AI_BENCHMARK.md), [API_REFERENCE.md](API_REFERENCE.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### Trading / Bots / Backtest

- 一句話說明：提供市場下單、借貸、bot、backtest、reference/risk-grade 價格，以及 root 可管理的 market registry。
- 設計目的：讓交易市場不再寫死於程式內，而是由 DB registry 控制執行期 source of truth，同時保留 catalog seed 供 bootstrap 與 drift 對照。
- 使用方法：root 可在市場 registry 後台新增市場、調整 provider mapping、停用市場；卡片現在也會顯示 `catalog_seed` / `custom`、`seed_version` 與 `seed_sync_status`。
- 原理：runtime 交易市場來自 `trading_markets_registry`；`services/trading_markets.py` 只負責 bootstrap seed 與版本對照，不會默默覆蓋 root 在 DB 內做過的調整。
- 失敗情境與提示：市場被 disable 時，後端會拒絕新下單但保留歷史；provider mapping probe 失敗時，不可啟用 `risk-grade` 用途；若 seeded 市場與 catalog 不一致，後台會顯示 `drifted`，但不會自動改回。
- 測試方式：registry CRUD、provider probe、precision/lot size/tick size 驗證、disabled market 阻擋、seed drift 狀態顯示、risk-grade gating。
- 相關文件連結：[08_TRADING_ENGINE.md](08_TRADING_ENGINE.md), [TRADING.md](trading/TRADING.md), [For_developer.md](For_developer.md), [11_QA_TESTING.md](11_QA_TESTING.md)

- 一句話說明：提供現貨交易、借貸交易、多交易所融合價格、DCA / 網格 / workflow bot 與回測。
- 設計目的：驗證金額、風控、撮合、PointsChain 結算與策略流程。
- 使用方法：root 先啟用 economy / trading，再到 `交易所` 分頁選擇價格來源（預設 Binance、可改融合價格或 root 手動價格）、調整手續費 / 借貸 APR，並決定是否要啟用 market registry、Bot Audit 與外部 `BTC_trade`。一般使用者交易頁的 `目前價格` 會每 `2` 秒刷新一次，買入/賣出預估與現貨 / 借貸浮盈虧也會同步更新。系統明確分成 `reference price` 與 `risk-grade price`：前者只用在展示、一般估值與 K 線；後者用在融資、強平、保證金、bot 風控、交易限制與正式風控口徑的 PnL。若來源降級成 fallback / cached / degraded，頁面會亮黃燈並提示「目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用」；但一般限價單仍需通過後端資產與市場狀態驗證。root 的 `融合價格即時比例` dashboard 顯示的是各 provider 在**本系統價格融合中的實際使用權重**、被排除原因與降級狀態，不代表整體市場的真實流動性占比。Grid Bot 建立前會先試算毛利、手續費、扣費後淨利與損益兩平間距；Bot Audit 會顯示未稽核 / 綠 / 黃 / 紅燈；market registry 則讓 root 新增 / 停用市場、調整 precision / lot size / tick size、維護 provider mapping，並先 probe 再決定是否允許 `risk-grade` 用途。詳細見 `08_TRADING_ENGINE.md`、`TRADING.md`、`TRADING_RISK_PRICE.md` 與 `TRADING_BOT_AUDIT.md`。
- 原理：前台顯示是輔助，實際撮合與結算由後端重新驗證；關鍵金額不信任前端。預設價格來源先走 Binance 公開 API，小型部署常態下不抓多交易所 order book，以降低交易頁輪詢延遲；若主要 API 不可用，才退到融合價格。root 若明確切成融合價格，系統會抓 Binance / OKX / Coinbase / Kraken / Gemini / Bitstamp 的掛單簿中價與深度，依深度或 root 手動權重融合；只要至少一個健康來源符合設定，就可作為交易風控來源，除非 root 提高門檻或啟用降級暫停策略。市場 seed、顯示 symbol、各交易所 provider id、預設手動價與 BTC_trade 支援條件仍集中在 `services/trading_markets.py`，但真正啟用中的市場、precision、lot/tick size、provider mapping 與 `risk-grade` 使用權限已進 DB registry，由 root 後台控制。交易頁每 `2` 秒輪詢一次輕量 `live-price` API，且每次拿到新價格後會同步重算買入/賣出預估；同一輪也會重算積分錢包裡的 spot / margin 浮盈虧與 root 虛擬總額，不再等到較慢的 full dashboard refresh 才跳一次。這支 API 不是純 read-only：它會在後端同步刷新 `trading_markets.manual_price_points / price_source` 的最新快取，讓後續撮合、估值與 dashboard 能共用同一份最新交易參考價。Grid Bot preview 也統一改由後端 `Decimal` 精算，會把每格毛利、買/賣手續費、扣費後淨利、損益兩平 spread 與紅/黃/綠燈一起回傳；前端只做顯示，不自行用浮點數偷算。交易帳本仍以整數 POINT 為最小單位，但手續費與利息的累積過程使用 `Decimal` / micropoints 保存小數殘值；只有現貨賣出、機器人停止、借貸結算或清算等真正結算點才轉成整數 POINT，且不足 1 點或有小數時無條件進位。借貸交易本身另有以名目金額計算的開 / 平倉手續費，例如保證金 100、借 400 買入時以 500 名目金額計費；借貸 APR 依借入資產分組，多單與空單因此可能使用不同利率；前台會直接顯示 `累積利息`、`已實扣`、`下一次計息`。後端也會同步累積每位使用者的 spot / margin 名目成交量、總 fee 與成交次數，供後續 VIP 系統或 root report 使用。回測引擎則把單批執行上限和總上限拆開：內部分段每批最多 `10,000` 根，但整體回測最多可連續處理 `20,000` 根；若 `candles < 2`，只有顯式 opt-in 才會抓 reference candles，不再靜默把隔離資料換成 live public history。交易 bot 稽核則由後端 scheduler 執行，先用「首筆成交或啟用滿 24 小時」作為納入條件，再產生綠 / 黃 / 紅燈與 bug report 對照資訊。BTC_trade 一鍵啟動則改成背景工作：資料 / 模型檢查、必要更新、重訓與預測都在伺服器端串接，root 前端只輪詢工作狀態，不會把長時間訓練誤判成 timeout 失敗。
- 失敗情境與提示：餘額不足、價格來源失效、circuit breaker、借貸流動性不足、功能旗標未完整啟用；若融合價格手動權重全部設成 0，系統會退回自動深度權重，並在 root dashboard / log 直接標示 `manual weights invalid`；若 order book 全失敗，會顯示 `價格來源降級` 並進入保守模式，而不是靜默把單一 ticker 當成正常 fused price。若你在手算小額單手續費時看到與舊版不同，先確認是否已升到 `2026.05.03-063` 之後的 rounding 規則，也確認是否還誤把 Grid 當成舊版 `50%` 折扣。若你在借貸部位看到 `interest_points` 暫時仍是 0，但 `interest_exact_points` 已有小數，代表系統正在累積未滿 1 點的利息殘值；若 APR 看起來和你預期不同，也要先分辨目前借的是 `BTC / ETH` 還是 USDT-equivalent quote asset。若回測區間超過 `20,000` 根 K 線，前後端都會明確要求縮小區間，而不是靜默截斷；若 `candles < 2` 卻還看到回測像是自己變成真實行情，請先確認 server 是否已升到 `2026.05.04-070`。若交易頁的 `目前價格` 看起來跟參考 K 線最新收盤不同，這在 `2026.05.04-071` 之後是正常設計：前者是實際交易參考價，後者只是圖表參考資料。若 root 稽核 dashboard 顯示 `未稽核`，通常代表 bot 尚未成交且也未滿 24 小時，不是系統壞掉。若 BTC_trade 一鍵啟動長時間停在訓練中，先看背景工作狀態；新版不再因單純 timeout 就判成失敗，只有腳本實際退出錯誤或等不到新的 report 才會轉紅。
- 測試方式：正常交易、邊界輸入、精度、回測、stress pentest、單一交易所 API 失效後的融合價格重算、root-only 融合價格 dashboard、Grid Bot fee preview 的紅 / 黃 / 綠燈與 break-even 驗證、交易 bot `未稽核 -> 綠 / 黃 / 紅燈` 稽核流程、DCA `max_runs=-1` 連續執行、restore consistency、`BTC/USDT 1h` 全年歷史回測、超過 `10,000` 根時的後端自動分段續跑、`candles < 2` isolation 驗證、小本金借貸利息 carry 驗證、參考 K 線圖的 `MA10 / MA20 / MA30 / MA60 / EMA12 / EMA26 / EMA50 / 布林線 / RSI14 / KD` 指標顯示，以及 `volume_stats / volume_summary` 是否正確累積。
- 相關文件連結：[08_TRADING_ENGINE.md](08_TRADING_ENGINE.md), [TRADING.md](trading/TRADING.md), [11_QA_TESTING.md](11_QA_TESTING.md)

### Snapshot / Restore / Reset

- 一句話說明：提供整站 snapshot、portable restore、runtime reset 與 PointsChain 恢復邊界。
- 設計目的：把「還原整站」與「修復帳本」分開，避免災難恢復時互相覆蓋。
- 使用方法：root 先建 snapshot，再進行 restore / reset；PointsChain 異常走 safe mode、forensic bundle、分支與緊急治理，不做 ledger backup restore。
- 原理：server snapshot、PointsChain append-only recovery、audit chain、runtime reset 各自有明確所有權邊界。
- 失敗情境與提示：snapshot restore 會回放 runtime secret files，若 restore event 出現 `runtime secret validation failed` 要先修這批檔案；reset 仍會清掉 runtime secrets 並要求重啟；PointsChain recovery 不能拿來代替全站 restore。
- 測試方式：create/list/download/restore/upload-restore/reset、post-restore consistency、offline/reconnect reset smoke。
- 相關文件連結：[09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md), [RUNTIME_RESET_AND_RECOVERY.md](ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md), [11_QA_TESTING.md](11_QA_TESTING.md)

## 相關文件連結

- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [04_USER_GUIDE.md](04_USER_GUIDE.md)
- [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md)
- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)
