# 00 Start Here

一句話說明：這是 `hackme_web` 的角色導向入口，先回答「你是誰、現在要做什麼、下一份應該看哪裡」。

## 先記住一個原則

若你還不熟這個 repo，請先把 [README.md](../README.md) 看完，再把
[docs/README.md](README.md) 當成 canonical doc index。入口文件只負責導流，
不要在這裡找所有深層功能細節。

## 如果你是第一次部署的人

1. [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md)
2. [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
3. [Production templates](../deploy/README.md)
4. [11_QA_TESTING.md](11_QA_TESTING.md)
5. [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)

這條路線的目標是：

- 先把站跑起來
- 知道 runtime / HTTPS / cookie / Nginx / systemd / backup / snapshot 的基本順序
- 上線前知道要驗什麼

本機或 staging 可以先用 `server.py` / `test_for_develop.sh` 驗證；正式對外服務請走
[02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md) 與 repo 內的 `deploy/` 範本，
不要把 Flask development server 直接暴露給使用者。

## 如果你是 root / admin

1. [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
2. [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
3. [11_QA_TESTING.md](11_QA_TESTING.md)

需要做高風險操作時，再進深層文件：

- [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md)
- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)

## 如果你是一般使用者或要寫使用教學

1. [04_USER_GUIDE.md](04_USER_GUIDE.md)
2. [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)

## 如果你是開發者 / API 維護者 / QA

1. [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
2. [For_developer.md](For_developer.md)
3. [11_QA_TESTING.md](11_QA_TESTING.md)
4. [AGENTS/QA_MISSION_FOR_AGENTS.md](AGENTS/QA_MISSION_FOR_AGENTS.md)

## 若你卡住

- 不確定部署順序：回 [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md)
- 不確定可不可以上線：看 [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
- 不知道功能依賴：看 [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
- 看到錯誤或行為不對：看 [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)

## 相關文件連結

- [README.md](../README.md)
- [README.md（文件總索引）](README.md)
- [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md)
- [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
- [Production templates](../deploy/README.md)
- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [04_USER_GUIDE.md](04_USER_GUIDE.md)
- [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
- [10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md](10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)
- [13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md)
