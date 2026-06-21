from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_grid_fee_ui_shows_net_profit_break_even_and_confirmation_wiring():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    trading_js = (
        (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "56-trading-bots.js").read_text(encoding="utf-8")
    )

    assert 'id="trading-grid-preview"' in index_html
    assert "trading/grid/preview" in trading_js
    assert "網格費率試算（限價掛單 / maker）" in trading_js
    assert "損益兩平間距" in trading_js
    assert "最不利一格毛利" in trading_js
    assert "最不利一格手續費" in trading_js
    assert "最不利一格扣費後淨利" in trading_js
    assert "預估一輪全格總手續費" in trading_js
    assert "feePerTrade" not in trading_js
    assert "feePerBuyOrder" in trading_js
    assert "confirm_thin_profit" in trading_js
    assert "利潤過薄" in trading_js


def test_grid_preset_apply_populates_and_persists_form_fields():
    trading_js = (
        (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "56-trading-bots.js").read_text(encoding="utf-8")
    )

    assert "function tradingSetGridPresetFieldValue" in trading_js
    assert 'tradingSetGridPresetFieldValue("trading-grid-lower-price", lower);' in trading_js
    assert 'tradingSetGridPresetFieldValue("trading-grid-upper-price", upper);' in trading_js
    assert 'tradingSetGridPresetFieldValue("trading-grid-count", cfg.grid_count);' in trading_js
    assert 'tradingSetGridPresetFieldValue("trading-grid-order-amount", cfg.order_amount);' in trading_js
    assert 'tradingSetGridPresetFieldValue("trading-grid-spacing-mode", cfg.spacing_mode);' in trading_js
    assert 'el.dispatchEvent(new Event("input", { bubbles: true }));' in trading_js
    assert 'el.dispatchEvent(new Event("change", { bubbles: true }));' in trading_js
    assert 'if (shouldSave) saveTradingPersonalFormState();' in trading_js
    assert 'if ($("trading-grid-preset")?.value) applyGridPreset({ quiet: true, save: false });' in trading_js
    assert 'if ($("trading-grid-preset")?.value) applyGridPreset({ quiet: true });' in trading_js
    assert "function tradingGridPresetMarket" in trading_js
    assert "function tradingGridPresetReferencePrice" in trading_js
