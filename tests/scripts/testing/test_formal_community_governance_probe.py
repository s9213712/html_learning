from __future__ import annotations

import inspect

import requests

from scripts.testing.formal_community_governance_probe import Api, browser_checks


def test_api_uses_rotated_csrf_cookie_on_next_write(monkeypatch) -> None:
    client = Api("https://127.0.0.1:5027", "member", "secret")
    client.csrf = "csrf-before"
    client.session.cookies.set("csrf_token", "csrf-before")
    sent_headers: list[dict[str, str]] = []

    def fake_request(method, url, *, json, headers, timeout):
        sent_headers.append(dict(headers))
        if len(sent_headers) == 1:
            client.session.cookies.set("csrf_token", "csrf-after")
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b'{"ok":true}'
        return response

    monkeypatch.setattr(client.session, "request", fake_request)

    client.request("POST", "/api/first", json_body={})
    client.request("POST", "/api/second", json_body={})

    assert sent_headers[0]["X-CSRF-Token"] == "csrf-before"
    assert sent_headers[1]["X-CSRF-Token"] == "csrf-after"


def test_browser_thread_evidence_includes_separate_heading_element() -> None:
    source = inspect.getsource(browser_checks)

    assert "#community-thread-heading" in source
    assert "#community-thread-detail" in source
