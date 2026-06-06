# ComfyUI Admin

一句話說明：這份文件只處理 `root/admin` 角度的 ComfyUI / Civitai 管理操作，不重複一般生圖介面教學。

## 先看這份的時機

當你要做以下事情時再打開：

- 決定 ComfyUI 用 `local` 還是 `remote`
- 管理 root-only Civitai 搜尋 / inspect / 下載
- 管理本地模型匯入、VAE、LoRA、ControlNet、workflow preset
- 排查 ComfyUI 能力缺失、下載權限或工作流匯入問題

## 管理順序

1. 先確認站點已正常部署：
   [01_DEPLOY_QUICKSTART.md](../01_DEPLOY_QUICKSTART.md)
2. 再看 root/admin 操作入口：
   [03_ADMIN_GUIDE.md](../03_ADMIN_GUIDE.md)
3. 最後才進 ComfyUI 細節：
   [WEB.md](../WEB.md)

## 重要邊界

- `remote` 模式只負責呼叫遠端 API 生圖；Civitai / model-upload 仍寫入本站設定的 ComfyUI models 目錄
- `Civitai API Key` 與 root-only 下載工具在 `local` / `remote` 模式都可見；遠端主機不同機時需自行同步或掛載 models 目錄
- `ComfyUI Account API Key` 只用於官方付費 / API nodes。Key 由 root 在伺服器設定中輸入，前端 workflow JSON、匯出檔與 audit 不可保存明文 key
- workflow preset / workflow JSON 匯入仍要通過 sanitize，不能接受本機絕對路徑、外部 URL、shell / exec 類節點

## 本地 / 遠端功能矩陣

| 功能 | local 本地 ComfyUI | remote 遠端 ComfyUI API | 備註 |
|---|---:|---:|---|
| 一般生圖 / workflow 執行 | 可用 | 可用 | 只要目標 ComfyUI API 可連線且模型 / custom nodes 齊全即可。 |
| `/status`、模型清單、history、dependency check | 可用 | 可用 | 由目前設定的 ComfyUI API backend 回應。 |
| workflow preset、template importer、UI graph normalize、manifest 顯示 | 可用 | 可用 | 這些主要是本站後端功能，不依賴本地檔案下載能力。 |
| Visual Builder / node catalog | 可用 | 可用 | node catalog 會呼叫目前 backend 的 `/object_info`。 |
| ComfyUI 官方付費 / API nodes（Partner Nodes） | 可用 | 條件式可用 | 需要 ComfyUI Account API Key 與官方 credits；遠端 backend 也必須支援 API Key integration 與安全網路條件。 |
| 啟動 / 停止 ComfyUI process | 可用 | 不可用 | 只管理本站主機上 `comfyui_base_dir` 內的本地程序。 |
| 下載 Linux 啟動腳本 template | 可用 | 不可用 | 只給本地模式配置使用。 |
| Civitai 搜尋 / inspect / 下載模型到 ComfyUI 目錄 | 可用 | 可用 | 下載會寫入本站設定的 ComfyUI models 目錄；遠端主機若不同機需自行同步或掛載。 |
| 本地模型檔案匯入 / 掃描本地 base dir | 可用 | 可用 | 需要本站主機能讀寫 ComfyUI base dir；遠端 API 不會替本站寫檔。 |
| 刪除 ComfyUI output 原始檔 | 可用 | 不可用 | 遠端模式只會清本站網頁預覽，不會刪遠端 output。 |

`remote` 不是「雲端託管本站」；它只是本站後端把 ComfyUI API request 送到指定 `http(s)://host:port`。

## API 與 Key 填寫位置

入口：root 登入後打開「伺服器設定」→「ComfyUI 連線與模型」。

### 連線模式

- `連線模式 = local`：
  - `本地 ComfyUI 目錄`：填本站主機可讀寫的 ComfyUI base dir。
  - `本地啟動腳本`：填 base dir 內的啟動腳本，例如 `run_in_linux.sh`。
  - `API Host` / `API Port`：填本站後端要呼叫的 ComfyUI API host/port，預設通常是 `localhost:8192`。
- `連線模式 = remote`：
  - `遠端 ComfyUI API`：填完整 `http(s)://host:port`。
  - 不要包含帳密、path、query string。例如填 `https://comfy.example.com:8192`，不要填 `/prompt`。

### Civitai API Key

`Civitai API Key` 只用於 root 的模型搜尋 / inspect / 下載區。它不是生圖 backend 的 ComfyUI API 位址，也不是 ComfyUI Account API Key；遠端模式下仍可保存與使用，但檔案會寫入本站設定的 ComfyUI models 目錄。它顯示在 AI 後端設定的 `ComfyUI` family，不會混在 `HF` family；前台 root 會在 ComfyUI surface 看到明確的 `Civitai / 模型匯入` 入口，HF active surface 則維持 Hugging Face / Diffusers 控制。

### ComfyUI Account API Key

`ComfyUI Account API Key` 只用於 ComfyUI 官方付費 / API nodes（官方文件稱 Partner Nodes / API Nodes）：

1. root 先勾選「允許 ComfyUI 付費 / API nodes」。
2. 在「ComfyUI Account API Key」欄位貼上 key 後儲存。
3. 留空儲存代表不變更；勾選「清除已儲存的 ComfyUI Account API Key」才會刪除。
4. `/api/admin/settings` 只回傳 `comfyui_account_api_key_configured`，不回傳明文 key。
5. 執行 workflow 時，後端只把 key 注入送往 ComfyUI `/prompt` 的 `extra_data.api_key_comfy_org`。

## 付費 / API nodes

ComfyUI 官方 API nodes 需要 ComfyUI Account API Key。本站的安全邊界是：

- root 必須先在「伺服器設定 / ComfyUI 連線與模型」啟用「允許 ComfyUI 付費 / API nodes」
- `ComfyUI Account API Key` 留空儲存代表不變更；勾選清除才會刪除已保存 key
- `/api/admin/settings` 只回傳 `comfyui_account_api_key_configured`，不回傳明文 key
- 後續執行 workflow 時，後端只在送往目前設定的 ComfyUI `/prompt` payload 補入 `extra_data.api_key_comfy_org`
- 若 workflow JSON、layout JSON 或匯出檔包含明文 key，視為缺陷，必須修正

## ComfyUI Credits 與本站積分完全不同

ComfyUI credits 是 ComfyUI 官方帳號系統的付費額度，用於官方 Partner Nodes / API Nodes。本站的積分、錢包、交易所體驗金、任務獎勵都不是 ComfyUI credits，不能用來支付 ComfyUI 官方 API nodes。

請務必用這個規則判斷成本：

- 使用本站一般功能可能消耗本站積分 / quota。
- 使用 ComfyUI 官方 Partner Nodes / API Nodes 會消耗 ComfyUI 官方 credits。
- 兩者帳本不互通，也不會互相折抵。
- 本站目前不販售、不充值、不退款 ComfyUI credits。

ComfyUI 官方文件說明 credits 需先登入 ComfyUI account，然後在 ComfyUI UI 的 `Settings` → `Credits` 購買、查看餘額與 credit history。ComfyUI Account API Key integration 也要求帳號有足夠 credits 才能呼叫 paid API nodes。本站目前沒有依賴的穩定官方 REST endpoint 可查 credit balance，所以只顯示「key 是否已設定」與「這次 workflow 可能消耗 credits」的風險提示；實際餘額與消耗紀錄請到 ComfyUI UI 查看。

## 深層參考

- [WEB.md](../WEB.md)
- [For_developer.md](../For_developer.md)
- [12_TROUBLESHOOTING.md](../12_TROUBLESHOOTING.md)
- ComfyUI 官方 credits 說明：<https://docs.comfy.org/interface/credits>
- ComfyUI 官方 API Key integration：<https://docs.comfy.org/development/comfyui-server/api-key-integration>
