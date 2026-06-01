# BTC_trade Integration

一句話說明：這份文件只整理外部 `BTC_trade` repo 的接線方式、root 操作入口與第一輪排查方向。

跨功能總覽另見：
[EXTERNAL_INTEGRATION_PLAYBOOK.md](../EXTERNAL_INTEGRATION_PLAYBOOK.md)。

## 什麼時候需要看

只有在你真的要接外部 `BTC_trade` repo，或要使用 root-only 的
`檢查 BTC_trade / 一鍵啟動預測` 時才需要。

## 基本流程

1. 先完成站點部署：
   [01_DEPLOY_QUICKSTART.md](../01_DEPLOY_QUICKSTART.md)
2. 先理解 root 管理入口：
   [03_ADMIN_GUIDE.md](../03_ADMIN_GUIDE.md)
3. 再看交易系統文件：
   [08_TRADING_ENGINE.md](../08_TRADING_ENGINE.md)

## 管理者應知道的事

- `BTC_trade` 是外部 repo 整合，不是站內原生模組
- 預設 repo URL 是 `https://github.com/s9213712/BTC_trade.git`
- 預設 branch 是 `strategy/v15b-plus`
- 預設本機 clone 位置是 ignored 的 `external/BTC_trade`
- `檢查 BTC_trade` 應先確認腳本、資料與模型是否齊全
- `一鍵啟動預測` 可能先做資料更新、重訓，再等待新的 report；長時間執行不等於失敗

## CLI bridge

先只看狀態：

```bash
PYTHONPATH=. python3 scripts/trading/bridges/btc_signal_bridge.py \
  --btc-trade-dir external/BTC_trade \
  --status
```

乾跑 bridge，不下單：

```bash
PYTHONPATH=. python3 scripts/trading/bridges/btc_signal_bridge.py \
  --btc-trade-dir external/BTC_trade \
  --market-symbol BTC/USDT \
  --dry-run
```

## 深層參考

- [08_TRADING_ENGINE.md](../08_TRADING_ENGINE.md)
- [TRADING.md](TRADING.md)
- [12_TROUBLESHOOTING.md](../12_TROUBLESHOOTING.md)
