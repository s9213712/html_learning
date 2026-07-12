# 07 PointsChain

一句話說明：PointsChain 是本專案的 permissioned financial settlement network；
PC1 負責 canonical reserve truth，PC0 負責 wrapped operational balances，Bridge
負責跨帳本結算。

## 設計目的

如果金額、點數、交易手續費、打賞直接改 wallet balance，就很難做審計、
恢復與異常修復。PointsChain 把重要資金變動收斂到可回放、可審計、
可分層驗證的 ledger 與 bridge events。它不是單鏈模型：PC1、PC0 與
Bridge 必須在帳務語意上分離。

## 使用方法

### 一般使用者

- 在站內消費、打賞、交易時，不直接碰鏈設定
- 看自己的 wallet 與 ledger 即可

### root

- 可查看 root report / audit
- root report 會統計目前在外積分、root-held points、ledger 淨額、供給差異、
  未封塊 ledger 數與 sealed coverage，作為未來全站區塊鏈化的供給口徑基礎
- root 積分錢包頁應提供唯讀的交易所基金 / 借貸流動性與全用戶倉位管理摘要，讓部署者能對照交易所基金、
  借貸倉位與 PointsChain 供給，不直接在頁面改用戶倉位
- 可 seal chain、verify chain、啟動 safe mode / forensic / 分支治理 recovery；不得建立可還原 ledger backup
- 可在 safe mode 下使用 one-click anomaly handler

## 原理

- PC1 canonical reserve 是資產供給與 reserve truth。
- PC0 operational wrapped layer 是站內營運負債，不是原生鏈上資產。
- Bridge settlement layer 負責 lock/mint、burn/unlock、pending settlement 與 invariant。
- `points_wallets` 是從 ledger replay 出來的結果，不是最終可信來源
- video tip、治理核准的官方錢包撥款、trading settle 都應寫入 PointsChain；手動 admin adjustment 已停用
- 恢復時重建 wallet，不信任舊 wallet snapshot 自己就是正確答案
- 全站供給相關 dashboard 應讀 ledger / report / snapshot 口徑，不應在前端自行加總後當作可信總量

## 模擬鏈錢包身份

- 一般會員註冊時會自動建立唯一的站內託管錢包（pc0 official hot wallet）；初始配點與註冊禮直接匯入該 pc0 錢包。
- 使用者可另行建立瀏覽器自管冷錢包或匯入冷錢包；冷錢包不會取代唯一 pc0 official hot wallet。
- RC1 不開放一般用戶可轉出的多簽錢包；舊多簽 policy 錢包會顯示為觀察/收款模式，不能轉出、不能支付服務費。
- 官方財庫多簽是正式功能：官方財庫提案通過與 timelock 結束後，仍需 manager+ / root signer 達到 threshold/weight 才能執行。
- 自管與匯入冷錢包的私鑰只在瀏覽器內產生或匯入；伺服器只收 public JWK、地址與簽章。
- root 可查看 mint / burn 系統錢包身份；這些身份只用於模擬供給 bookkeeping，不保存私鑰。

正式多帳本結算架構見
[architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md](architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md)，
詳細 wallet identity contract 見
[architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md](architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md)。

## Phase 1A 私有鏈經濟層

Status: RC1 雙層帳本路線。這一層提供 fund wallet、replay、derived cache、
snapshot read model、root dashboard、pc0 站內服務費、影音投幣、交易結算、
governance recovery 與 bridge invariant；產品 flow 未完成 walletize 的部分不得再擴張。

- 經濟層新增 MINT、BURN、official treasury、PROMO fund、EXCHANGE fund 五種 deterministic fund address；MINT/BURN 是 system-special address，不是官方 `pc0` 熱錢包。
- Bootstrap 會從 MINT idempotently 分配初始 official / promo / exchange fund，不會因重跑 root report 重複 mint。
- `points_economy_events` 是 append-only fund event ledger；`points_economy_derived_balances` 是 `derived_cache`，可 rebuild / verify。
- Root dashboard 顯示的 max supply、reserved locked、minted、burned、fund balance、circulating supply 都來自 replay / derived view，且 UI 必須分清 PC1 canonical reserve、PC0 wrapped operational liabilities、Bridge settlement 與 Audit。
- Economic incident 在 Phase 1A 只 append，不自動改 balance。
- 用戶可用自己的錢包地址送出交易；`pc0 -> pc0` 是站內託管帳本即時轉帳、免鏈上 fee；`pc0 -> pc1` 走 withdrawal bridge lock，達 20/20 Proved 後才完成；`pc1/cold -> pc0` 不是直接鏈上轉帳，必須走平台入金地址。
- Wallet-to-wallet 鏈上交易手續費歸入 system-special BURN，不進 official treasury，避免官方 / root 交易形成自收費收益。
- 站內高頻小額服務費預設走 `pc0` 站內託管錢包 immediate internal debit，`network_fee_points=0`，`service_fee_points=amount`，收入進官方 Treasury 可 replay 對帳。冷錢包直接服務付款目前拒絕，需先入金到 pc0 或等待正式 cold-chain approval flow。
- 「交易管理」顯示近期轉入 / 轉出、官方 Treasury 發點、transaction hash、pending/confirmed 狀態與 Proved 進度；root 可看全站近期交易，但不顯示 username 標籤。
- ComfyUI、Trading、Video、Storage、Games 產品流仍在逐步 walletize，未完成的 legacy flow 不應再擴張。

硬限制見 [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md)。

## 自動發放規則

- 初始配點不在 legacy 帳本身份上直接入帳；符合資格時匯入會員唯一的 pc0 官方熱錢包。
- 新註冊一般會員的註冊禮會在註冊時匯入 pc0 官方熱錢包，且仍以 ledger idempotency 保證只入帳一次。
- 會員生日禮為 1000 點與 1GB 雲端硬碟 7 日，只在會員生日當天成功登入時發放。
- 生日判定使用 root 設定的伺服器時區；未在生日當天登入則不補發。
- 生日禮金透過 `birthday_gift:<year>:<user_id>` idempotency key 寫入 PointsChain，生日雲端容量使用 `birthday_storage:<user_id>:<year>` 記錄，同一會員同一年最多入帳一次。
- root 帳號不領取生日禮金；管理者帳號若有生日資料，仍以會員登入規則處理。

## 失敗情境與提示

- 前端餘額顯示與後端不一致：
  先跑 chain verify，再看是否需要 recovery。
- 想用 server snapshot restore 修好 PointsChain：
  不可行；snapshot 可封存 `finance/points_chain/trading` 供 forensic，但 restore 會以
  `append_only_financial_restore_disabled` 明確略過 live 金融 DB。若只有經濟鏈壞掉，
  走 safe mode、forensic bundle、分支、緊急治理與 append-only correction。
- chain verify fail：
  應建立 forensic / branch / governance plan，而不是硬覆蓋 live ledger。
- root 以為自己交易也會寫 PointsChain：
  root 的模擬交易餘額是分開的，與正常使用者點數結算不同。

## 測試方式

- wallet / ledger / disabled manual adjustment / seal / verify / disabled backup endpoints / recovery
- 影片打賞與交易後的 ledger 對帳
- root report 的目前在外積分、ledger 淨額與 wallet replay 對帳
- root 積分錢包交易所基金 / 借貸流動性 / 全用戶倉位唯讀摘要與交易快照對帳
- snapshot restore 或 safe-mode recovery 後的 wallet replay / rebuild
- 極小額、多次累加、精度與手續費驗算

## 相關文件連結

- [BLOCKCHAIN/README.md](BLOCKCHAIN/README.md)
- [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)
- [10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md](10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md)
- [architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md](architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md)
- [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md)
- [architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md](architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md)
- [RUNTIME_RESET_AND_RECOVERY.md](ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md)
- [For_developer.md](For_developer.md)
- [VIDEO_PLATFORM.md](video/VIDEO_PLATFORM.md)
- [TRADING.md](trading/TRADING.md)
