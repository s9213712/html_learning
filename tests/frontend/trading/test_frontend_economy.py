from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_trading_background_refresh_failures_are_visible():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")

    for silent_call in [
        "loadTradingLivePrice().catch(() => {})",
        "loadGridBots().catch(() => {})",
        "loadTradingBotCompetition().catch(() => {})",
        "renderGridBotPreview({ quiet: true }).catch(() => {})",
    ]:
        assert silent_call not in trading_js
    assert "function tradingSetBackgroundStatus" in trading_js
    assert "即時價格讀取失敗" in trading_js
    assert "競賽排行讀取失敗" in trading_js


def test_public_registration_only_requires_account_password_and_nickname():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    register_section = index_html.split('id="sec-register"', 1)[1].split('id="captcha-field"', 1)[0]

    assert 'id="reg-user"' in register_section
    assert 'id="reg-pw"' in register_section
    assert 'id="reg-pw-confirm"' in register_section
    assert 'id="reg-autofill-decoys"' in register_section
    assert 'id="reg-pw" placeholder="請設定密碼" autocomplete="off"' in register_section
    assert 'id="reg-pw-confirm" placeholder="請再次輸入密碼" autocomplete="off"' in register_section
    assert 'data-lpignore="true"' in register_section
    assert 'id="reg-nickname"' in register_section
    assert "bindRegisterAutofillGuards" in auth_js
    assert "registerAutofillGuardBound" in auth_js
    assert 'input.setAttribute("data-1p-ignore", "true");' in auth_js
    assert "input.readOnly = true;" in auth_js
    assert 'id="reg-idno"' not in register_section
    assert "身分證不可為空" not in auth_js
    assert "真實姓名不可為空" not in auth_js
    assert "請填寫生日" not in auth_js
    assert "請填寫電話" not in auth_js
    assert "id_number: idNo" not in auth_js


def test_economy_missing_management_snapshot_uses_opt_in_200_response():
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")

    assert 'requestUrl.includes("/root/management/snapshots/")' in economy_js
    assert 'missing_ok=1' in economy_js
    assert 'fetchEconomyJson(json.latest_snapshot_url, { allowMissingSnapshot: true })' in economy_js


def test_root_points_page_is_chain_operations_console():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert 'id="economy-user-summary-grid"' in index_html
    assert 'id="economy-user-ledger-card"' in index_html
    assert 'id="economy-ledger-body" style="display:none;"' not in index_html
    assert 'id="economy-ledger-body">' in index_html
    assert '最近 50 筆收入 / 支出明細' in index_html
    assert 'id="economy-subtabs"' in index_html
    assert 'id="tab-economy-balance"' in index_html
    assert 'id="tab-economy-explorer"' in index_html
    assert 'id="tab-economy-funding-pools"' not in index_html
    assert 'id="tab-economy-all-positions"' not in index_html
    assert 'id="tab-economy-positions"' not in index_html
    assert 'id="tab-economy-chain"' in index_html
    assert 'id="economy-balance-page"' in index_html
    assert 'id="economy-explorer-page"' in index_html
    assert 'id="economy-governance-page"' in index_html
    assert 'id="economy-funding-pools-page"' not in index_html
    assert 'id="economy-all-positions-page"' not in index_html
    assert 'id="economy-positions-page"' not in index_html
    assert 'id="economy-chain-page"' in index_html
    economy_balance_page = index_html.split('id="economy-balance-page"', 1)[1].split('id="economy-explorer-page"', 1)[0]
    economy_explorer_page = index_html.split('id="economy-explorer-page"', 1)[1].split('id="economy-governance-page"', 1)[0]
    economy_governance_page = index_html.split('id="economy-governance-page"', 1)[1].split('id="economy-chain-page"', 1)[0]
    economy_chain_page = index_html.split('id="economy-chain-page"', 1)[1].split('id="economy-msg"', 1)[0]
    assert 'id="economy-user-summary-grid"' in economy_balance_page
    assert 'id="economy-user-ledger-card"' in economy_balance_page
    assert 'id="economy-root-balance-card"' not in economy_balance_page
    assert 'id="economy-root-outstanding-points"' not in economy_balance_page
    assert 'id="economy-root-virtual-card"' not in economy_balance_page
    assert 'id="economy-trading-summary-card"' not in economy_balance_page
    assert 'id="economy-root-wallet-management-card"' in economy_balance_page
    assert 'id="economy-root-wallet-mint-address"' in economy_balance_page
    assert 'id="economy-root-wallet-official-address"' in economy_balance_page
    assert 'id="economy-root-wallet-promo-address"' in economy_balance_page
    assert 'id="economy-root-wallet-exchange-address"' in economy_balance_page
    assert 'id="economy-root-wallet-burn-address"' in economy_balance_page
    assert 'id="economy-root-official-grant-details"' in economy_balance_page
    assert 'id="economy-root-official-grant-destination"' in economy_balance_page
    assert 'id="economy-root-official-grant-btn"' in economy_balance_page
    assert 'id="economy-treasury-analysis-summary"' in economy_balance_page
    assert "官方財庫收支分析" in economy_balance_page
    assert "Treasury Signer Center" not in economy_balance_page
    assert 'id="economy-treasury-service-fee-list"' in economy_balance_page
    assert "finance-flow-dashboard-list" in economy_balance_page
    assert 'id="economy-treasury-income-list"' in economy_balance_page
    assert 'id="economy-treasury-expense-list"' in economy_balance_page
    assert 'id="economy-treasury-monthly-feature-list"' in economy_balance_page
    assert 'id="economy-treasury-pricing-fit-list"' not in economy_balance_page
    assert 'id="economy-treasury-analysis-refresh-btn"' in economy_balance_page
    assert 'id="economy-treasury-analysis-updated-at"' in economy_balance_page
    assert 'id="economy-explorer-query"' in economy_explorer_page
    assert 'id="economy-explorer-search-btn"' in economy_explorer_page
    assert 'id="economy-explorer-result"' in economy_explorer_page
    assert 'id="economy-explorer-layer-tabs"' in economy_explorer_page
    assert 'data-economy-explorer-layer="pc1"' in economy_explorer_page
    assert 'data-economy-explorer-layer="pc0"' in economy_explorer_page
    assert 'data-economy-explorer-layer="bridge"' in economy_explorer_page
    assert 'data-economy-explorer-layer="audit"' in economy_explorer_page
    assert "Settlement Explorer" in economy_explorer_page
    assert "Operational Explorer" in economy_explorer_page
    assert "Bridge Explorer" in economy_explorer_page
    assert "Audit Explorer" in economy_explorer_page
    assert 'id="economy-governance-card"' in economy_governance_page
    assert 'id="economy-governance-scam-create-btn"' not in economy_governance_page
    assert 'id="economy-governance-freeze-create-btn"' not in economy_governance_page
    assert 'id="economy-governance-branch-create-btn"' in economy_governance_page
    assert 'id="economy-treasury-analysis-summary"' not in economy_governance_page
    assert 'id="economy-governance-treasury-create-btn"' not in economy_governance_page
    assert "payload hash" in economy_js
    assert "signing hash" in economy_js
    assert "execution_payload_hash" in economy_js
    assert "signing_payload_hash" in economy_js
    assert "全站多人投票" in economy_governance_page
    assert "20 Proved" in economy_explorer_page
    assert "受鏈上忙碌度影響" in economy_explorer_page
    assert 'id="economy-root-virtual-card"' not in index_html
    assert 'id="economy-trading-summary-card"' not in index_html
    assert 'id="economy-asset-overview-card"' not in index_html
    assert 'id="trading-root-sitewide-card"' in index_html
    assert 'id="trading-asset-overview-card"' in index_html
    assert 'id="trading-root-reserve-balance"' in index_html
    assert 'id="trading-root-funding-available"' in index_html
    assert 'id="trading-root-lending-pool-list"' in index_html
    assert 'id="economy-root-position-users"' not in index_html
    assert 'id="economy-root-position-wallet-total"' not in index_html
    assert 'id="economy-root-wallet-position-list"' not in index_html
    assert "會員錢包摘要" not in index_html
    assert 'id="tab-economy-transactions"' in index_html
    assert 'id="economy-transactions-page"' in index_html
    assert 'id="economy-transactions-list"' in index_html
    assert 'id="economy-transactions-refresh-btn"' in index_html
    assert 'id="economy-transfer-last-result"' in index_html
    assert 'id="economy-wallet-onboarding-card"' in index_html
    assert 'id="economy-wallet-create-card"' in index_html
    assert '<details class="drive-card" id="economy-wallet-create-card"' in index_html
    assert '<details class="drive-card" id="economy-wallet-create-card" style="display:none;margin-top:.75rem;">' in index_html
    assert "錢包管理" in index_html
    assert "冷錢包管理" in index_html
    assert 'id="economy-wallet-initial-grant"' not in index_html
    assert 'id="economy-wallet-initial-grant-note"' not in index_html
    assert 'id="economy-wallet-signup-bonus"' not in index_html
    assert 'id="economy-wallet-bound-actions"' in index_html
    assert 'id="economy-wallet-delete-cold-address"' not in index_html
    assert 'id="economy-wallet-delete-cold-btn"' not in index_html
    assert 'id="economy-wallet-generated-panel"' in index_html
    assert 'id="economy-wallet-generated-address"' in index_html
    assert 'id="economy-wallet-generated-file-name"' in index_html
    assert 'id="economy-wallet-generated-trade-password"' in index_html
    assert 'id="economy-wallet-download-file-btn"' in index_html
    assert 'id="economy-wallet-copy-trade-password-btn"' in index_html
    assert 'id="economy-wallet-use-generated-cold-btn"' in index_html
    assert '["economy-wallet-download-file-btn", economyDownloadDraftColdWalletFile]' in economy_js
    assert '["economy-wallet-copy-trade-password-btn", economyCopyDraftTradePassword]' in economy_js
    assert "錢包管理：已準備下載冷錢包檔" in economy_js
    assert "錢包管理：${successText}" in economy_js
    assert "私鑰已加密封存在錢包檔中" in index_html
    assert 'id="economy-spend-source-wallet"' not in index_html
    assert "預設付款錢包" not in index_html
    assert 'id="economy-wallet-count"' in index_html
    assert 'id="economy-wallet-creation-fee-source"' in index_html
    assert "收入進官方 Treasury" in index_html
    assert 'id="trading-payment-wallet"' in index_html
    assert 'id="trading-payment-wallet-note"' in index_html
    assert 'window.dispatchEvent(new CustomEvent("economy:default-spend-wallet-changed"' in economy_js
    assert 'fetchTradingJson(`/trading/dashboard${tradingSourceWalletQuery()}`)' in trading_js
    assert 'source_wallet_address: tradingDefaultSpendWalletAddress()' in trading_js
    assert 'window.addEventListener("economy:default-spend-wallet-changed"' in trading_js
    assert 'id="economy-wallet-identity-list"' in index_html
    assert "刪除後不再列入帳戶總額" in index_html
    assert 'data-wallet-delete-cold' in economy_js
    assert 'data-wallet-secret-check' in economy_js
    assert 'data-wallet-receive' in economy_js
    assert 'data-wallet-send' in economy_js
    assert 'data-wallet-default' in economy_js
    assert "此 pc0 綁定的橋接 pc1 入金地址" in economy_js
    assert "pc1 冷錢包不能直接轉到 pc0" in economy_js
    assert 'id="economy-wallet-action-panel"' in index_html
    assert 'id="economy-transfer-fee-estimate"' in economy_js
    assert 'id="economy-transfer-submit-btn"' in economy_js
    assert 'id="trading-root-spot-position-list"' in index_html
    assert 'id="trading-root-margin-position-list"' in index_html
    assert 'id="trading-root-bot-position-list"' in index_html
    assert 'id="trading-root-position-bots"' in index_html
    assert 'id="economy-root-card"' in economy_chain_page
    assert 'id="economy-admin-card"' not in economy_chain_page
    assert "PointsChain 私有鏈管理" in index_html
    assert 'id="economy-root-report-btn"' in index_html
    assert 'id="economy-rollback-ledger-uuid"' not in index_html
    assert 'id="economy-rollback-btn"' not in index_html
    assert 'id="economy-audit-list"' in index_html
    assert 'id="economy-risk-ledger-list"' in index_html
    assert 'id="economy-unsealed-transaction-list"' in index_html
    assert 'id="economy-chain-countdown"' in index_html
    assert 'id="economy-chain-loaded-at"' in index_html
    assert 'id="economy-chain-status"' in index_html
    assert 'id="economy-layer-mint-remaining"' in index_html
    assert 'id="economy-layer-active-supply"' in index_html
    assert 'id="economy-layer-circulating-supply"' in index_html
    assert 'id="economy-layer-legacy-outstanding"' not in economy_chain_page
    assert 'id="economy-layer-promo-bridged"' not in economy_chain_page
    assert 'id="economy-layer-official-balance"' not in economy_chain_page
    assert 'id="economy-layer-promo-balance"' not in economy_chain_page
    assert 'id="economy-layer-exchange-balance"' not in economy_chain_page
    assert 'id="economy-layer-burned-total"' not in economy_chain_page
    assert 'id="economy-layer-supply-formula"' in index_html
    assert 'class="economy-supply-formula" id="economy-layer-supply-formula"' in index_html
    assert 'class="economy-supply-equation-ui"' in index_html
    assert 'class="economy-formula-card"' in index_html
    assert 'class="security-log-box" id="economy-layer-supply-formula"' not in index_html
    assert 'id="economy-layer-snapshot-height"' in index_html
    assert 'id="economy-layer-derived-verify"' in index_html
    assert "<pre id=\"economy-chain-status\"" not in index_html
    assert 'id="economy-root-virtual-card"' not in index_html
    assert 'id="economy-root-virtual-total"' not in index_html
    assert 'id="economy-root-virtual-margin-value"' not in index_html
    assert "交易所內帳、現貨與借貸倉位的估值摘要" in index_html
    assert 'id="economy-manual-adjust-details"' not in index_html
    assert 'id="economy-chain-seal-details"' in index_html
    assert 'id="economy-chain-audit-details"' in index_html
    assert 'id="economy-chain-incident-details"' in index_html
    assert 'id="economy-chain-unsealed-details"' in index_html
    assert 'id="economy-chain-account-details"' not in index_html
    assert 'id="economy-chain-detail-lists"' not in index_html
    assert 'id="economy-account-query-card"' not in index_html
    assert 'id="economy-query-user-id"' not in index_html
    assert 'id="economy-account-query-btn"' not in index_html
    assert 'id="economy-query-points-balance"' not in index_html
    assert 'id="economy-wallet-sanction-card"' not in index_html
    assert 'id="economy-wallet-sanction-status"' not in index_html
    assert 'id="economy-wallet-sanction-risk"' not in index_html
    assert 'id="economy-wallet-freeze-amount"' not in index_html
    assert 'id="economy-wallet-unfreeze-amount"' not in index_html
    assert 'id="economy-wallet-sanction-btn"' not in index_html
    assert 'id="economy-query-ledger-list"' not in index_html
    assert "最近帳本明細" not in index_html
    assert "收入、支出、凍結與系統獎勵都會列在這裡" not in index_html
    assert 'id="economy-adjustment-list"' not in index_html
    assert 'id="economy-admin-card-title"' not in index_html
    assert 'id="economy-admin-card-sub"' not in index_html
    assert 'id="economy-adjust-panel"' not in index_html
    assert '<select id="economy-adjust-user-id">' not in index_html
    assert '<input type="number" id="economy-adjust-user-id"' not in index_html
    assert 'id="economy-adjust-currency"' not in index_html
    assert "全站積分" in index_html
    assert "近期未封交易 hash" in index_html
    assert "手動加減分與待審核" not in index_html
    assert "積分錢包" in index_html
    assert "積分交易所" in index_html
    assert "/js/55-economy.js?v=" in index_html
    # Match any cache-bust version — Codex bumps these for browser
    # cache invalidation, the test should not break on each bump.
    assert "/js/50-admin.js?v=" in index_html
    assert "/js/56-trading.js?v=" in index_html
    assert 'id="economy-recovery-card"' in index_html
    assert 'id="economy-recovery-auto-handle-btn"' in index_html
    assert 'id="economy-backup-btn"' not in index_html
    assert 'id="economy-recovery-approve-btn"' not in index_html
    assert "function renderEconomyRecovery" in economy_js
    assert 'fetchEconomyJson("/root/points/chain/backups"' not in economy_js
    assert 'fetchEconomyJson("/root/points/chain/recovery/auto-handle"' in economy_js
    assert "異常鏈處理方案已排入背景任務" in economy_js
    assert 'fetchEconomyJson("/root/points/chain/recovery/approve"' not in economy_js
    assert "async function autoHandlePointsChainRecovery()" in economy_js
    assert '["economy-recovery-auto-handle-btn", autoHandlePointsChainRecovery]' in economy_js
    assert "/js/90-bootstrap.js?v=" in index_html
    assert 'const rootMode = currentUser === "root";' in economy_js
    assert 'return economyChainEnabled() && economyGovernanceCanManage();' in economy_js
    assert 'rootWalletManagementCard.style.display = economyGovernanceCanManage() && chainFeatureOn ? "" : "none";' in economy_js
    assert 'const canManagePoints = canManageEconomyPoints();' not in economy_js
    assert 'adminCard.style.display = "none"' not in economy_js
    assert 'economy-admin-ledger-list' not in economy_js
    assert 'economy-pending-list' not in economy_js
    assert "function setEconomyActivePage(page" in economy_js
    assert "function economyPositionsAvailable()" not in economy_js
    assert 'const positionsAvailable = false;' not in economy_js
    assert 'positionsTab.style.display = "none";' not in economy_js
    assert 'chainTab.textContent = rootMode ? "積分私有鏈" : "官方錢包管理";' in economy_js
    assert 'if (nextPage === "positions") title.textContent = "倉位管理";' not in economy_js
    assert 'else if (nextPage === "explorer") title.textContent = "鏈上瀏覽器";' in economy_js
    assert 'else if (nextPage === "funding-pools") title.textContent = "資金池管理";' not in economy_js
    assert 'else if (nextPage === "all-positions") title.textContent = "全用戶倉位管理";' not in economy_js
    assert 'else if (!rootMode) title.textContent = nextPage === "chain" ? "官方錢包管理" : "積分錢包";' in economy_js
    assert 'else title.textContent = nextPage === "chain" ? "積分私有鏈" : "官方錢包管理";' in economy_js
    assert 'fetchEconomyJson("/root/points/report")' in economy_js
    assert 'path.startsWith("/api/") ? path : API + path' in economy_js
    assert 'fetchTradingJson("/root/trading/sitewide/refresh"' in trading_js
    assert 'fetchTradingJson("/root/trading/sitewide/pools", { allowMissingSnapshot: true })' in trading_js
    assert 'fetchTradingJson("/root/trading/sitewide/user-positions", { allowMissingSnapshot: true })' in trading_js
    assert 'const shouldLoadRootTrading = rootMode && chainFeatureOn && ["funding-pools", "all-positions"].includes(economyActivePage);' not in economy_js
    assert "PointsChain EXCHANGE" not in economy_js
    assert "未綁定地址" in economy_js
    assert "function refreshEconomyRootTradingSnapshots" not in economy_js
    assert 'loadEconomyRootTradingReadOnly({ refreshSnapshot: true' not in economy_js
    assert 'balanceTab.textContent = rootMode ? "官方錢包管理" : "積分餘額";' in economy_js
    assert "function renderEconomyRootBalanceSummary" not in economy_js
    assert "function renderEconomyLayerSummary" in economy_js
    assert "function economyFormulaCard" in economy_js
    assert "function economyFormulaOperator" in economy_js
    assert 'setEconomyText("economy-layer-mint-remaining"' in economy_js
    assert 'setEconomyText("economy-layer-active-supply"' in economy_js
    assert 'setEconomyText("economy-layer-legacy-outstanding"' not in economy_js
    assert '"economy-layer-supply-equation"' not in economy_js
    assert '"economy-root-wallet-management-card"' in economy_js
    assert 'setEconomyText("economy-root-wallet-mint-address"' in economy_js
    assert 'setEconomyText("economy-root-wallet-official-balance"' in economy_js
    assert 'setEconomyText("economy-root-wallet-promo-status"' in economy_js
    assert '["economy-root-wallet-refresh-btn", refreshEconomyOfficialWalletManagement]' in economy_js
    assert '["economy-root-official-grant-btn", sendEconomyRootOfficialGrant]' in economy_js
    assert 'fetchEconomyJson("/admin/points/governance/treasury-transfer"' in economy_js
    assert 'fetchEconomyJson("/root/points/official-wallet/grant"' not in economy_js
    assert 'fetchEconomyJson("/points/governance/proposals?limit=50")' in economy_js
    assert 'fetchEconomyJson("/points/governance/address-risk"' not in economy_js
    assert 'fetchEconomyJson("/points/governance/wallet-freeze"' not in economy_js
    assert 'fetchEconomyJson("/admin/points/governance/recovery-branch"' in economy_js
    assert 'fetchEconomyJson(`/points/governance/proposals/${encodeURIComponent(proposalUuid)}/vote`' in economy_js
    assert 'fetchEconomyJson(`/admin/points/governance/proposals/${encodeURIComponent(proposalUuid)}/execute`' in economy_js
    assert "economy-governance-emergency-create-details" in index_html
    assert "function renderEconomyTreasuryAnalysis" in economy_js
    assert "站內服務費收入" in economy_js
    assert "各功能服務費收入" in economy_js
    assert "計費單價請到系統管理" in economy_js
    assert "服務費定價擬合" not in economy_js
    assert "actual_chain_transfer_destination_fund_key" not in economy_js
    assert '["economy-treasury-analysis-refresh-btn", () => loadEconomyTreasurySignerCenter()]' in economy_js
    assert "不會刪改舊 ledger" in economy_js
    assert 'if (value === "official_fund_transfer") return "官方基金調撥";' in economy_js
    assert 'json.msg || json.message || json.error || `HTTP ${res.status}`' in economy_js
    assert '官方 Treasury 撥款提案送出失敗' in economy_js
    assert "function renderEconomySpendWalletOptions" not in economy_js
    assert "function renderEconomyWalletCreationFeeOptions" in economy_js
    assert "function economyWalletCreationFeePayload" in economy_js
    assert "wallet_creation_fee" in economy_js
    assert "function renderTradingPaymentWalletOptions" in trading_js
    assert "trading-payment-wallet" in trading_js
    assert 'if (currentUser === "root") return "";' in trading_js
    assert 'root 下單固定使用 root 模擬資金，不可選用站內託管錢包或官方財庫。' in trading_js
    assert 'const ECONOMY_SPEND_WALLET_STORAGE_KEY = "hackme_web:economy:default_spend_wallet";' in economy_js
    assert "function readEconomyDefaultSpendWalletAddress" in economy_js
    assert "source_wallet_address: sourceWallet" in economy_js
    assert 'window.prompt("請確認本次付款錢包地址；可改用其他錢包。"' in economy_js
    assert "economy-wallet-create-card" in economy_js
    assert "economy-wallet-initial-grant" not in economy_js
    assert "初始配點已入帳" not in economy_js
    assert "綁定錢包後由官方基金匯入" not in economy_js
    assert "economy-wallet-signup-bonus" not in economy_js
    assert "let economyColdWalletDraft = null;" in economy_js
    assert "let economyColdWalletBindCandidate = null;" in economy_js
    assert "let economyDocumentEventsBound = false;" in economy_js
    assert "冷錢包只建立草稿，尚未匯入或綁定" in economy_js
    assert "function destroyEconomyColdWalletSecrets" in economy_js
    assert '"economy-wallet-generated-address"' in economy_js
    assert '"economy-wallet-generated-file-name"' in economy_js
    assert '"economy-wallet-generated-trade-password"' in economy_js
    assert '"economy-wallet-file-password"' in economy_js
    assert '"economy-wallet-file-input"' in economy_js
    assert "function economyLoadColdWalletBackup" not in economy_js
    assert "function economyNotifyFailure" in economy_js
    assert 'el.classList.toggle("show", Boolean(text));' in economy_js
    assert 'el.classList.toggle("err", Boolean(text) && !ok);' in economy_js
    assert 'el.setAttribute("role", ok ? "status" : "alert");' in economy_js
    assert "function selectGeneratedColdWalletForImport" in economy_js
    assert 'id="economy-wallet-generated-selection-status"' in index_html
    assert '["economy-wallet-use-generated-cold-btn", selectGeneratedColdWalletForImport]' in economy_js
    assert 'const ECONOMY_COLD_BACKUP_PREFIX = "pcw1.p256";' not in economy_js
    assert 'const ECONOMY_COLD_WALLET_KDF_ITERATIONS = 600000;' in economy_js
    assert 'const ECONOMY_COLD_UNLOCK_MNEMONIC_WORD_COUNT = 12;' in economy_js
    assert 'const ECONOMY_COLD_UNLOCK_QUIZ_COUNT = 4;' in economy_js
    assert 'const ECONOMY_COLD_WALLET_FILE_FORMAT = "hackme-pcw1-encrypted-wallet";' in economy_js
    assert "function economyEncryptColdWalletFile" in economy_js
    assert "function economyDecryptColdWalletFilePayload" in economy_js
    assert "function economyLoadEncryptedColdWalletFile" in economy_js
    assert "function economyPromptColdWalletForSigning" in economy_js
    assert "function economyCompactColdWalletBackup" not in economy_js
    assert "function economyParseColdWalletBackup" not in economy_js
    assert "function checkEconomyColdWalletMnemonicQuiz" in economy_js
    assert "function economyBuildColdWalletMnemonicQuiz" in economy_js
    assert "function startEconomyColdWalletMnemonicQuiz" in economy_js
    assert '["economy-wallet-start-mnemonic-quiz-btn", startEconomyColdWalletMnemonicQuiz]' in economy_js
    assert 'secretInput.value = "";' in economy_js
    assert "本次冷錢包草稿已作廢" in economy_js
    assert "function economyBuildTransferSignature" in economy_js
    assert "function economyBuildGovernanceMultisigSignature" in economy_js
    assert "economyWalletSignTransfer" not in economy_js
    assert "points_wallet_transfer" in economy_js
    assert "function economyBuildServiceFeeSignature" in economy_js
    assert "points_service_fee_payment" in economy_js
    assert "服務費已凍結" not in economy_js
    assert "批次鏈上扣款" not in economy_js
    assert "冷錢包直接服務付款已停用" in economy_js
    assert "錢包檔 JSON 或舊版 pcw1 備份碼以本機簽署" not in economy_js
    assert "請貼上冷錢包檔 JSON" not in economy_js
    assert "請貼上有效的加密冷錢包檔 JSON" not in economy_js
    assert "function economyPickColdWalletFileForSigning" in economy_js
    assert 'id="economy-wallet-signing-file-input"' in index_html
    assert 'input.type = "file";' in economy_js
    assert "await economyReadTextFile(file)" in economy_js
    assert "function economyVerifyColdWalletSigningSession" in economy_js
    assert "economyRememberColdWalletSigningSession" in economy_js
    assert "本機已暫時解鎖此冷錢包" in economy_js
    assert "不需要輸入完整解鎖助記詞" in economy_js
    assert "首次解鎖或本機簽署會話逾期" in economy_js
    assert "錢包檔與冷錢包解鎖助記詞只在可信裝置使用" in economy_js
    assert "showAppToast(`${label}：${message}`" in economy_js
    assert "false,\n    [\"sign\"]" in economy_js
    assert "privateJwk.d = \"\";" in economy_js
    assert "等待冷錢包本機簽署" in economy_js
    assert "冷錢包檔地址與付款錢包不一致" in economy_js
    assert 'data-dispute-amount="${sanitize(String(tx.amount_points || tx.amount || 0))}"' in economy_js
    assert 'data-dispute-amount="${sanitize(String(row.claimed_amount_points || 0))}"' in economy_js
    assert "signature," in economy_js
    assert "const tradePassword = await economyDerivedColdWalletTradePassword(privateJwk, address);" in economy_js
    assert "const walletFile = await economyEncryptColdWalletFile" in economy_js
    assert "新建冷錢包不會顯示這種備份碼" not in index_html
    assert "記憶詞考試" in index_html
    assert 'id="economy-wallet-start-mnemonic-quiz-btn"' in index_html
    assert "確認已保存，隱藏助記詞並開始考試" in index_html
    assert "pcp1 只是解鎖助記詞前綴，不是第一個 pc1 冷錢包" in index_html
    assert "官方 pc0 熱錢包也不計入第一個 pc1 冷錢包" in index_html
    assert "答錯會作廢本次草稿並要求重來" in index_html
    assert 'id="economy-wallet-mnemonic-check-btn"' in index_html
    assert '["economy-wallet-mnemonic-check-btn", checkEconomyColdWalletMnemonicQuiz]' in economy_js
    assert "我已下載錢包檔並離線保存冷錢包解鎖助記詞" in index_html
    assert "持有錢包檔與冷錢包解鎖助記詞者可恢復該地址" in index_html
    assert "伺服器未保存用戶冷錢包檔或冷錢包解鎖助記詞" in economy_js
    assert 'if ($("economy-wallet-private-key")) $("economy-wallet-private-key").value = JSON.stringify(privateJwk, null, 2);' not in economy_js
    assert "economyColdWalletDraft = { privateKey" not in economy_js
    assert "economyColdWalletDraft.privateJwk" not in economy_js
    assert "殘差舊算法" not in economy_js
    assert 'const formulaBalanced = gap === 0;' in economy_js
    assert 'formulaBalanced ? "Settlement invariant 正常" : "需查帳"' in economy_js
    assert "economyColdWalletDraft.backupCode" not in economy_js
    assert "Legacy 帳本身份" in economy_js
    assert "Legacy 帳本 ID" in economy_js
    assert "舊帳本公開識別碼" in economy_js
    assert '"economy-layer-supply-formula"' in economy_js
    assert "多帳本結算控制平面" in economy_js
    assert "PC1 Canonical Reserve" in economy_js
    assert "PC0 Wrapped Operational Supply" in economy_js
    assert "Bridge Settlement / Pending Isolation" in economy_js
    assert "Financial Reconciliation" in economy_js
    assert "鏈上/橋外在外流通" not in economy_js
    assert "pc0出站" not in economy_js
    assert "入金入站" not in economy_js
    assert "帳本/事件差" not in economy_js
    assert "PC1 Reserve 對帳差" in economy_js
    assert "用戶 PC0 站內流通" in economy_js
    assert "member_internal_circulating_points" in economy_js
    assert "root/其他 PC0 站內餘額" in economy_js
    assert "交易所應收本金" in economy_js
    assert "扣留式費用對帳" in economy_js
    assert "未分類橋接流量差" in economy_js
    assert "ledgerEconomyGap || unexplainedFlowGap" in economy_js
    assert "ledgerEconomyGap || flowGap" not in economy_js
    assert "pc0 服務費收入" in economy_js
    assert "最近服務費收入帳本" in economy_js
    assert "最高管理者" in economy_js
    assert "簽署權重" in economy_js
    assert "super_admin · weight" not in economy_js
    assert "manager · weight" not in economy_js
    assert "站內服務待結算" not in economy_js
    assert "下一次批次差額" not in economy_js
    assert "最近實際鏈上批次轉帳" not in economy_js
    assert "economy-root-balance-refresh-btn" not in economy_js
    assert 'setEconomyText("economy-layer-derived-verify"' in economy_js
    assert "function renderEconomyRootFundingPools" not in economy_js
    assert "function renderEconomyRootAllPositions" not in economy_js
    assert "function renderTradingRootSitewidePools" in trading_js
    assert "function renderTradingRootSitewidePositions" in trading_js
    assert 'setEconomyText("economy-root-position-users"' not in economy_js
    assert '"economy-root-wallet-position-list"' not in economy_js
    assert 'setEconomyText("economy-root-position-bots"' not in economy_js
    assert 'renderEconomyRootList(botRows, "economy-root-bot-position-list"' not in economy_js
    assert 'tradingSetText("trading-root-position-bots"' in trading_js
    assert 'tradingRootList(botRows, "trading-root-bot-position-list"' in trading_js
    assert "startEconomyBlockCountdown" in economy_js
    assert "function canManageEconomyPoints()" in economy_js
    assert "function relocateEconomyOfficialWalletCard(rootMode)" in economy_js
    assert 'chainPage.insertBefore(card, managerCard || chainPage.firstChild);' in economy_js
    assert "function setEconomyRootLayout(rootMode)" in economy_js
    assert "function economyChainEnabled()" in economy_js
    assert 'siteConfig.feature_points_chain_enabled !== false' in economy_js
    assert 'return economyChainEnabled() && economyGovernanceCanManage();' in economy_js
    assert 'id="economy-manager-points-management-card"' in index_html
    assert 'id="economy-manager-points-official-balance"' in index_html
    assert 'id="economy-manager-points-pending-list"' in index_html
    assert "官方錢包管理" in index_html
    assert "manager 以上可查看官方基金錢包" in index_html
    assert 'const managerCard = $("economy-manager-points-management-card");' in economy_js
    assert 'const managerMode = canManage && currentUser !== "root";' in economy_js
    assert '["economy-manager-points-open-wallets-btn", () => {' in economy_js
    assert '$("economy-root-wallet-management-card")?.scrollIntoView?.({ block: "start", behavior: "smooth" });' in economy_js
    assert 'id="edit-user-governance-disposition-fields"' in index_html
    assert 'id="edit-user-restriction-features"' in index_html
    assert 'id="edit-user-fine-amount"' in index_html
    assert "payload.restriction_features = restrictionFeatures" in auth_js
    assert "payload.fine_amount_points = fineAmount" in auth_js
    assert "最後更新" in economy_js
    assert "bindEconomyInlineEvents" in economy_js
    assert "function renderEconomyExplorerResult" in economy_js
    assert "function economyExplorerBridgeCard" in economy_js
    assert "function economyExplorerAuditCard" in economy_js
    assert "function setEconomyExplorerLayer" in economy_js
    assert 'fetchEconomyJson(`/points/explorer/bridge/${encodeURIComponent(value)}`)' in economy_js
    assert 'fetchEconomyJson("/root/points/financial-invariants")' in economy_js
    assert "Audit invariant 已排入背景任務" in economy_js
    assert "function renderEconomyGovernance" in economy_js
    assert "function createGovernanceAddressRiskProposal" not in economy_js
    assert "function createGovernanceWalletFreezeProposal" not in economy_js
    assert "function createGovernanceRecoveryBranchProposal" in economy_js
    assert 'fetchEconomyJson(`/points/explorer/search?q=${encodeURIComponent(value)}&limit=25`)' in economy_js
    assert 'fetchEconomyJson(`/points/explorer/fee-estimate?fee_points=${encodeURIComponent(String(fee))}`)' in economy_js
    assert 'fetchEconomyJson("/points/explorer/accelerate"' in economy_js
    assert "priority_fee_diminishing_ratio_v2" in economy_js or "feeReferencePoints" in economy_js
    assert "economy-explorer-accelerate-estimate" in economy_js
    assert "提高費用並加速" in economy_js
    assert "已送出鏈上加速費用，Proved" in economy_js
    assert "加速費" in economy_js
    assert 'fetchEconomyJson("/points/transactions/submit"' in economy_js
    assert 'fetchEconomyJson("/points/transactions?limit=50&compact=1")' in economy_js
    assert "function renderEconomyTransactions" in economy_js
    assert "function loadEconomyTransactions" in economy_js
    assert '["economy-transactions-refresh-btn", loadEconomyTransactions]' in economy_js
    assert '["economy-governance-refresh-btn", () => loadEconomyGovernance()]' in economy_js
    assert '["economy-governance-scam-create-btn", createGovernanceAddressRiskProposal]' not in economy_js
    assert '["economy-governance-freeze-create-btn", createGovernanceWalletFreezeProposal]' not in economy_js
    assert '["economy-governance-branch-create-btn", createGovernanceRecoveryBranchProposal]' in economy_js
    assert 'requestedPage === "transactions" && !rootMode' not in economy_js
    assert 'transactionsTab.style.display = rootMode ? "none" : ""' not in economy_js
    assert 'if (!currentUser || currentUser === "root")' not in economy_js
    assert 'if (value === "official_outgoing") return "官方 Treasury 支出";' in economy_js
    assert "function economyTransactionFinalityIsInternal" in economy_js
    assert "Number(finality.target_proved_count ?? 20) === 0" in economy_js
    assert "function economyTransactionProvedText" in economy_js
    assert 'if (economyTransactionFinalityIsInternal(finality)) return "免 Proved";' in economy_js
    assert "function economyTransactionAmountText" in economy_js
    assert 'if (tx.direction === "official_outgoing") return `Treasury 支出 ${formatEconomyPointsValue(amount + fee)}`;' in economy_js
    assert "Pending 不會讓收款錢包入帳" in economy_js
    assert "transaction_hash" in economy_js
    assert 'setEconomyActivePage("transactions")' in economy_js
    assert "function submitEconomyWalletTransfer()" in economy_js
    assert "const successMessage = immediate" in economy_js
    assert "pc0 站內互轉已完成" in economy_js
    assert "economySetMsg(visibleMessage, !warningSuffix);" in economy_js
    assert "成交前收款方不會入帳" in economy_js
    assert "設定自動發放交易免鏈上費用" in economy_js
    assert "BURN 銷毀錢包" in economy_js
    assert "下一個 Proved 約" in economy_js
    assert "startEconomyExplorerCountdown" in economy_js
    assert "data-finality-next-text" in economy_js
    assert "Transaction Hash" in economy_js
    assert "Transaction Fee" in economy_js
    assert "Gas Price" in economy_js
    assert "Input Data" in economy_js
    assert "economyAutoRefreshTimer" in economy_js
    assert 'currentModuleTab !== "economy"' in economy_js
    assert "function economyDashboardRefreshMs()" in economy_js
    assert "}, economyDashboardRefreshMs())" in economy_js
    assert "economy-unsealed-transaction-list" in economy_js
    assert "safeReport.unsealed_transactions" in economy_js
    assert "economy-adjustment-list" not in economy_js
    assert 'adminLedgerList.style.display = rootMode ? "none" : ""' not in economy_js
    assert 'if (adjustPanel) adjustPanel.style.display = rootMode ? "" : "none";' not in economy_js
    assert 'adminTitle.textContent = "手動加減分";' not in economy_js
    assert "加減分歷史統一在下方明細查看" not in economy_js
    assert "function formatEconomyLedgerAction" in economy_js
    assert "function formatEconomyLedgerAmount" in economy_js
    assert "遊戲每日任務獎勵" in economy_js
    assert "formatEconomyLedgerSource" in economy_js
    assert "只有 root 可以手動調整積分" not in economy_js
    assert 'fetchEconomyJson("/admin/users")' not in economy_js
    assert "function renderEconomyAdjustUserOptions" not in economy_js
    assert "async function loadEconomyAccountLookup()" not in economy_js
    assert "async function downloadCsvEndpoint" in economy_js
    assert "apiFetch(API + path" in economy_js
    assert "window.location.href = API + path" not in economy_js
    assert "async function sanctionEconomyWallet()" not in economy_js
    assert "renderEconomyAccountLookup" not in economy_js
    assert "formatEconomyVerificationSummary" in economy_js
    assert "formatEconomyRecoveryResult" in economy_js
    assert "PointsChain 已完成異常處理檢查" in economy_js
    assert "PointsChain 已恢復並完成驗證" not in economy_js
    assert "const resultMessage = formatEconomyRecoveryResult(json);" in economy_js
    assert "await loadEconomyDashboard();\n    if (json.action === \"verified_clean\")" in economy_js
    assert "economySetMsg(json.msg || resultMessage || \"異常鏈處理完成\", !!json.ok);" in economy_js
    assert "setEconomyChainStatus" in economy_js
    assert "JSON.stringify(json.report?.verification" not in economy_js
    assert "JSON.stringify(json.verification" not in economy_js
    assert '/admin/points/wallets/${encodeURIComponent(userId)}' not in economy_js
    assert '/root/points/wallets/${encodeURIComponent(userId)}/sanction' not in economy_js
    assert '["economy-wallet-sanction-btn", sanctionEconomyWallet]' not in economy_js
    assert "function deleteEconomyColdWallet" in economy_js
    assert "const address = String(addressOverride || \"\").trim();" in economy_js
    assert "請先選擇要刪除的冷錢包" in economy_js
    assert "JSON.stringify({ address, reason: \"user_deleted_cold_wallet\" })" in economy_js
    assert 'fetchEconomyJson("/points/wallet/onboarding", {\n      method: "DELETE"' in economy_js
    assert '["economy-wallet-delete-cold-btn", deleteEconomyColdWallet]' not in economy_js
    assert "function startColdWalletImport" in economy_js
    assert '["economy-wallet-import-cold-btn", startColdWalletImport]' in economy_js
    assert "請貼上要匯入或恢復的冷錢包備份碼" not in economy_js
    assert "function economyVisibleWallets" in economy_js
    assert "addWallet(onboarding.wallet);" in economy_js
    assert "const visibleWallets = economyVisibleWallets(onboarding);" in economy_js
    assert "deleteColdBtn.disabled = !coldWallets.length;" not in economy_js
    assert "目前沒有可刪冷錢包" not in economy_js
    assert "function economyInlineMsg" in economy_js
    assert "showAppToast(`${fallbackLabel}：${message}`" in economy_js
    assert "function economyWarningSuffix" in economy_js
    assert "notification_delivery_failed" in economy_js
    assert "錢包資料部分讀取失敗" in economy_js
    assert "/js/55-economy.js?v=" in index_html
    assert 'wallet?.wallet_type === "official_hot"' not in economy_js
    assert "官方熱錢包由系統託管，不能刪除" not in economy_js
    assert "economy-account-query-btn" not in economy_js
    assert "會員讀取失敗" not in economy_js
    assert "請先選擇要查詢的會員" not in economy_js
    assert "請先選擇要調整的會員" not in economy_js
    assert "economy-adjust-currency" not in economy_js
    assert 'return "點";' in economy_js
    assert 'async function rollbackEconomyLedger()' not in economy_js
    assert "/rollback" not in economy_js
    assert "bindEconomyInlineEvents" in bootstrap_js
    assert 'id="tab-settings-billing"' not in index_html
    assert 'id="sec-settings-billing"' in index_html
    assert 'id="economy-root-pricing-settings-card"' in index_html
    assert 'id="economy-pricing-settings-slot"' in index_html
    assert 'id="tab-settings-trading"' not in index_html
    assert 'id="sec-settings-trading"' in index_html
    assert 'id="trading-settings-page"' in index_html
    assert 'id="trading-settings-slot"' in index_html
    assert 'id="trading-root-settings-page-btn"' in index_html
    assert "function openTradingSettingsPage()" in trading_js
    assert 'action: "trading:settings"' in core_js
    assert 'id="root-catalog-item-key"' in index_html
    assert 'id="root-catalog-storage-gb"' in index_html
    assert 'id="root-catalog-save-btn"' in index_html
    assert 'id="root-service-fee-quick-pricing-list"' in index_html
    assert "這裡是服務計價的實際設定區" in index_html
    assert "本月收入統計請到積分錢包" in index_html
    assert "ROOT_SERVICE_FEE_PRICING_PRESETS" in admin_js
    assert "saveRootServiceFeePricingPreset" in admin_js
    assert "video_publish_basic" in admin_js
    assert "marketplace_listing_fee" in admin_js
    assert 'id="root-trading-enabled"' in index_html
    assert 'id="root-trading-borrowing-enabled"' in index_html
    assert 'id="root-trading-borrowing-enabled" checked' in index_html
    assert 'id="root-trading-borrow-apr-btc-eth"' in index_html
    assert 'id="root-trading-borrow-apr-usdt-points"' in index_html
    assert 'id="root-trading-borrow-interest-interval-hours"' in index_html
    assert 'id="root-trading-borrow-interest-minimum-hours"' in index_html
    assert 'id="root-trading-grid-fee-discount-percent"' in index_html
    assert 'id="root-trading-margin-long-financing-percent"' in index_html
    assert 'id="root-trading-margin-max-pool-utilization-percent"' in index_html
    assert 'id="root-trading-short-collateral-percent"' in index_html
    assert 'id="root-trading-exchange-liability-limit-points"' in index_html
    assert 'id="root-trading-exchange-liability-grace-minutes"' in index_html
    assert 'id="root-trading-profit-settlement-interval-minutes"' in index_html
    assert "融資九成" in index_html
    assert "借券六成" in index_html
    assert "融資可貸比例（%）" in index_html
    assert "借貸基金使用率上限（%）" in index_html
    assert "借券原始保證金比例（%）" in index_html
    assert "交易所暫時負債上限（點）" in index_html
    assert "盈利結算週期（分鐘）" in index_html
    assert "維持保證金比例（%）" in index_html
    assert 'id="root-trading-price-source"' in index_html
    assert 'value="fused_weighted"' in index_html
    assert 'id="root-trading-price-fusion-mode"' in index_html
    assert 'id="root-trading-price-fusion-depth-band-percent"' in index_html
    assert 'id="root-trading-price-fusion-depth-levels"' in index_html
    assert 'id="root-trading-price-fusion-min-coverage-percent"' in index_html
    assert 'id="root-trading-price-fusion-max-provider-weight"' in index_html
    assert 'id="root-trading-price-fusion-min-provider-count"' in index_html
    assert 'id="root-trading-price-fusion-weights"' in index_html
    assert 'class="trading-fusion-weight-list"' in index_html
    assert 'id="root-trading-price-fusion-market"' in index_html
    assert 'id="root-trading-price-fusion-refresh-btn"' in index_html
    assert 'id="root-trading-price-fusion-summary"' in index_html
    assert 'id="root-trading-price-fusion-provider-list"' in index_html
    assert 'id="root-trading-price-fusion-excluded-list"' in index_html
    assert "價格來源降級" in admin_js
    assert 'id="root-trading-max-price-staleness"' in index_html
    assert 'id="root-trading-liquidation-enabled"' in index_html
    assert 'id="root-trading-maintenance-percent"' in index_html
    assert 'id="root-trading-reserved-panel" style="display:none;margin-top:.75rem;"' in index_html
    assert 'id="root-trading-futures-enabled"' in index_html
    assert 'id="root-trading-pvp-enabled"' in index_html
    assert "合約 / PVP 預留功能" in index_html
    assert 'id="root-trading-reserve-pool"' in index_html
    assert 'id="root-trading-btc-trade-enabled"' in index_html
    assert 'id="root-trading-btc-trade-repo"' in index_html
    assert 'id="root-trading-btc-trade-branch"' in index_html
    assert 'id="root-trading-btc-trade-path"' in index_html
    assert 'id="root-trading-btc-trade-check-btn"' in index_html
    assert 'id="root-trading-btc-trade-setup-btn"' in index_html
    assert "BTC_trade 專案資料夾" in index_html
    assert 'id="root-trading-market-settings"' in index_html
    assert 'id="root-trading-settings-save-btn"' in index_html
    assert "function loadRootEconomyCatalog()" in admin_js
    assert 'apiFetch(API + "/root/economy/catalog"' in admin_js
    assert "saveRootEconomyCatalogItem" in admin_js
    assert "function loadRootTradingSettings()" in admin_js
    assert "function saveRootTradingSettings()" in admin_js
    assert "function renderRootTradingFusionWeightInputs" in admin_js
    assert "trading-fusion-weight-chip" in admin_js
    assert "trading-fusion-weight-unit" in admin_js
    assert "top 10 / 50 / 100 / 200 / 500 / 1000" in index_html
    assert "coverage truncated" in admin_js
    assert "唯一合格來源" in admin_js
    assert "reference 來源" in admin_js
    assert "reference 可用來源" in admin_js
    assert "請求市場" in admin_js
    assert "內部市場" in admin_js
    assert "目前不是正常 fused price" in admin_js
    assert "reference 占比" in admin_js
    assert "風控級占比" in admin_js
    assert "depth score" in admin_js
    assert "density" in admin_js
    assert "資料截斷，不代表該交易所真實深度不足" in admin_js
    assert "provider depth limit reached" in admin_js
    assert 'id="root-trading-price-stream-ws-enabled"' in index_html
    assert 'id="root-trading-price-stream-ws-stale-seconds"' in index_html
    assert "price_stream_ws_enabled" in admin_js
    assert "price_stream_ws_stale_seconds" in admin_js
    assert "provider input" in admin_js
    assert 'fallback ${transportState.fallback ? "HTTP polling" : "no"}' in admin_js
    assert "last update" in admin_js
    assert "transportState" in admin_js
    assert "最低覆蓋門檻（%）" in index_html
    assert "最少可用來源數" in index_html
    assert "price_fusion_depth_band_percent" in admin_js
    assert "price_fusion_min_orderbook_coverage_percent" in admin_js
    assert "max_single_provider_weight_percent" in admin_js
    assert "price_fusion_depth_levels" in admin_js
    assert "price_fusion_min_provider_count" in admin_js
    assert "function renderRootTradingPriceFusionMarketOptions" in admin_js
    assert "function renderRootTradingPriceFusionStatus" in admin_js
    assert "function loadRootTradingPriceFusionStatus" in admin_js
    assert "function toggleRootTradingPriceFusionControls" in admin_js
    assert "collectRootTradingFusionWeights" in admin_js
    assert "adminInputPercent" in admin_js
    assert "adminFormatPercent" in admin_js
    assert 'apiFetch(API + "/root/trading/settings"' in admin_js
    assert 'apiFetch(API + `/root/trading/price-fusion-status?market_symbol=${encodeURIComponent(selectedMarket)}`' in admin_js
    assert "parseRootTradingSettingsResponse" in admin_js
    assert "交易所參數 API 找不到" in admin_js
    assert "交易所參數儲存中" in admin_js
    assert 'id="root-trading-borrow-apr-btc-eth"' in index_html
    assert 'id="root-trading-borrow-apr-usdt-points"' in index_html
    assert 'id="root-trading-borrow-interest-interval-hours"' in index_html
    assert 'id="root-trading-borrow-interest-minimum-hours"' in index_html
    assert 'id="root-trading-grid-fee-discount-percent"' in index_html
    assert 'id="trading-funding-pool-rate-btc-eth"' in index_html
    assert 'id="trading-funding-pool-rate-usdt-points"' in index_html
    assert "borrow_apr_btc_eth_percent" in admin_js
    assert "borrow_apr_usdt_points_percent" in admin_js
    assert "borrow_interest_interval_hours" in admin_js
    assert "borrow_interest_minimum_hours" in admin_js
    assert "grid_fee_discount_percent" in admin_js
    assert "margin_long_financing_percent" in admin_js
    assert "margin_max_pool_utilization_percent" in admin_js
    assert "short_collateral_percent" in admin_js
    assert "exchange_liability_limit_points" in admin_js
    assert "exchange_liability_grace_minutes" in admin_js
    assert "profit_settlement_interval_minutes" in admin_js
    assert "price_source" in admin_js
    assert "max_price_staleness_seconds" in admin_js
    assert "btc_trade_enabled" in admin_js
    assert "btc_trade_repo_url" in admin_js
    assert "btc_trade_branch" in admin_js
    assert "btc_trade_project_dir" in admin_js
    assert "root-trading-btc-trade-start-btn" in index_html
    assert "/root/trading/btc-trade/start" in admin_js
    assert "/root/trading/btc-trade/start-status" in admin_js
    assert "pollRootBtcTradeStartJob" in admin_js
    assert "一鍵啟動預測" in index_html
    assert "資料是否過期" in index_html
    assert "自動下載/更新、安裝依賴" in index_html
    assert 'body: JSON.stringify({ project_dir: projectDir, repo_url: repoUrl, branch, timeframe: "4h" })' in admin_js
    assert "function checkRootBtcTradeStatus" in admin_js
    assert 'apiFetch(API + "/root/trading/btc-trade/check"' in admin_js
    assert "function setupRootBtcTrade" in admin_js
    assert 'apiFetch(API + "/root/trading/btc-trade/setup"' in admin_js
    assert "margin_liquidation_enabled" in admin_js
    assert "margin_maintenance_percent" in admin_js
    assert "collectRootTradingMarketSettings" in admin_js
    assert 'if (tab === "billing") {' in admin_js
    assert 'switchSettingsSection("billing")' not in bootstrap_js
    assert 'switchSettingsSection("trading")' not in bootstrap_js
    assert "loadRootTradingSettings" in bootstrap_js
    assert "checkRootBtcTradeStatus" in bootstrap_js
    assert "setupRootBtcTrade" in bootstrap_js
    billing_section = index_html.split('id="sec-settings-billing"', 1)[1].split('id="sec-settings-trading"', 1)[0]
    trading_settings_section = index_html.split('id="sec-settings-trading"', 1)[1].split('id="sec-settings-drive"', 1)[0]
    assert 'id="root-trading-enabled"' not in billing_section
    assert "服務扣點與容量商品" in billing_section
    assert "交易所設定已獨立成單獨分頁" in trading_settings_section
    assert "基本開關與風控" in trading_settings_section
    assert "價格來源與融合比例" in trading_settings_section
    assert "不必剛好加總 100，系統會自動正規化" in trading_settings_section
    assert "前 N 檔" in trading_settings_section
    assert "中間價附近 ±1% 範圍內的買賣盤名目量" in trading_settings_section
    assert "較弱一側作為 depth score" in trading_settings_section
    assert "不建議單獨作為強平、機器人或實際成交的唯一依據" in trading_settings_section
    assert "機器人掃描與稽核" in trading_settings_section
    assert "BTC_trade 信號整合" in trading_settings_section
    assert "各交易對參數" in trading_settings_section
    assert 'id="trading-root-card"' in trading_settings_section
    assert "交易所基金與市場營運" in trading_settings_section
    assert "交易所基金撥補需改走官方財庫治理提案" in trading_settings_section
    assert "資金池" not in trading_settings_section
    assert 'id="trading-root-inline-msg"' in trading_settings_section
    assert 'id="trading-limit-match-btn"' in trading_settings_section
    assert 'id="trading-liquidation-scan-btn"' in trading_settings_section
    assert 'id="trading-reserve-allocate-btn"' not in trading_settings_section
    assert 'id="trading-reserve-source-user-id"' not in trading_settings_section
    assert "/root/trading/reserve/allocate" not in trading_js
    assert "function allocateTradingReserve" not in trading_js
    assert 'id="trading-root-contract-card"' not in trading_settings_section
    assert 'id="trading-root-sim-card"' in index_html
    assert "累積利息" in trading_js
    assert "下一次計息" in trading_js
    assert ".trading-fusion-weight-list" in styles
    assert ".trading-fusion-weight-chip" in styles
    assert ".trading-fusion-inline-input" in styles


def test_trading_exchange_is_separate_from_wallet_page():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    workflow_templates = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "workflows" / "trading_bot").glob("*.json"))
    )
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    economy_section = index_html.split('id="module-economy"', 1)[1].split('id="module-trading"', 1)[0]
    trading_section = index_html.split('id="module-trading"', 1)[1].split('id="module-accounts"', 1)[0]
    trading_settings_section = index_html.split('id="sec-settings-trading"', 1)[1].split('id="sec-settings-drive"', 1)[0]

    assert 'id="tab-module-trading"' in index_html
    assert "積分交易所" in trading_section
    assert 'id="trading-card"' in trading_section
    assert 'id="trading-submit-order-btn"' in trading_section
    assert 'id="trading-order-estimate"' in trading_section
    assert 'id="trading-sellable-hint"' in trading_section
    assert 'aria-live="polite"' in trading_section
    assert 'id="trading-margin-card"' in trading_section
    assert 'id="trading-margin-type"' in trading_section
    assert 'id="trading-margin-collateral"' in trading_section
    assert 'id="trading-margin-open-btn"' in trading_section
    assert 'id="trading-margin-position-list"' in trading_section
    assert 'id="trading-margin-account-summary"' in trading_section
    assert "全倉維持率" in trading_section
    assert 'id="trading-order-form"' in trading_section
    assert 'id="trading-availability-note"' in trading_section
    assert 'id="trading-background-status"' in trading_section
    assert 'id="trading-trial-credit-available"' in trading_section
    assert 'id="trading-trial-credit-note"' in trading_section
    assert 'id="trading-root-card"' not in trading_section
    assert 'id="trading-limit-match-btn"' not in trading_section
    assert 'id="trading-liquidation-scan-btn"' not in trading_section
    assert 'id="trading-liquidation-status"' not in trading_section
    assert 'id="trading-root-card"' in trading_settings_section
    assert 'id="trading-root-inline-msg"' in trading_settings_section
    assert 'id="trading-limit-match-btn"' in trading_settings_section
    assert 'id="trading-liquidation-scan-btn"' in trading_settings_section
    assert 'id="trading-liquidation-status"' in trading_settings_section
    assert 'id="trading-funding-available"' in trading_section
    assert "交易總可用" in trading_section
    assert 'id="trading-portfolio-total-equity"' in trading_section
    assert 'id="trading-portfolio-leverage-count"' in trading_section
    assert 'id="trading-portfolio-card"' in trading_section
    assert 'id="trading-portfolio-asset-list"' in trading_section
    assert "我的資產組合" in trading_section
    assert "現貨倉位" in trading_section
    assert 'id="trading-root-reset-sim-btn"' in trading_section
    assert 'id="trading-btc-signal-card"' in trading_section
    assert 'id="trading-btc-signal-body"' in trading_section
    assert "比特幣信號" in trading_section
    assert 'id="trading-reference-chart"' in trading_section
    assert 'id="trading-reference-tooltip"' in trading_section
    assert 'id="trading-reference-interval"' in trading_section
    assert trading_section.index('id="trading-reference-chart"') < trading_section.index('id="trading-order-form"')
    assert trading_section.index('id="trading-risk-dashboard"') < trading_section.index('id="trading-order-form"')
    assert 'id="trading-signal-light-price"' in trading_section
    assert 'id="trading-signal-light-bot"' in trading_section
    assert 'id="trading-signal-light-risk"' in trading_section
    assert "交易訊號" in trading_section
    assert "交易控制台" not in trading_section
    assert '<option value="1s">1 秒</option>' not in trading_section
    assert '<option value="1m">1 分</option>' not in trading_section
    assert '<option value="5m">5 分</option>' in trading_section
    assert '<option value="15m" selected>15 分</option>' in trading_section
    assert 'id="trading-indicator-ma5"' in trading_section
    assert 'id="trading-indicator-ma10"' in trading_section
    assert 'id="trading-indicator-ma20" checked' in trading_section
    assert 'id="trading-indicator-ma30"' in trading_section
    assert 'id="trading-indicator-ma60"' in trading_section
    assert 'id="trading-indicator-ema12"' in trading_section
    assert 'id="trading-indicator-ema26"' in trading_section
    assert 'id="trading-indicator-ema50"' in trading_section
    assert 'id="trading-indicator-bollinger"' in trading_section
    assert 'id="trading-indicator-rsi14"' in trading_section
    assert 'id="trading-indicator-kd"' in trading_section
    assert 'id="trading-btc-trade-card"' not in trading_section
    assert 'id="trading-btc-trade-path"' not in trading_section
    assert "Binance 參考價格" in trading_section
    assert "蠟燭圖" in trading_section
    assert "現貨下單、交易機器人與借貸功能都留在同一個交易所頁" in trading_section
    assert 'label: "我的倉位", action: "trading:my-positions", hideForRoot: true' in core_js
    assert "root 的全站倉位、基金營運與模擬倉位另列管理頁" in trading_section
    assert 'id="trading-root-sim-card"' in trading_section
    assert 'id="trading-root-contract-card"' in trading_section
    assert 'id="trading-root-contract-card"' not in trading_settings_section
    assert '<details class="drive-collapsible-panel settings-collapse" id="trading-root-contract-card"' in trading_section
    assert '<div class="drive-card" id="trading-root-contract-card"' not in trading_section
    assert 'id="trading-root-sim-spot-count"' in trading_section
    assert 'id="trading-root-sim-margin-count"' in trading_section
    assert 'id="trading-root-sim-contract-count"' in trading_section
    assert 'id="trading-root-sim-spot-list"' in trading_section
    assert 'id="trading-root-sim-margin-list"' in trading_section
    assert "現貨模擬倉位" in trading_section
    assert "借貸模擬倉位" in trading_section
    assert "合約模擬倉位" in trading_section
    assert 'id="trading-contract-open-btn"' in trading_section
    assert 'id="trading-contract-position-list"' in trading_section
    assert 'id="trading-submit-order-btn"' not in economy_section
    assert 'id="trading-root-card"' not in economy_section
    assert 'id="economy-wallet-download-btn"' in economy_section
    assert 'id="economy-treasury-analysis-summary"' in economy_section
    assert "economy-flow-dashboard" in economy_section
    assert 'id="economy-trading-summary-card"' not in economy_section
    assert 'id="economy-trading-export-btn"' not in economy_section
    assert 'id="economy-spot-position-quantity"' not in economy_section
    assert 'id="economy-spot-position-detail-list"' not in economy_section
    assert "現貨明細" not in economy_section
    assert "進階倉位明細" not in economy_section
    assert "市價平倉" not in economy_section
    assert "現貨部位" not in economy_section
    assert "各交易對分開計算" not in economy_section
    assert 'id="economy-margin-position-count"' not in economy_section
    assert 'id="economy-margin-position-summary"' not in economy_section
    assert 'id="economy-margin-position-detail-list"' not in economy_section
    assert 'id="trading-asset-overview-card"' in trading_section
    assert 'id="trading-root-sitewide-card"' in trading_section
    assert 'id="trading-root-fund-flow-inflow"' in trading_section
    assert 'id="trading-root-fund-flow-outflow"' in trading_section
    assert 'id="trading-root-fund-flow-net"' in trading_section
    assert 'id="trading-root-fund-flow-balance"' in trading_section
    assert 'id="trading-root-fund-flow-meter"' in trading_section
    assert 'id="trading-root-fund-flow-category-list"' in trading_section
    assert "finance-flow-panel" in trading_section
    assert "finance-flow-sections" in trading_section
    assert "營運流入" in trading_section
    assert "營運流出" in trading_section
    assert "本金移轉不列收支" in trading_section
    assert "已實現淨收支分類" in trading_section
    assert "只彙總交易所基金已實現營運損益" in trading_section
    assert "收支狀態" in trading_section
    assert "fund_flow_summary" in trading_js
    assert "tradingFundFlowStatus" in trading_js
    assert "realizedFlowCategories" in trading_js
    assert "尚無已實現淨收支分類" in trading_js
    assert "本金移轉 ·" not in trading_js
    assert "flow_summary" in economy_js
    assert "service_fee_flow_categories" in economy_js
    assert "function renderEconomyTreasuryFlowList" in economy_js
    assert "function economyFlowMeterHtml" in economy_js
    assert "economy-flow-chart" not in economy_js
    assert "economy-flow-bar-track" not in economy_js
    assert "function tradingFundFlowMeterHtml" in trading_js
    assert "function tradingFundFlowTileHtml" in trading_js
    assert "流入總額" in economy_js
    assert "流出總額" in economy_js
    assert 'id="trading-exchange-reference-card"' in trading_section
    assert 'id="trading-market-summary-grid"' in trading_section
    assert 'id="trading-spot-position-card"' in trading_section
    assert 'id="trading-spot-position-detail-list"' in trading_section
    assert 'id="trading-bot-position-card"' in trading_section
    assert 'id="trading-bot-position-list"' in trading_section
    assert 'id="trading-orders-fills-grid"' in trading_section
    assert '"trading-orders-fills-grid",' in trading_js
    assert "let tradingActivePage" in trading_js
    assert "function setTradingActivePage" in trading_js
    assert "function openTradingExchangePage" in trading_js
    assert 'const backgroundStatus = $("trading-background-status");' in trading_js
    assert 'if (engineStatus) engineStatus.style.display = active === "spot" ? "" : "none";' in trading_js
    assert 'if (backgroundStatus) backgroundStatus.style.display = active === "spot" && backgroundStatus.textContent ? "" : "none";' in trading_js
    assert 'status = $("trading-background-status")' in trading_js
    assert "整戶維持率" in trading_js
    assert "補保證金" in trading_js
    assert "原始保證金" in trading_js
    assert "原始保證金率" in trading_js
    assert "原始保證金最低需求" in trading_js
    assert "原始保證金點數（不含開倉費）" in trading_section
    assert "實際預扣" in trading_js
    assert "本欄位最多可填" in trading_js
    assert "維持率 + 費率安全底線" in trading_js
    assert "tradingSettlementFeePoints" in trading_js
    assert "tradingMicropointsToSettlementPoints" in trading_js
    assert "const feeMicropoints = tradingFeeMicropoints(notional, feeRatePercent);" in trading_js
    assert "const closeFeeMicropoints = tradingFeeMicropoints(exitNotional, feeRatePercent);" in trading_js
    assert "Math.ceil(notional * feeRatePercent / 100)" not in trading_js
    assert "放空價格風險" in trading_js
    assert "價格上漲會虧損並降低維持率" in trading_js
    assert "未實現盈虧" in trading_js
    assert "損益平衡價" in trading_js
    assert "逐倉估算強平價" in trading_js
    assert "損益平衡價已含開倉費、累積利息與預估平倉手續費" in trading_js
    assert "實際清算仍依全倉維持率" in trading_js
    assert "你填寫的保證金已超過本次買入名目金額，這不屬於融資交易" in trading_js
    assert "請改用現貨買入" in trading_js
    assert "且至少要借 1 點" in trading_js
    assert "原始保證金不足，至少需要" in trading_js
    assert "renderTradingMarginAccountSummary" in trading_js
    assert "liquidation_price_points" in trading_js
    assert "breakeven_price_points" in trading_js
    assert "unrealized_pnl_points" in trading_js
    assert "function tradingMarginBillableInterestPoints" in trading_js
    assert "return tradingMicropointsToSettlementPoints(micro);" in trading_js
    assert "function tradingMarginLiveInterest" in trading_js
    assert "function tradingMarginBreakEvenPrice" in trading_js
    assert "function tradingMarginNextInterestAtMs" in trading_js
    assert 'id="economy-contract-position-count"' not in economy_section
    assert 'id="economy-contract-position-summary"' not in economy_section
    assert 'id="economy-trading-order-list"' not in economy_section
    assert 'id="economy-trading-fill-list"' not in economy_section
    assert 'id="economy-catalog-list"' not in economy_section
    assert "服務價格" not in economy_section
    assert 'tabId: "tab-module-trading"' in core_js
    assert 'module: "trading"' in core_js
    assert 'switchModuleTab("trading")' in bootstrap_js
    assert 'if (normTab === "trading"' in admin_js
    assert "function renderTradingWalletSummary" not in trading_js
    assert "function loadTradingBtcSignal" in trading_js
    assert "function tradingBtcSignalCountdownText" in trading_js
    assert "function updateTradingBtcSignalMeta" in trading_js
    assert "next_prediction_at" in trading_js
    assert "下次預測倒數" in trading_js
    assert "策略版本" in trading_js
    assert "fear_greed" in trading_js
    assert "/trading/btc-signal" in trading_js
    assert "function rootVirtualSpotValue" not in trading_js
    assert "function renderEconomySpotPositionDetails" not in trading_js
    assert "function renderEconomyMarginPositionDetails" not in trading_js
    assert "function submitEconomySpotSell" not in trading_js
    assert "data-economy-spot-limit" not in trading_js
    assert "data-economy-spot-market-close" not in trading_js
    assert "data-economy-margin-close" not in trading_js
    assert "data-economy-margin-add-collateral" not in trading_js
    assert "data-economy-margin-withdraw-collateral" not in trading_js
    assert "data-margin-add-collateral" in trading_js
    assert "data-margin-withdraw-collateral" in trading_js
    assert "function bindTradingMarginAssetActions" in trading_js
    assert "function bindTradingContractAssetActions" in trading_js
    assert "tradingPortfolioMarginRow" in trading_js
    assert "trading-portfolio-actions" in trading_js
    assert "tradingScopedActionInput" in trading_js
    assert "addTradingMarginCollateral" in trading_js
    assert "withdrawTradingMarginCollateral" in trading_js
    assert "function tradingMarginWithdrawEstimate" in trading_js
    assert "預估可抽出" in trading_js
    assert "amount > withdrawEstimate.maxWithdrawable" in trading_js
    assert "function scheduleTradingMutationRefresh" in trading_js
    assert "applyTradingOrderResult(json.order || null)" in trading_js
    assert "交易引擎未回傳訂單" in trading_js
    assert 'currentUser === "root" ? "root 模擬" : ""' in trading_js
    assert "await loadEconomyDashboard();" not in trading_js.split("async function submitTradingOrder()", 1)[1].split("async function saveTradingBot()", 1)[0]
    assert "margin_positions" in trading_js
    assert "margin_summary" in trading_js
    assert ">確認</button>" not in trading_js
    assert ">市價平倉</button>" in trading_js
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    assert "/points/wallet/export.csv" in economy_js
    assert "/trading/history/export.csv" in economy_js
    assert '"economy-wallet-download-btn", downloadEconomyWalletCsv' in economy_js
    assert '"economy-trading-export-btn", downloadEconomyTradingCsv' not in economy_js
    assert "grid-template-columns: minmax(120px, .85fr) repeat(5" in styles
    assert "持有成本" in trading_js
    assert "損益平均價格" in trading_js
    assert "目前部位價值" in trading_js
    assert "盈虧" in trading_js
    assert "已實現盈虧" in trading_js
    assert "realized_pnl_points" in trading_js
    assert "unrealized_pnl_points" in trading_js
    assert "tradingSpotCostBasis" in trading_js
    assert "function tradingMergeLivePriceContext" in trading_js
    assert "function tradingMergeLiveMarket" in trading_js
    assert "function tradingSpotBackendPnl" in trading_js
    assert 'tradingSpotCostBasis(position, market, "risk_grade")' in trading_js
    assert "沿用上一筆可用風控價顯示盈虧" in trading_js
    assert "payload.emergency_close = true" not in trading_js
    assert "手續費按平時 2 倍計算" not in trading_js
    assert "function tradingOrderDraftEstimate" in trading_js
    assert "function syncTradingOrderSideTheme" in trading_js
    assert "function tradingOrderInputMode" in trading_js
    assert "function syncTradingOrderInputMode" in trading_js
    assert 'id="trading-input-mode"' in trading_section
    assert "用點數換算" in trading_section
    assert "買入時點數視為含手續費的總支出" in trading_js
    assert "quantity: tradingQuantityForSubmit(estimate.quantity)" in trading_js
    assert "trading-order-buy" in trading_js
    assert "trading-order-sell" in trading_js
    assert "買入下單" in trading_js
    assert "賣出下單" in trading_js
    assert ".trading-order-buy" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert ".trading-submit-sell" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert ".trading-sellable-hint" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert "function updateTradingOrderEstimate" in trading_js
    assert "function updateTradingSellableHint" in trading_js
    assert "data-trading-fill-sellable" in trading_js
    assert "目前可賣" in trading_js
    assert "data-sellable-quantity" in trading_js
    assert "最多 ${formatTradingQuantityValue(state.available)} ${state.asset}" in trading_js
    assert "目前可賣 ${formatTradingQuantityValue(state.available)} ${state.asset}" in trading_js
    assert ".trading-sellable-hint.no-sellable" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert "function closeTradingSpotPositionMarket" in trading_js
    assert "data-trading-spot-sell-market" in trading_js
    assert "data-trading-spot-sell-limit" in trading_js
    assert "超過可用" in trading_js
    assert "超過可賣現貨" in trading_js
    assert "function openTradingMarginPosition" in trading_js
    assert "function closeTradingMarginPosition" in trading_js
    assert "現貨交易機器人" in trading_section
    assert "自動化 Workflow" in trading_section
    assert "定投" in trading_section
    assert 'data-trading-bot-tab="competition"' in trading_section
    assert 'id="trading-bot-competition-week"' in trading_section
    assert 'id="trading-bot-competition-list"' in trading_section
    assert 'id="trading-bot-competition-award-btn"' in trading_section
    assert "定投回測 Dashboard" in trading_section
    assert "網格回測 Dashboard" in trading_section
    assert "Workflow 回測 Dashboard" in trading_section
    assert "/trading-workflow-editor.html" in trading_section
    assert 'id="trading-auto-workflow-json"' in trading_section
    assert 'id="trading-workflow-load-btn"' in trading_section
    assert 'id="trading-workflow-template-select"' in trading_section
    assert 'id="trading-workflow-template-apply-btn"' in trading_section
    assert 'id="trading-workflow-template-explanation"' in trading_section
    assert 'id="trading-workflow-custom-name"' in trading_section
    assert 'id="trading-workflow-custom-save-btn"' in trading_section
    assert "回檔趨勢吸納 + 超晚減倉 15% + 不加碼（Codex）" in trading_section
    assert "自動搜索勝出（Claude rev3_return）" in trading_section
    assert "自動搜索勝出（Claude rev2）" in trading_section
    assert (ROOT / "public" / "trading-workflow-editor.html").exists()
    assert 'id="trading-auto-bot-save-btn"' in trading_section
    assert 'id="trading-dca-bot-save-btn"' in trading_section
    assert 'id="trading-dca-share-parameters"' in trading_section
    assert 'id="trading-grid-share-parameters"' in trading_section
    assert 'id="trading-auto-share-parameters"' in trading_section
    assert 'id="trading-bot-scan-btn"' in trading_section
    assert 'id="trading-dca-backtest-run-btn"' in trading_section
    assert 'id="trading-grid-backtest-run-btn"' in trading_section
    assert 'id="trading-workflow-backtest-run-btn"' in trading_section
    assert 'id="trading-auto-bot-market"' in trading_section
    assert 'id="trading-dca-bot-market"' in trading_section
    assert 'id="trading-dca-backtest-market"' in trading_section
    assert 'id="trading-grid-backtest-market"' in trading_section
    assert 'id="trading-workflow-backtest-market"' in trading_section
    assert 'id="trading-dca-backtest-date-hint"' in trading_section
    assert 'id="trading-grid-backtest-date-hint"' in trading_section
    assert 'id="trading-workflow-backtest-date-hint"' in trading_section
    assert 'data-trading-bot-tab="backtest"' not in trading_section
    assert 'id="trading-bot-tab-backtest"' not in trading_section
    assert 'id="trading-bot-type"' not in trading_section
    assert "function saveTradingBot" in trading_js
    assert "function saveTradingDcaBot" in trading_js
    assert "function scanTradingBots" in trading_js
    assert "function backtestTradingBot" in trading_js
    assert "function formatBacktestDatetimeLocal" in trading_js
    assert "function updateBacktestDateRangeGuidance" in trading_js
    assert "TRADING_BACKTEST_CONTEXTS" in trading_js
    assert "function tradingBacktestConfig" in trading_js
    assert "function tradingBacktestEl" in trading_js
    assert "function tradingBotRecentFills" in trading_js
    assert "function renderTradingBotFillDetails" in trading_js
    assert "function renderTradingBotCompetition" in trading_js
    assert "function loadTradingBotCompetition" in trading_js
    assert "function awardTradingBotCompetition" in trading_js
    assert "網格機器人讀取失敗" in trading_js
    assert "// silent" not in trading_js
    assert "/trading/bot-competition" in trading_js
    assert "/root/trading/bot-competition/award" in trading_js
    assert "/trading/bots/${encodeURIComponent(botUuid)}/share" in trading_js
    assert "/trading/grid-bots/${encodeURIComponent(botUuid)}/share" in trading_js
    assert "trading_bot_weekly_competition_reward" in economy_js
    assert "birthday_gift" in economy_js
    assert "生日禮金" in economy_js
    assert "function increaseTradingBotMaxRuns" in trading_js
    assert "/trading/bots/${encodeURIComponent(botUuid)}/increase-runs" in trading_js
    assert 'id="trading-auto-bot-budget-points"' in trading_section
    assert "function adjustTradingBotBudget" in trading_js
    assert "/trading/bots/${encodeURIComponent(botUuid)}/budget" in trading_js
    assert "data-trading-bot-budget" in trading_js
    assert "調整可用" in trading_js
    assert "function renderTradingBotPositionCard" in trading_js
    assert "function toggleGridBot" in trading_js
    assert "準備暫停交易機器人" in trading_js
    assert "準備暫停網格機器人" in trading_js
    assert "正在停止網格機器人" in trading_js
    assert "function tradingBotNextRunText" in trading_js
    assert "function updateTradingBotCountdowns" in trading_js
    assert "data-trading-bot-next-run" in trading_js
    assert "data-trading-bot-increase-runs" in trading_js
    assert "增加次數" in trading_js
    assert "不限制" in trading_js
    assert "交易明細（" in trading_js
    assert "設定摘要" in trading_js
    assert "已立即執行第一筆" in trading_js
    assert "function tradingErrorText" in trading_js
    assert "交易所 API 不存在" in trading_js
    assert "function bindTradingActionButton" in trading_js
    assert "function showActionFeedback" in core_js
    assert ".action-feedback" in styles
    assert "tradingActiveActionButton" in trading_js
    assert "自動化機器人新增失敗" in trading_js
    assert "定投機器人新增失敗" in trading_js
    assert "未提供錯誤原因" in trading_js
    assert "正在新增自動化機器人" in trading_js
    assert 'id="trading-dca-bot-max-runs"' in trading_section
    assert "輸入 -1 代表不限制執行次數" in trading_section
    assert "parseTradingWorkflowInput" in trading_js
    assert "function tradingWorkflowTemplates" in trading_js
    assert "function loadTradingWorkflowTemplates" in trading_js
    assert "function renderTradingWorkflowTemplateExplanation" in trading_js
    assert "function saveTradingWorkflowCustomTemplate" in trading_js
    assert "function applyTradingWorkflowTemplate" in trading_js
    assert "workflow_graph" in workflow_templates
    assert "codex_competition" in workflow_templates
    assert "param_search" in workflow_templates
    assert "take_profit_percent" in workflow_templates
    assert 'hasCandleData ? "目前圖表不涵蓋你選的回測區間，" : "未載入圖表，"' in trading_js
    assert "正在由後端下載歷史 K 線後回測" in trading_js
    assert "目前圖表不涵蓋你選的回測區間" in trading_js
    assert "auto_fetch_reference_candles = true" in trading_js
    assert "estimateBacktestRequestedCandles" in trading_js
    assert "BACKTEST_TOTAL_CANDLE_LIMIT" in trading_js
    assert "若保留開始時間，結束最晚可選" in trading_js
    assert "若保留結束時間，開始最早可選" in trading_js
    assert "先選開始或結束時間" in trading_js
    assert "basePayload.candle_limit = estimatedCandles || 500" in trading_js
    assert 'id="root-trading-bot-audit-summary"' in index_html
    assert 'id="root-trading-bot-audit-run-btn"' in index_html
    assert "function loadRootTradingBotAuditDashboard" in admin_js
    assert "function runRootTradingBotAudit" in admin_js
    assert "function reviewTradingAuditBugReport" in admin_js
    assert "後端自動分" in trading_js
    assert "資料來源" in trading_js
    assert "prepareTradingBacktestFromBot" in trading_js
    assert 'trading-dca-backtest-run-btn' in trading_js
    assert 'trading-grid-backtest-run-btn' in trading_js
    assert 'trading-workflow-backtest-run-btn' in trading_js
    assert '"/trading/bots/scan"' in trading_js
    assert '"/trading/bots/backtest"' in trading_js
    assert "tradingState.bots" in trading_js
    assert "function matchTradingLimitOrders" in trading_js
    assert "function scanTradingLiquidations" in trading_js
    assert "function renderTradingMarginPositions" in trading_js
    assert "function renderTradingFills" in trading_js
    assert "record_type" in trading_js
    assert "margin_" in trading_js
    assert "進階交易" in trading_js
    assert "損益" in trading_js
    assert '"/trading/margin/open"' in trading_js
    assert '"/root/trading/liquidations/scan"' in trading_js
    assert '"/root/trading/orders/match"' in trading_js
    assert "borrowing_enabled" in trading_js
    assert "margin_liquidation_enabled" in trading_js
    assert "margin_maintenance_percent" in trading_js
    assert "margin_long_financing_percent" in trading_js
    assert "margin_max_pool_utilization_percent" in trading_js
    assert "short_collateral_percent" in trading_js
    assert "exchange_liability_limit_points" in trading_js
    assert "max_outstanding_principal_points" in trading_js
    assert "remaining_borrow_capacity_points" in trading_js
    assert 'id="trading-stop-loss-percent"' in trading_section
    assert 'id="trading-take-profit-percent"' in trading_section
    assert 'id="trading-margin-stop-loss-percent"' in trading_section
    assert 'id="trading-margin-take-profit-percent"' in trading_section
    assert 'id="trading-dca-stop-loss-percent"' in trading_section
    assert 'id="trading-dca-take-profit-percent"' in trading_section
    assert 'id="trading-grid-stop-loss-percent"' in trading_section
    assert 'id="trading-grid-take-profit-percent"' in trading_section
    assert 'id="trading-workflow-backtest-stop-loss-percent"' not in trading_section
    assert 'id="trading-workflow-backtest-take-profit-percent"' not in trading_section
    assert 'id="trading-dca-bot-side"' not in trading_section
    assert 'id="trading-dca-custom-interval-field" style="display:none;"' in trading_section
    assert "function tradingRiskTargetText" in trading_js
    assert "stop_loss_percent: tradingOptionalPercentValue" in trading_js
    assert "take_profit_percent: tradingOptionalPercentValue" in trading_js
    assert "tradingInputPercent" in trading_js
    assert "formatTradingPercent" in trading_js
    assert "initial_margin_points" in trading_js
    assert "maintenance_margin_points" in trading_js
    assert "融資可貸比例" in trading_js
    assert "借券保證金比例" in trading_js
    assert "const showMarginCard = borrowingEnabled || openMarginCount > 0;" in trading_js
    assert 'marginCard.style.display = activePage === "my-positions" || (activePage === "spot" && showMarginCard) ? "" : "none";' in trading_js
    assert 'marginCard.style.display = active === "my-positions" || (active === "spot" && tradingMarginCardShouldShow()) ? "" : "none";' in trading_js
    assert 'if (activePage === "my-positions") marginCard.open = true;' in trading_js
    assert 'function renderTradingBotPositionCard' in trading_js
    assert '"trading-bot-position-card"' in trading_js
    assert 'renderTradingBotPositionCard();' in trading_js
    assert '|| !data.open_count' not in trading_js
    assert 'if (marginOpenForm) marginOpenForm.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";' in trading_js
    assert 'if (marginEstimate) marginEstimate.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";' in trading_js
    assert 'if (fundingPoolCard) fundingPoolCard.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";' in trading_js
    assert "marginControlsDisabled" in trading_js
    assert 'const marginControlsDisabled = !borrowingEnabled;' in trading_js
    assert 'marginControlsDisabled = !borrowingEnabled || currentUser === "root"' not in trading_js
    assert "root 可用模擬資金進行融資 / 借券" in trading_js
    assert "root 尚未開啟借貸交易，目前僅可查看此區。" in trading_js
    assert "保證金不足，至少需要" in trading_js
    assert "手續費會在平倉 / 清算時合併結算" in trading_js
    assert "進階交易開倉失敗：" in trading_js
    assert "trading-margin-open-btn" in trading_js
    assert '"trading-limit-match-btn", matchTradingLimitOrders' in trading_js
    assert '"trading-liquidation-scan-btn", scanTradingLiquidations' in trading_js
    assert "economy-root-virtual-total" not in trading_js
    assert "rootVirtualMarginPositionEquity" not in trading_js
    assert "available + spotValue + marginValue" not in trading_js
    assert "economy-root-virtual-margin-value" not in trading_js
    assert "trial_credit" in trading_js
    assert "function tradingTrialCountdownText" in trading_js
    assert "setInterval(updateTradingTrialCountdown, 1000)" in trading_js
    assert "總可用" in trading_js
    assert "體驗金" in trading_js
    assert "真實積分" in trading_js
    assert "體驗金優先" in trading_js
    assert "function loadTradingReferencePrices" in trading_js
    assert "function restartTradingReferenceAutoRefresh" in trading_js
    assert "function tradingReferenceAutoRefreshMs" in trading_js
    assert "return tradingReferencePriceRefreshMs();" in trading_js
    assert 'interval === "1s"' not in trading_js
    assert "loadTradingReferencePrices({ silent: true, priceOnly: true })" in trading_js
    assert "tradingReferenceChartAutoTimer" in trading_js
    assert "tradingReferenceChartAutoBusy" in trading_js
    assert "}, tradingReferenceChartRefreshMs())" in trading_js
    assert "function tradingReferenceChartLimit" in trading_js
    assert "function mergeTradingReferenceLatestPayload" in trading_js
    assert "function tradingReferencePayloadHasCandles" in trading_js
    assert "loadTradingReferencePrices({ silent: true, latestOnly: true })" in trading_js
    assert 'const latestParam = latestOnly ? "&latest=1" : "";' in trading_js
    assert "tradingReferencePriceAbort" in trading_js
    assert "tradingReferenceChartAbort" in trading_js
    assert "保留上一張蠟燭圖" in trading_js
    assert "tradingState.referencePrices = null" not in trading_js
    assert 'currentModuleTab !== "trading"' in trading_js
    assert "tradingDashboardAutoTimer" in trading_js
    assert "function renderTradingReferenceChart" in trading_js
    assert "function updateTradingReferenceTooltip" in trading_js
    assert "function tradingIndicatorSeries" in trading_js
    assert "function tradingBollingerSeries" in trading_js
    assert "function tradingRsiSeries" in trading_js
    assert "function tradingKdSeries" in trading_js
    assert "function buildTradingReferenceIndicators" in trading_js
    assert "function drawTradingOscillatorPanel" in trading_js
    assert "drawTradingIndicatorLine" in trading_js
    assert "trading-indicator-ma20" in trading_js
    assert "trading-indicator-rsi14" in trading_js
    assert "trading-indicator-kd" in trading_js
    assert "RSI / KD" in trading_js
    assert "布林線" in trading_section
    assert "function tradingReferenceTimeLabel" in trading_js
    assert "tradingReferenceChartModel" in trading_js
    assert "tradingReferenceHoverIndex" in trading_js
    assert 'referenceChart.addEventListener("mousemove", updateTradingReferenceTooltip)' in trading_js
    assert 'referenceChart.addEventListener("mouseleave", hideTradingReferenceTooltip)' in trading_js
    assert "resetRootTradingSimulatedBalance" in trading_js
    assert "/trading/reference-prices" in trading_js
    assert "/trading/btc-trade-signal" not in trading_js
    assert "/root/trading/btc-trade-settings" not in trading_js
    assert "/root/trading/simulated-balance/reset" in trading_js
    assert "刪除 root 的模擬訂單、成交紀錄、現貨與合約持倉" in trading_js
    assert "已清除訂單" in trading_js
    assert "Binance 公開 API" in trading_js
    assert "function tradingDisplaySymbol" in trading_js
    assert "function tradingBaseAssetLabel" in trading_js
    assert "目前對 root 以外用戶開放 ${publicSpotSymbols.join(\"、\") || \"已啟用的積分現貨市場\"} 現貨。" in trading_js
    assert "直接輸入 ${assetLabel} 枚數。" in trading_js
    assert 'tradingDisplaySymbol(market.symbol)' in trading_js
    assert "ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH)" in trading_js
    assert "ctx.strokeRect(x - bodyW / 2, bodyTop, bodyW, bodyH)" in trading_js
    assert "1 POINT = 1 USDT" not in trading_js
    assert "≈" not in trading_js
    assert "function renderBtcTradeSignal" not in trading_js
    assert "root 模擬資金" in trading_js
    assert 'contractCard.style.display = currentUser === "root" && active === "root-sim" ? "" : "none"' in trading_js
    assert "openRootTradingContract" in trading_js
    assert "closeRootTradingContract" in trading_js
    assert "/root/trading/contracts" in trading_js
    assert "futures_positions" in trading_js
    assert "function renderTradingRootSimulationPositions" in trading_js
    assert "renderTradingRootSimulationPositions();" in trading_js
    assert "尚無 root 現貨模擬倉位" in trading_js
    assert "尚無 root 借貸模擬倉位" in trading_js
    assert "totalSpotQuantity" not in trading_js
    assert "function tradingPositionLabel" in trading_js
    assert 'activePositions.map((row) => tradingPositionLabel(row)).join(" / ")' not in trading_js
    assert "function renderTradingSpotPositions" in trading_js
    assert "function tradingSpotPositionDetailRow" in trading_js
    assert "function tradingSpotUnrealizedPnl" in trading_js
    assert "function renderTradingPortfolioSummary" in trading_js
    assert "function tradingPortfolioSummary" in trading_js
    assert 'positions: [\n      "trading-portfolio-card",' in trading_js
    assert 'positions: [\n      "trading-asset-overview-card",' not in trading_js
    assert 'positions: [\n      "trading-market-summary-grid",' not in trading_js
    assert "tradingState.futuresPositions = payload.futures_positions || [];" in trading_js
    assert "tradingState.spotSummary = payload.spot_summary || null;" in trading_js
    assert "rootVirtualSpotValue(activePositions, markets)" not in trading_js
    assert 'renderTradingOrders(orders, "economy-trading-order-list", false)' not in trading_js
    assert '"economy-trading-open-btn", openTradingModuleFromWallet' not in trading_js
    assert '"economy-root-virtual-open-btn", openTradingModuleFromWallet' not in trading_js
    assert 'id="trading-current-delta"' in trading_section
    assert 'id="trading-current-health"' in trading_section
    assert 'id="trading-risk-dashboard"' in trading_section
    assert 'id="trading-risk-dashboard-grid"' in trading_section
    assert 'id="trading-signal-light-price"' in trading_section
    assert 'id="trading-signal-light-bot"' in trading_section
    assert 'id="trading-signal-light-risk"' in trading_section
    assert "交易訊號" in trading_section
    assert "交易控制台" not in trading_section
    assert "function tradingLivePriceRefreshMs()" in trading_js
    assert 'return tradingRefreshMs("trading_live_price_refresh_seconds", 2, 1, 60);' in trading_js
    assert "function loadTradingLivePrice()" in trading_js
    assert "function renderTradingCurrentPrice" in trading_js
    assert "function renderTradingRiskDashboard" in trading_js
    assert "function tradingWorstReadinessState" in trading_js
    assert "function tradingSignalStateLabel" in trading_js
    assert "function updateTradingSignalLight" in trading_js
    assert "function tradingMarketBootSummary" in trading_js
    assert "function tradingSelectedPriceReadiness" in trading_js
    assert "Bot / backtest" in trading_js
    assert "交易所基金" in trading_js
    assert "借貸基金" in trading_js
    assert "Trial credit" in trading_js
    assert "Margin / lending" in trading_js
    assert "boot pending" in trading_js
    assert "/trading/live-price?market=" in trading_js
    assert "updateTradingOrderEstimate();" in trading_js
    assert "price_health" in trading_js
    assert "fallback_reason" in trading_js
    assert "excluded_sources" in trading_js
    assert "high_risk_block_reason" in trading_js
    assert "warnings" in trading_js
    assert "defaulted_market" in trading_js
    assert "function tradingTransportStateSummary" in trading_js
    assert "transport_state" in trading_js
    assert "WebSocket provider input 已退回 HTTP polling" in trading_js
    assert "provider input stale" in trading_js
    assert 'tradingWarningText("reference_degraded")' in trading_js
    assert 'tradingWarningText("reference_healthy_risk_usable")' in trading_js
    assert 'tradingWarningText("reference_healthy_confidence"' in trading_js
    assert "目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用" in trading_js
    assert "價格來源降級，交易是否自動暫停由 root 風控開關決定" in trading_js
    assert "root 目前僅警示，不自動暫停交易" in trading_js
    assert "root 已設定自動暫停：" in trading_js
    assert "風控可用" in trading_js

    workflow_editor = (ROOT / "public" / "trading-workflow-editor.html").read_text(encoding="utf-8")
    workflow_editor_js = (ROOT / "public" / "js" / "trading-workflow-editor.js").read_text(encoding="utf-8")
    workflow_editor_surface = workflow_editor + workflow_editor_js
    assert "workflow_graph" in workflow_editor_surface
    assert "nodes" in workflow_editor_surface
    assert "edges" in workflow_editor_surface
    assert "input/output ports" in workflow_editor_surface
    assert "TRUE/FALSE branch" in workflow_editor_surface
    assert "start_node_id" in workflow_editor_surface
    assert "nested AND/OR" in workflow_editor_surface or "Nested AND" in workflow_editor_surface
    assert 'data-add="logic"' in workflow_editor_surface
    assert 'data-add="condition"' in workflow_editor_surface
    assert 'data-add="action"' in workflow_editor_surface
    assert 'data-add="control"' in workflow_editor_surface
    assert "data-port-node" in workflow_editor_surface
    assert "data-graph-canvas" in workflow_editor_surface
    assert 'draggable="true"' in workflow_editor_surface
    assert "function handleClick" in workflow_editor_surface
    assert "function handleDrop" in workflow_editor_surface
    assert "function handleInput" in workflow_editor_surface
    assert "HackmeTradingWorkflowEditor" in workflow_editor_surface


def test_trading_reference_polling_does_not_overwrite_live_execution_price():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")

    assert 'Reference-price polling is for the chart only.' in trading_js
    assert 'market.manual_price_points = Number(last.close_points || 0);' not in trading_js
    assert 'market.price_source = json.source || "binance_public_api";' not in trading_js


def test_frontend_personal_browser_state_is_account_scoped():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    trading_editor_js = (ROOT / "public" / "js" / "trading-workflow-editor.js").read_text(encoding="utf-8")
    comfyui_js = (ROOT / "public" / "js" / "36-comfyui.js").read_text(encoding="utf-8")
    comfyui_workflows_js = (ROOT / "public" / "js" / "36-comfyui-workflows.js").read_text(encoding="utf-8")
    comfyui_editor_js = (ROOT / "public" / "js" / "comfyui-workflow-editor.js").read_text(encoding="utf-8")
    drive_js = (ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8")
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")

    assert 'ACCOUNT_SCOPE_STORAGE_KEY = "hackme_web.account.active_scope"' in core_js
    assert "function getCurrentAccountStorageScope()" in core_js
    assert "function accountScopedStorageKey(key" in core_js
    assert 'document.dispatchEvent(new CustomEvent("hackme:account-context-changed"' in core_js
    assert "syncActiveAccountStorageScope(previousAccountScope);" in core_js

    assert "TRADING_PERSONAL_FORM_STORAGE_KEY" in trading_js
    assert 'id: "trading-input-mode", value: "quantity"' in trading_js
    assert 'id: "trading-margin-collateral", value: "100"' in trading_js
    assert "function ensureTradingAccountScope" in trading_js
    assert 'document.addEventListener("hackme:account-context-changed"' in trading_js
    assert "loadTradingPersonalFormState();" in trading_js
    assert "bindTradingPersonalFormPersistence();" in trading_js
    assert 'console.warn("[trading] failed to save personal form state", err);' in trading_js
    assert "localStorage.getItem(tradingUserStorageKey(TRADING_WORKFLOW_STORAGE_KEY))" in trading_js
    assert "localStorage.setItem(tradingUserStorageKey(TRADING_WORKFLOW_STORAGE_KEY)" in trading_js
    assert "localStorage.getItem(TRADING_WORKFLOW_STORAGE_KEY)" not in trading_js
    assert "localStorage.setItem(TRADING_WORKFLOW_STORAGE_KEY" not in trading_js
    assert "editorScopedStorageKey(STORAGE_KEY)" in trading_editor_js
    assert "editorScopedStorageKey(PREVIEW_STORAGE_KEY)" in trading_editor_js

    assert "comfyuiUserStorageKey(COMFYUI_VIEW_STORAGE_KEY)" in comfyui_js
    assert 'return comfyuiUserStorageKey("comfyui:draft");' in comfyui_js
    assert 'comfyuiWorkflowEditorStorageKey("hackme_comfyui_workflow_editor_result")' in comfyui_workflows_js
    assert 'comfyuiWorkflowEditorStorageKey("hackme_comfyui_workflow_editor_input")' in comfyui_workflows_js
    assert "editorScopedStorageKey(STORAGE_KEY)" in comfyui_editor_js
    assert "editorScopedStorageKey(RESULT_KEY)" in comfyui_editor_js
    assert "accountScopedStorageKey(\"albumThumbSize\")" in drive_js
    assert "function economyPageStorageKey()" in economy_js
    assert "accountScopedStorageKey(ECONOMY_PAGE_STORAGE_KEY)" in economy_js


def test_trading_live_price_polling_uses_two_second_timer_and_health_badges():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert "tradingLivePriceTimer = setInterval" in trading_js
    assert "function tradingLivePriceRefreshMs()" in trading_js
    assert "trading_live_price_refresh_seconds" in trading_js
    assert "function tradingDashboardRefreshMs()" in trading_js
    assert "function tradingReferenceChartRefreshMs()" in trading_js
    assert 'currentModuleTab !== "trading" && currentModuleTab !== "economy"' in trading_js
    assert "function tradingLivePriceTargetSymbols()" in trading_js
    assert "function refreshTradingWalletLiveMetrics()" in trading_js
    assert "refreshTradingWalletLiveMetrics();" in trading_js
    assert "function tradingLiveMarginFreeMarginPoints()" in trading_js
    assert "function tradingDisplayedMarginSummary(summary = null, rows = null)" in trading_js
    assert "function setTradingPriceTooltip(el, text, detail = \"\")" in trading_js
    assert "function updateTradingReferenceTooltipFromTouch(event)" in trading_js
    assert 'referenceChart.addEventListener("pointermove", updateTradingReferenceTooltip)' in trading_js
    assert 'referenceChart.addEventListener("touchstart", updateTradingReferenceTooltipFromTouch' in trading_js
    assert "current_price_degraded_short" in trading_js
    assert 'tradingWarningText("current_price_healthy_short")' in trading_js
    assert "tradingState.state = state;" in trading_js
    assert "const marginSummary = tradingDisplayedMarginSummary(payload.margin_summary, marginPositions);" not in trading_js
    assert "const displayedMarginSummary = tradingDisplayedMarginSummary(tradingState.marginSummary);" in trading_js
    assert "const accountEquity = totalPositionEquity + freeMargin;" in trading_js
    assert "available_margin_points: availableMargin" in trading_js
    assert "const liveRisk = tradingLiveMarginRisk(row);" in trading_js
    assert "#trading-current-price.trading-price-up" in styles
    assert "#trading-current-price.trading-price-down" in styles
    assert "#trading-current-health.warning" in styles
    assert ".trading-price-tooltip::after" in styles
    assert ".trading-price-tooltip:hover::after" in styles
    assert ".trading-reference-panel-primary .trading-reference-chart" in styles
    assert "touch-action: manipulation;" in styles
    assert ".trading-signal-light[data-state=\"ok\"]" in styles
    assert ".trading-signal-panel:hover .trading-readiness-grid" in styles
    assert ".trading-signal-panel:focus-within .trading-readiness-grid" in styles
    assert ".trading-readiness-grid" in styles
    assert ".trading-readiness-item" in styles
    assert ".trading-readiness-warn" in styles


def test_spot_position_details_show_holding_cost_and_break_even_price():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")

    assert "function tradingSpotHoldingCost(position, market)" in trading_js
    assert "function tradingSpotHoldingCostPerUnit(position, market)" in trading_js
    assert "function tradingSpotBreakEvenExitPrice(position, market)" in trading_js
    assert "持有成本" in trading_js
    assert "損益平均價格" in trading_js
    assert "單顆" in trading_js
    assert "已含預估賣出手續費" in trading_js
    assert "risk-grade 價計算未實現盈虧" in trading_js


def test_trading_ui_labels_reference_and_risk_grade_price_usage():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    trading_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    assert "function tradingMarketPriceContext" in trading_js
    assert "function tradingPriceDegradePolicy" in trading_js
    assert "function tradingPriceDegradePauseMessage" in trading_js
    assert "function tradingWarningLanguage" in trading_js
    assert "function tradingWarningText" in trading_js
    assert "function tradingPriceContextSummary" in trading_js
    assert "risk_grade_usable" in trading_js
    assert "riskContext?.high_risk_blocked" in trading_js
    assert "riskContext?.risk_grade_usable === false" in trading_js
    assert "Risk-grade price is unavailable. Market orders and other high-risk paths remain paused; limit orders are still allowed." in trading_js
    assert 'id="root-trading-price-fusion-trade-min-provider-count"' in trading_html
    assert 'id="root-trading-warning-language"' in trading_html
    assert 'id="root-trading-price-degrade-pause-market-orders"' in trading_html
    assert 'id="root-trading-price-degrade-pause-bots"' in trading_html
    assert 'id="root-trading-price-degrade-pause-borrowing"' in trading_html
    assert 'id="root-trading-simulated-slippage-enabled"' in trading_html
    assert 'id="root-trading-simulated-slippage-base-basis-points"' in trading_html
    assert 'id="root-trading-simulated-slippage-size-basis-points-per-10k-notional"' in trading_html
    assert 'id="root-trading-simulated-slippage-max-basis-points"' in trading_html
    assert "目前價格（reference）" in trading_html
    assert "用途：展示 / 一般估值" in trading_html
    assert "市價單估值採用風控級價格" in trading_js
    assert "風控級價格用途：融資 / 強平 / 保證金 / PnL" in trading_js
    assert "目前部位價值採 reference price；未實現盈虧採 risk-grade price" in trading_js
    assert "reference price：" in trading_js
    assert "warning_language" in admin_js
    assert "price_fusion_trade_min_provider_count" in admin_js
    assert "price_degrade_pause_market_orders" in admin_js
    assert "price_degrade_pause_bots" in admin_js
    assert "price_degrade_pause_borrowing" in admin_js
    assert "simulated_slippage_enabled" in admin_js
    assert "simulated_slippage_base_basis_points" in admin_js
    assert "simulated_slippage_size_basis_points_per_10k_notional" in admin_js
    assert "simulated_slippage_max_basis_points" in admin_js
    assert 'usable ${risk.risk_grade_usable ? "yes" : "no"}' in admin_js


def test_trading_frontend_surfaces_bot_and_backtest_errors_with_direct_grid_benchmark_numbers():
    trading_js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    trading_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    assert "function tradingFriendlyErrorText" in trading_js
    assert "目前圖表 K 線不涵蓋你選的回測區間" in trading_js
    assert "這個市場目前未開放交易機器人" in trading_js
    assert "Workflow 歷史回測報告讀取失敗" in trading_js
    assert "目前沒有可用的 Workflow 歷史回測資料" in trading_js
    assert "目前沒有開放機器人的市場" in trading_js
    assert "docs/COMPETITION/GRID_SKYFLOOR_COMPARISON.md" not in trading_html
    assert "5 個資產 5 年 1h 回測平均報酬：保守 +37.39%、窄天地 +73.59%、中天地 +70.81%、寬天地 +77.01%、極寬天地 +111.47%。" in trading_html
