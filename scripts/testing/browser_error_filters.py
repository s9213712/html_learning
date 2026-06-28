"""Shared browser error filters for Playwright acceptance checks."""

from __future__ import annotations


EXPECTED_BROWSER_HTTP_FAILURE_PATHS = (
    "/api/admin/trading/report",
    "/api/comfyui/generate",
    "/api/points/explorer/fee-estimate",
    "/api/root/trading/sitewide/user-positions",
    "/api/trading/asset-overview",
    "/api/trading/bot-competition",
    "/api/trading/btc-signal",
    "/api/trading/dashboard",
    "/api/trading/live-price",
    "/api/trading/reference-prices",
)


EXPECTED_BROWSER_HTTP_FAILURE_NAMESPACES = (
    "/api/admin/trading/",
    "/api/root/trading/",
    "/api/trading/",
)


def ignored_browser_error(compact: str) -> bool:
    text = str(compact or "")
    if "phase15 forced failure" in text:
        return True
    if "Failed to load resource: the server responded with a status of 503" in text:
        return True
    if "Failed to load resource: the server responded with a status of 404" in text:
        return True
    if text.startswith(("503 ", "404 ")) and any(
        namespace in text for namespace in EXPECTED_BROWSER_HTTP_FAILURE_NAMESPACES
    ):
        return True
    if text.startswith(("503 ", "404 ")) and "/api/videos/" in text and "/realtime-proxy" in text:
        return True
    if text.startswith(("503 ", "404 ")) and any(path in text for path in EXPECTED_BROWSER_HTTP_FAILURE_PATHS):
        return True
    return False
