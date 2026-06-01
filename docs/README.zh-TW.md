# hackme_web 文件入口

[English README](../README.md)

目前 Release ID：`2026.06.01-003`

這份文件只做導覽，不放功能流水帳。近期變更看
[UPDATE_SUMMARY.md](UPDATE_SUMMARY.md)，完整英文索引看
[README.md](README.md)。

## 先從這裡開始

| 你要做什麼 | 先讀 |
|---|---|
| 第一次接手 repo | [00_START_HERE.md](00_START_HERE.md) |
| 本機或 staging 啟動 | [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md) |
| production 上線 | [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md) |
| root/admin 維運 | [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md) |
| 一般使用者教學 | [04_USER_GUIDE.md](04_USER_GUIDE.md) |
| 功能總覽 | [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md) |
| QA / 驗證 | [11_QA_TESTING.md](11_QA_TESTING.md) |
| 故障排查 | [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md) |

## 最短啟動

本機 / staging：

```bash
python3 server.py --doctor
python3 server.py
./test_for_develop.sh --port 50785
```

正式對外服務：

```text
先讀 02_DEPLOY_PRODUCTION.md，再套用 ../deploy/README.md 的 Nginx / systemd 範本。
Nginx 對外，Gunicorn 只綁 127.0.0.1:8000。
```

原則：

- 正式啟動前先跑 `python3 server.py --doctor`。
- 開發測試優先用 `./test_for_develop.sh`，它會在 `/tmp` 建隔離副本。
- 臨時 LAN / NAT public IP 測試可看 [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md) 的 `--public-host`、背景 log 與 `--shutdown` 用法。
- 不要把 Flask development server 直接暴露給使用者。
- 不要直接把 runtime、cache、pytest 產物留在 repo 工作樹。

## 主題路線

| 主題 | 文件 |
|---|---|
| 安全模型 | [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [SECURITY.md](SECURITY.md) |
| PointsChain | [07_POINTSCHAIN.md](07_POINTSCHAIN.md), [architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md](architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md), [architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md](architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md), [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md) |
| 帳本 / DB 架構 | [architecture/PC0_DUAL_RAIL_WALLET_MODEL.md](architecture/PC0_DUAL_RAIL_WALLET_MODEL.md), [architecture/DATABASE_DOMAIN_SPLIT.md](architecture/DATABASE_DOMAIN_SPLIT.md) |
| 設定 UI 重整 | [architecture/SETTINGS_UI_REDESIGN_TODO.md](architecture/SETTINGS_UI_REDESIGN_TODO.md) |
| Trading | [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md), [trading/README.md](trading/README.md) |
| Snapshot / Reset / Restore | [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md), [ops_boundaries/README.md](ops_boundaries/README.md) |
| 個人主頁 / 好友系統 | [social/USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md) |
| ComfyUI | [comfyui/README.md](comfyui/README.md) |
| Games / AI | [games/README.md](games/README.md) |
| Video | [video/README.md](video/README.md), [video/VIDEO_STREAMING_SERVICE_TIERS.md](video/VIDEO_STREAMING_SERVICE_TIERS.md) |
| Server Mode v2 | [server_mode_v2/README.md](server_mode_v2/README.md) |
| 正式部署範本 | [../deploy/README.md](../deploy/README.md), [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md) |
| Agent QA | [AGENTS/README.md](AGENTS/README.md) |
| Agent research / 未來規格 | [AGENTS/research/README.md](AGENTS/research/README.md) |

Trading 背景常駐引擎設計入口：
[trading/TRADING_BACKGROUND_ENGINE.md](trading/TRADING_BACKGROUND_ENGINE.md)。

個人主頁、好友代碼、指定對象權限與 PM / private group 好友限制入口：
[social/USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md)。

部署判讀原則：日常部署以 numbered guides、domain `README.md` 與 API reference
為準；`archive/`、`evidence/`、一次性報告是歷史證據；`AGENTS/research/` 是未來規格，
除非同一功能也在正式操作文件中標為已實作，否則不要把它當成可直接上線的功能。

## 深層參考

- [For_developer.md](For_developer.md)
- [API_REFERENCE.md](API_REFERENCE.md)
- [CLI_ADMIN_PLAYBOOK.md](CLI_ADMIN_PLAYBOOK.md)
- [WEB.md](WEB.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [../deploy/README.md](../deploy/README.md)
- [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)
- [RELEASE_LAYOUT.md](RELEASE_LAYOUT.md)
- [REPOSITORY_STRUCTURE.md](REPOSITORY_STRUCTURE.md)
- [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md)
