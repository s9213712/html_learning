#!/usr/bin/env python3
"""Targeted Playwright probe for PointsChain governance/dispute UI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", default="root")
    parser.add_argument(
        "--password",
        default=os.environ.get("HACKME_ROOT_PASSWORD") or os.environ.get("HTML_LEARNING_ROOT_PASSWORD", ""),
        help="Root password. Prefer HACKME_ROOT_PASSWORD over a command-line value.",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("HACKME_ROOT_PASSWORD or --password is required")

    result = {"ok": False, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(10000)
        page.goto(args.base_url, wait_until="domcontentloaded")
        page.fill("#li-user", args.username)
        page.fill("#li-pw", args.password)
        page.click("#li-btn")
        page.wait_for_selector("#tab-module-economy", state="visible", timeout=15000)
        check("login", True, f"logged in as {args.username}")

        page.click("#tab-module-economy")
        page.wait_for_selector("#module-economy", state="visible", timeout=10000)
        page.wait_for_selector("#tab-economy-governance", state="visible", timeout=10000)
        check("governance_tab_visible", True)

        page.click("#tab-economy-balance")
        page.wait_for_selector("#economy-balance-page.active", timeout=10000)
        page.wait_for_selector("#economy-wallet-onboarding-card", state="attached", timeout=10000)
        page.wait_for_timeout(800)
        check("wallet_onboarding_card_present", page.locator("#economy-wallet-onboarding-card").count() == 1)
        wallet_card_visible = page.locator("#economy-wallet-onboarding-card").is_visible()
        if wallet_card_visible:
            wallet_action_count = page.locator("#economy-wallet-identity-list [data-wallet-transfer-to]").count()
            if wallet_action_count:
                page.locator("#economy-wallet-identity-list [data-wallet-transfer-to]").first.click()
                page.wait_for_selector("#economy-wallet-transfer-card", state="visible", timeout=5000)
                page.fill("#economy-transfer-amount", "0")
                page.click("#economy-transfer-submit-btn")
                page.wait_for_timeout(150)
                transfer_msg = page.locator("#economy-transfer-msg").inner_text()
                transfer_msg_class = page.locator("#economy-transfer-msg").get_attribute("class") or ""
                check(
                    "wallet_transfer_invalid_input_visible_feedback",
                    "show" in transfer_msg_class and "請確認 From、To、Value" in transfer_msg,
                    json.dumps({"message": transfer_msg, "class": transfer_msg_class}, ensure_ascii=False),
                )
            else:
                check("wallet_transfer_action_skipped_no_wallet", True, "no wallet transfer action in this account")

            page.evaluate("economyTransferMsg('front-end probe transfer signing failure visible', false)")
            synthetic_transfer_msg = page.locator("#economy-transfer-msg").inner_text()
            synthetic_transfer_class = page.locator("#economy-transfer-msg").get_attribute("class") or ""
            check(
                "wallet_transfer_error_message_layer_visible",
                "show" in synthetic_transfer_class and "err" in synthetic_transfer_class and "transfer signing failure" in synthetic_transfer_msg,
                json.dumps({"message": synthetic_transfer_msg, "class": synthetic_transfer_class}, ensure_ascii=False),
            )

            page.eval_on_selector("#economy-wallet-create-card", "el => el.open = true")
            page.click("#economy-wallet-create-cold-btn")
            page.wait_for_function(
                "() => !!document.querySelector('#economy-wallet-generated-address')?.value",
                timeout=10000,
            )
            cold_create_msg = page.locator("#economy-wallet-onboarding-msg").inner_text()
            cold_create_class = page.locator("#economy-wallet-onboarding-msg").get_attribute("class") or ""
            check(
                "cold_wallet_create_has_visible_feedback",
                "show" in cold_create_class and "冷錢包" in cold_create_msg,
                json.dumps({"message": cold_create_msg, "class": cold_create_class}, ensure_ascii=False),
            )
            page.click("#economy-wallet-use-generated-cold-btn")
            page.wait_for_timeout(300)
            cold_select_msg = page.locator("#economy-wallet-onboarding-msg").inner_text()
            cold_select_class = page.locator("#economy-wallet-onboarding-msg").get_attribute("class") or ""
            check(
                "cold_wallet_use_generated_has_visible_feedback",
                "show" in cold_select_class and "已選用" in cold_select_msg,
                json.dumps({"message": cold_select_msg, "class": cold_select_class}, ensure_ascii=False),
            )
            page.locator("#economy-wallet-private-key-confirmed").set_checked(False)
            page.click("#economy-wallet-confirm-cold-btn")
            page.wait_for_timeout(150)
            cold_confirm_msg = page.locator("#economy-wallet-onboarding-msg").inner_text()
            cold_confirm_class = page.locator("#economy-wallet-onboarding-msg").get_attribute("class") or ""
            check(
                "cold_wallet_confirm_without_ack_visible_error",
                "show" in cold_confirm_class and "err" in cold_confirm_class and "請先確認已保存備份碼" in cold_confirm_msg,
                json.dumps({"message": cold_confirm_msg, "class": cold_confirm_class}, ensure_ascii=False),
            )
        else:
            check("wallet_frontend_actions_skipped_for_hidden_wallet_card", True, "wallet card hidden for this role")

        page.click("#tab-economy-governance")
        page.wait_for_selector("#economy-governance-page.active", timeout=10000)
        page.wait_for_timeout(800)
        check("governance_page_split_from_explorer", page.locator("#economy-governance-page.active").count() == 1)
        check("dispute_card_present", page.locator("#economy-dispute-card").count() == 1)
        check("governance_category_select_present", page.locator("#economy-governance-category-select").count() == 1)
        check("governance_status_tabs_present", page.locator("[data-governance-status-filter]").count() == 3)
        page.click('#economy-governance-status-tabs [data-governance-status-filter="voting"]')
        page.wait_for_timeout(150)
        check("governance_voting_tab_active", "active" in (page.locator('#economy-governance-status-tabs [data-governance-status-filter="voting"]').get_attribute("class") or ""))
        page.click('#economy-governance-status-tabs [data-governance-status-filter="closed"]')
        page.wait_for_timeout(150)
        check("governance_closed_tab_active", "active" in (page.locator('#economy-governance-status-tabs [data-governance-status-filter="closed"]').get_attribute("class") or ""))
        page.click('#economy-governance-status-tabs [data-governance-status-filter="review"]')
        page.wait_for_timeout(150)
        check("governance_review_tab_active", "active" in (page.locator('#economy-governance-status-tabs [data-governance-status-filter="review"]').get_attribute("class") or ""))
        check("mint_ui_present", page.locator("#economy-governance-mint-create-details").count() == 1)
        check("policy_ui_present", page.locator("#economy-governance-policy-create-details").count() == 1)
        check("emergency_lockdown_ui_present", page.locator("#economy-governance-lockdown-create-btn").count() == 1)
        check("cold_wallet_legacy_delete_select_removed", page.locator("#economy-wallet-delete-cold-address").count() == 0)

        page.select_option("#economy-governance-category-select", "mint")
        page.wait_for_timeout(250)
        check("mint_category_visible", page.locator("#economy-governance-mint-create-details").evaluate("el => getComputedStyle(el).display !== 'none'"))
        if page.locator("#economy-public-governance-create-details").count():
            check("public_category_hidden_when_mint_selected", page.locator("#economy-public-governance-create-details").evaluate("el => getComputedStyle(el).display === 'none'"))
        else:
            check("legacy_public_governance_create_removed", True, "public governance creation is no longer a standalone form")

        page.select_option("#economy-governance-category-select", "dispute")
        page.wait_for_timeout(250)
        selected_case_text = page.locator("#economy-governance-selected-case").inner_text()
        check("dispute_category_requires_case_selection", "未選取疑義交易案件" in selected_case_text, selected_case_text)

        page.click("#tab-economy-explorer")
        page.wait_for_selector("#economy-explorer-page.active", timeout=10000)
        check("explorer_not_governance", page.locator("#economy-governance-page.active").count() == 0)

        page.click("#tab-economy-governance")
        page.wait_for_selector("#economy-governance-page.active", timeout=10000)
        page.select_option("#economy-governance-category-select", "treasury")
        page.wait_for_timeout(250)
        if page.locator("#economy-governance-treasury-create-details").count():
            page.eval_on_selector("#economy-governance-treasury-create-details", "el => el.open = true")
            page.select_option("#economy-governance-treasury-action", "EXCHANGE_FUND_REPLENISH")
            page.wait_for_function(
                "() => { const el = document.querySelector('#economy-governance-treasury-destination'); return el && el.readOnly && el.value && el.value.startsWith('pc0'); }",
                timeout=15000,
            )
            treasury_dest = page.eval_on_selector(
                "#economy-governance-treasury-destination",
                "el => ({value: el.value, readOnly: el.readOnly})",
            )
            check(
                "exchange_replenish_address_locked",
                bool(treasury_dest["readOnly"]) and str(treasury_dest["value"]).startswith("pc0"),
                json.dumps(treasury_dest, ensure_ascii=False),
            )
        else:
            check(
                "treasury_create_moved_to_official_wallet_management",
                page.locator("#economy-manager-points-management-card").count() == 1,
                "legacy treasury governance form removed; official wallet management card remains",
            )

        page.click("#tab-economy-transactions")
        page.wait_for_selector("#economy-transactions-page.active", timeout=10000)
        page.wait_for_timeout(800)
        dispute_buttons = page.locator("#economy-transactions-list [data-dispute-tx]").count()
        check("transaction_dispute_button_rendered", dispute_buttons >= 1, f"count={dispute_buttons}")
        if dispute_buttons:
            dialogs = []
            page.on("dialog", lambda dialog: (dialogs.append({"type": dialog.type, "message": dialog.message}), dialog.dismiss()))
            first_dispute = page.locator("#economy-transactions-list [data-dispute-tx]").first
            dataset = first_dispute.evaluate(
                "el => ({tx: el.dataset.disputeTx || '', from: el.dataset.disputeFrom || '', to: el.dataset.disputeTo || '', amount: el.dataset.disputeAmount || '', branch: el.dataset.disputeBranch || '', bound: el.dataset.disputeBound || ''})"
            )
            check("transaction_dispute_dataset_complete", bool(dataset["tx"] and dataset["from"] and dataset["to"] and dataset["amount"]), json.dumps(dataset, ensure_ascii=False))
            first_dispute.click()
            page.wait_for_timeout(250)
            transaction_msg = page.locator("#economy-transactions-msg").inner_text()
            msg_class = page.locator("#economy-transactions-msg").get_attribute("class") or ""
            root_governance_hint = args.username == "root" and "root 帳號不使用匿名地址疑義流程" in transaction_msg
            admin_or_user_prompt = args.username != "root" and bool(dialogs)
            check(
                "transaction_dispute_click_has_feedback",
                "疑義交易" in transaction_msg or "匿名地址疑義" in transaction_msg or bool(dialogs),
                json.dumps({"message": transaction_msg, "class": msg_class, "dialogs": dialogs}, ensure_ascii=False),
            )
            check(
                "transaction_dispute_feedback_visible",
                bool(dialogs) or "show" in msg_class,
                json.dumps({"message": transaction_msg, "class": msg_class, "dialogs": dialogs}, ensure_ascii=False),
            )
            check(
                "transaction_dispute_expected_prompt_or_root_hint",
                root_governance_hint or admin_or_user_prompt,
                json.dumps({"username": args.username, "message": transaction_msg, "dialogs": dialogs}, ensure_ascii=False),
            )
            if args.username != "root":
                first_dialog_message = dialogs[0]["message"] if dialogs else ""
                check(
                    "transaction_dispute_prompt_discloses_statement_minimum",
                    "至少 12 字" in first_dialog_message,
                    first_dialog_message,
                )

        result["ok"] = all(item["ok"] for item in result["checks"])
        browser.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
