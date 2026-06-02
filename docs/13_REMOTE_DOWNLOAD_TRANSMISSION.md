# 13 Remote Download: Transmission and aria2

一句話說明：Cloud Drive 的 Direct link、BT、magnet 遠端下載可以用不同後端；部署者應用 env 或 root 前台設定把 BT/magnet 接到 Transmission RPC，並保留 aria2c fallback。

## 支援模式

| 下載類型 | 預設後端 | 說明 |
| --- | --- | --- |
| HTTP/HTTPS Direct link | 內建 HTTP/remote worker | 不需要 Transmission。|
| `.torrent` URL / `.torrent` upload | Transmission 或 aria2c | 由 `BT/magnet 下載後端` 決定。|
| magnet link | Transmission 或 aria2c | 建議 production 使用 Transmission RPC。|

後端選項：

| 值 | 行為 |
| --- | --- |
| `auto` | 優先用 Transmission RPC；RPC 不可用或新增任務失敗時 fallback aria2c。|
| `transmission` | 強制只用 Transmission RPC；RPC 不可用就報錯。|
| `aria2` | 強制使用 aria2c。|

`auto` 不會在使用者取消、暫停、容量超限或下載中錯誤時偷偷重跑 aria2；fallback 只用於 Transmission RPC 不可用或無法新增任務這類後端可用性問題。

## 部署 env 設定

正式部署可在 `/etc/hackme_web/hackme-web.env` 加入：

```env
HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL=1
HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER=1
HACKME_BT_BACKEND=auto
HACKME_TRANSMISSION_RPC_URL=http://127.0.0.1:9091/transmission/rpc
# HACKME_TRANSMISSION_RPC_USERNAME=
# HACKME_TRANSMISSION_RPC_PASSWORD=
```

這些鍵已列入 [deploy/systemd/hackme-web.env.example](../deploy/systemd/hackme-web.env.example)。

注意：env 會作為 fresh DB / first boot 的預設值。已存在的站點若 root 前台已保存過設定，請以 root 前台為準，或直接更新 DB/system settings。

## 互動式啟動腳本設定

本機 / staging 用 `./test_for_develop.sh` 時，不一定要手寫 env 檔。互動模式會在 server runner / capacity 後詢問：

```text
Configure remote download / BT backend for this launch [y/N]:
BT/magnet remote-download backend:
  1) auto - prefer Transmission RPC, fallback aria2 when RPC is unavailable
  2) transmission - require Transmission RPC
  3) aria2 - force aria2c
Transmission RPC URL [http://127.0.0.1:9091/transmission/rpc]:
Transmission RPC username (blank = no auth) []:
Transmission RPC password (blank = no auth) []:
Remote download global concurrency [1]:
Remote download per-user concurrency [1]:
```

非互動模式可用 flags：

```bash
./test_for_develop.sh --cli \
  --bt-backend auto \
  --transmission-rpc-url http://127.0.0.1:9091/transmission/rpc \
  --remote-download-global 1 \
  --remote-download-per-user 1
```

若有 RPC 帳密，再加：

```bash
  --transmission-rpc-username USER \
  --transmission-rpc-password PASS
```

互動式腳本只影響本次啟動匯出的 env。正式部署仍應把穩定值寫進 `/etc/hackme_web/hackme-web.env` 或在 root 前台保存。

## root 前台設定

root 登入後到系統設定，設定：

```text
遠端下載全站併發：老硬體建議 1
遠端下載單用戶併發：老硬體建議 1
BT/magnet 下載後端：auto
Transmission RPC URL：http://127.0.0.1:9091/transmission/rpc
Transmission RPC 帳號：依 daemon 設定，無認證可留空
Transmission RPC 密碼：依 daemon 設定，無認證可留空
```

root 前台設定會覆蓋 fresh DB/env 預設，適合部署後營運調整。

## 安裝與啟動 Transmission daemon

Ubuntu/Debian：

```bash
sudo apt install transmission-daemon transmission-cli aria2 -y
sudo systemctl daemon-reload
sudo systemctl enable --now transmission-daemon
systemctl status transmission-daemon --no-pager
```

確認 RPC：

```bash
transmission-remote 127.0.0.1:9091 --session-info
```

若 daemon 啟用 RPC 帳密：

```bash
transmission-remote 127.0.0.1:9091 --auth 使用者:密碼 --session-info
```

HTTP 檢查：

```bash
curl -v http://127.0.0.1:9091/transmission/rpc
```

第一次未帶 session id 時，Transmission 正常會回：

```text
HTTP/1.1 409 Conflict
Server: Transmission
X-Transmission-Session-Id: <session id>
```

這是 Transmission RPC 的 CSRF/session 機制，不是錯誤；hackme_web client 會自動取 session id 後重送。

## app capability 檢查

當 Transmission RPC 可用，capability 應顯示類似：

```text
{
  'direct_link': True,
  'bt_magnet': True,
  'bt_file': True,
  'bt_backend': 'auto',
  'bt_backend_active': 'transmission',
  'aria2c_path': '/usr/bin/aria2c',
  'transmission_rpc_url': 'http://127.0.0.1:9091/transmission/rpc',
  'transmission_rpc_available': True
}
```

若看到：

```text
'bt_backend_active': 'aria2'
```

代表 Transmission RPC 沒通，系統正在 fallback aria2。

## 故障排查

### systemd timeout

已知失敗輸出範例：

```text
transmission-daemon.service: start operation timed out
Closing transmission session... done.
Failed to start transmission-daemon.service - Transmission BitTorrent Daemon.
```

處理：

```bash
sudo systemctl daemon-reload
sudo systemctl restart transmission-daemon
journalctl -u transmission-daemon -n 120 --no-pager
sudo sed -n '1,220p' /etc/transmission-daemon/settings.json
```

### 9091 沒監聽

```bash
ss -ltnp | grep 9091
systemctl status transmission-daemon --no-pager
```

### 帳密錯誤

用 `transmission-remote --auth 使用者:密碼 --session-info` 先確認，再把同一組填進 root 前台或 env。

### app 還是走 aria2

依序確認：

1. `systemctl status transmission-daemon --no-pager`
2. `transmission-remote 127.0.0.1:9091 --session-info`
3. root 前台 `BT/magnet 下載後端` 是否為 `auto` 或 `transmission`
4. RPC URL/帳密是否正確
5. `HACKME_BT_BACKEND` 是否被設定為 `aria2`

## 安全建議

- RPC 建議只允許本機 `127.0.0.1`。
- 若 RPC 對 LAN 開放，必須啟用帳密。
- 不要把無認證的 `9091` 暴露到公網。
- 老硬體先保持 `global=1`、`per_user=1`，確認 p95/p99 與磁碟 IO 穩定後再提高。
