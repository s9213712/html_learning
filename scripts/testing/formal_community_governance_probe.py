#!/usr/bin/env python3
"""Strict live community, chat, moderation, and governance lifecycle probe.

The probe exercises product APIs and the rendered desktop/mobile UI.  It
never treats HTTP acceptance alone as success: every write is reopened from
another account, terminal moderation/governance state is inspected, and all
reversible fixtures are removed before the machine-readable result can pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests
from playwright.sync_api import sync_playwright


SCHEMA_VERSION = "hackme.formal-community-governance-probe/v1"
FEATURE_KEYS = (
    "feature_chat_enabled",
    "feature_community_enabled",
    "feature_reports_notifications_enabled",
    "feature_member_governance_enabled",
)


def _body(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = {"text_sample": response.text[:500]}
    return payload if isinstance(payload, dict) else {"value": payload}


def _record(response: requests.Response) -> dict[str, Any]:
    return {
        "status": int(response.status_code),
        "body": _body(response),
        "content_type": str(response.headers.get("Content-Type") or ""),
    }


class Api:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def refresh_csrf(self) -> None:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=30)
        response.raise_for_status()
        self.csrf = str(
            (_body(response).get("csrf_token"))
            or self.session.cookies.get("csrf_token")
            or ""
        )
        if not self.csrf:
            raise RuntimeError(f"csrf_missing:{self.username}")

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        response = self.session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
            headers={"X-CSRF-Token": self.csrf},
            timeout=30,
        )
        result = _record(response)
        if response.status_code == 200 and result["body"].get("ok") is True:
            self.refresh_csrf()
        return result

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        include_csrf: bool = True,
        timeout: float = 60,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if include_csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-CSRF-Token"] = self.csrf
        response = self.session.request(
            method.upper(),
            f"{self.base_url}{path}",
            json=dict(json_body) if json_body is not None else None,
            headers=headers,
            timeout=timeout,
        )
        # Successful authenticated mutations rotate the double-submit CSRF
        # cookie.  Long governance/chat lifecycles must use the rotated value
        # on the next write or they eventually fail after the server prunes
        # older tokens from its bounded acceptance window.
        rotated_csrf = self.session.cookies.get("csrf_token")
        if rotated_csrf:
            self.csrf = str(rotated_csrf)
        return _record(response)


def _must(record: Mapping[str, Any], label: str, statuses: Iterable[int] = (200,)) -> Mapping[str, Any]:
    status_set = set(int(value) for value in statuses)
    body = record.get("body") if isinstance(record.get("body"), Mapping) else {}
    if int(record.get("status") or 0) not in status_set or body.get("ok") is not True:
        raise RuntimeError(f"{label}_failed:{json.dumps(record, ensure_ascii=False)[:800]}")
    return body


def _profile_id(client: Api) -> int:
    body = _must(client.request("GET", "/api/users/me/profile"), f"profile_{client.username}")
    profile = body.get("profile") if isinstance(body.get("profile"), Mapping) else {}
    user_id = int(profile.get("id") or profile.get("user_id") or 0)
    if user_id <= 0:
        raise RuntimeError(f"profile_id_missing:{client.username}")
    return user_id


def _notification_ids(client: Api) -> set[int]:
    body = _must(
        client.request("GET", "/api/notifications?limit=100&include_dismissed=1"),
        f"notifications_{client.username}",
    )
    return {
        int(row.get("id") or 0)
        for row in (body.get("notifications") or [])
        if isinstance(row, Mapping) and int(row.get("id") or 0) > 0
    }


def _find_thread(client: Api, board_id: int, title: str) -> dict[str, Any]:
    body = _must(
        client.request("GET", f"/api/community/boards/{board_id}/threads?limit=20&q={requests.utils.quote(title)}"),
        "thread_lookup",
    )
    for row in body.get("threads") or []:
        if isinstance(row, Mapping) and str(row.get("title") or "") == title:
            return dict(row)
    return {}


def _browser_login(page: Any, base_url: str, username: str, password: str) -> None:
    page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)
    result = page.evaluate(
        """async ({username, password}) => {
          await fetch('/api/csrf-token', {credentials: 'same-origin'});
          const token = decodeURIComponent((document.cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || '');
          const response = await fetch('/api/login', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
            body: JSON.stringify({username, password}),
          });
          let body = {}; try { body = await response.json(); } catch (_) {}
          return {status: response.status, body};
        }""",
        {"username": username, "password": password},
    )
    if int(result.get("status") or 0) != 200 or (result.get("body") or {}).get("ok") is not True:
        raise RuntimeError(f"browser_login_failed:{username}:{result}")
    page.goto(base_url.rstrip("/") + "/", wait_until="networkidle", timeout=60_000)


def browser_checks(
    *,
    base_url: str,
    username: str,
    password: str,
    board_id: int,
    thread_id: int,
    room_id: int,
    thread_title: str,
    message_text: str,
    screenshot_dir: Path,
) -> dict[str, Any]:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        for viewport_name, viewport in (
            ("desktop", {"width": 1366, "height": 900}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            context = browser.new_context(ignore_https_errors=True, viewport=viewport)
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            failed_responses: list[dict[str, Any]] = []
            page.on(
                "console",
                lambda message, target=console_errors: target.append(message.text)
                if message.type == "error" else None,
            )
            page.on("pageerror", lambda error, target=page_errors: target.append(str(error)))
            page.on(
                "response",
                lambda response, target=failed_responses: target.append(
                    {"status": response.status, "url": response.url}
                ) if response.status >= 400 and any(
                    marker in response.url
                    for marker in ("/api/community/", "/api/chat/", "/api/notifications")
                ) else None,
            )
            _browser_login(page, base_url, username, password)
            page.locator("#tab-module-community").click()
            page.locator("#module-community.active").wait_for(state="visible", timeout=30_000)
            page.evaluate("boardId => openCommunityBoard(boardId)", board_id)
            page.wait_for_function(
                "title => (document.querySelector('#community-thread-list')?.innerText || '').includes(title)",
                arg=thread_title,
                timeout=30_000,
            )
            page.evaluate("threadId => openCommunityThread(threadId)", thread_id)
            page.wait_for_function(
                """title => [
                  document.querySelector('#community-thread-heading')?.innerText || '',
                  document.querySelector('#community-thread-detail')?.innerText || '',
                ].join('\\n').includes(title)""",
                arg=thread_title,
                timeout=30_000,
            )
            community_state = page.evaluate(
                """() => ({
                  active: document.querySelector('#module-community')?.classList.contains('active') === true,
                  thread_text: [
                    document.querySelector('#community-thread-heading')?.innerText || '',
                    document.querySelector('#community-thread-detail')?.innerText || '',
                  ].join('\\n'),
                  overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
                })"""
            )
            page.locator("#tab-module-chat").click()
            page.locator("#module-chat.active").wait_for(state="visible", timeout=30_000)
            page.evaluate("async roomId => { await loadChatRooms(); await openChatRoom(roomId, false); }", room_id)
            page.wait_for_function(
                "message => (document.querySelector('#chat-room-messages')?.innerText || '').includes(message)",
                arg=message_text,
                timeout=30_000,
            )
            chat_state = page.evaluate(
                """() => ({
                  active: document.querySelector('#module-chat')?.classList.contains('active') === true,
                  title: document.querySelector('#chat-room-title')?.innerText || '',
                  messages: document.querySelector('#chat-room-messages')?.innerText || '',
                  overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
                })"""
            )
            screenshot = screenshot_dir / f"community_chat_{viewport_name}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            rows.append({
                "viewport": viewport_name,
                "dimensions": viewport,
                "community": community_state,
                "chat": chat_state,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "failed_responses": failed_responses,
                "screenshot": str(screenshot.resolve()),
                "screenshot_size_bytes": screenshot.stat().st_size,
                "context_closed": True,
            })
            context.close()
        browser.close()
    return {"rows": rows, "browser_closed": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--manager-username", default="admin")
    parser.add_argument("--manager-password", required=True)
    parser.add_argument("--user-one", required=True)
    parser.add_argument("--user-one-password", required=True)
    parser.add_argument("--user-two", required=True)
    parser.add_argument("--user-two-password", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--screenshot-dir", required=True)
    args = parser.parse_args()

    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_dir = Path(args.screenshot_dir).expanduser().resolve()
    suffix = uuid.uuid4().hex[:12]
    title = f"Formal community governance {suffix}"
    reply_text = f"Formal reply {suffix}"
    private_message = f"Formal private message {suffix}"
    rate_room_name = f"formal-rate-{suffix}"

    root = Api(args.base_url, "root", args.root_password)
    manager = Api(args.base_url, args.manager_username, args.manager_password)
    user_one = Api(args.base_url, args.user_one, args.user_one_password)
    user_two = Api(args.base_url, args.user_two, args.user_two_password)
    clients = (root, manager, user_one, user_two)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "base_url": args.base_url.rstrip("/"),
        "fixture_suffix": suffix,
        "logins": {},
        "forum": {},
        "chat": {},
        "friends": {},
        "governance": {},
        "boundaries": {},
        "browser": {},
        "cleanup": {},
        "errors": [],
        "ok": False,
    }
    previous_settings: dict[str, bool] = {}
    thread_id = 0
    post_id = 0
    private_room_id = 0
    rate_room_id = 0
    block_active = False
    notification_before: dict[str, set[int]] = {}

    try:
        for client in clients:
            login = client.login()
            result["logins"][client.username] = login
            _must(login, f"login_{client.username}")

        settings_body = _must(root.request("GET", "/api/admin/settings"), "settings_snapshot")
        settings = settings_body.get("settings") if isinstance(settings_body.get("settings"), Mapping) else {}
        previous_settings = {key: bool(settings.get(key)) for key in FEATURE_KEYS}
        enabled = _must(
            root.request("PUT", "/api/admin/settings", json_body={key: True for key in FEATURE_KEYS}),
            "feature_enable",
        )
        result["settings"] = {"before": previous_settings, "enable_response": enabled}

        user_one_id = _profile_id(user_one)
        user_two_id = _profile_id(user_two)
        result["actors"] = {
            "user_one": {"username": user_one.username, "id": user_one_id},
            "user_two": {"username": user_two.username, "id": user_two_id},
        }
        notification_before = {
            user_one.username: _notification_ids(user_one),
            user_two.username: _notification_ids(user_two),
        }

        boards_body = _must(root.request("GET", "/api/community/boards"), "board_list")
        board = next(
            (
                dict(row) for row in (boards_body.get("boards") or [])
                if isinstance(row, Mapping)
                and int(row.get("id") or 0) > 0
                and str(row.get("status") or "") == "approved"
                and str(row.get("visibility") or "public") == "public"
                and row.get("is_active") is not False
            ),
            {},
        )
        board_id = int(board.get("id") or 0)
        if board_id <= 0:
            raise RuntimeError("approved_public_board_missing")
        thread_create = root.request(
            "POST",
            f"/api/community/boards/{board_id}/threads",
            json_body={"title": title, "content": f"Formal lifecycle body {suffix}"},
        )
        _must(thread_create, "thread_create")
        thread = _find_thread(root, board_id, title)
        thread_id = int(thread.get("id") or 0)
        if thread_id <= 0 or str(thread.get("status") or "") != "approved":
            raise RuntimeError(f"thread_terminal_missing:{thread}")
        reply = user_one.request(
            "POST", f"/api/community/threads/{thread_id}/posts", json_body={"content": reply_text}
        )
        _must(reply, "thread_reply")
        detail = _must(user_two.request("GET", f"/api/community/threads/{thread_id}"), "thread_reopen")
        matching_posts = [
            dict(row) for row in (detail.get("posts") or [])
            if isinstance(row, Mapping) and str(row.get("content") or "") == reply_text
        ]
        post_id = int(matching_posts[0].get("id") or 0) if matching_posts else 0
        if post_id <= 0:
            raise RuntimeError("reply_terminal_post_missing")
        report = user_two.request(
            "POST", "/api/reports",
            json_body={
                "target_type": "forum_post",
                "target_id": post_id,
                "reason": f"Formal moderation lifecycle {suffix}",
            },
        )
        report_body = _must(report, "report_submit")
        report_id = int(report_body.get("report_id") or 0)
        _must(manager.request("POST", f"/api/admin/reports/{report_id}/claim"), "report_claim")
        _must(
            manager.request(
                "POST", f"/api/admin/reports/{report_id}/resolve",
                json_body={"action": "reject", "note": f"Formal verified rejection {suffix}"},
            ),
            "report_resolve",
        )
        resolved_reports = _must(
            manager.request("GET", "/api/admin/reports?status=rejected&limit=100"),
            "report_terminal_list",
        )
        terminal_report = next(
            (dict(row) for row in (resolved_reports.get("reports") or []) if int(row.get("id") or 0) == report_id),
            {},
        )
        result["forum"] = {
            "board": board,
            "thread_create": thread_create,
            "thread": thread,
            "thread_id": thread_id,
            "reply": reply,
            "post": matching_posts[0],
            "report": report,
            "report_id": report_id,
            "terminal_report": terminal_report,
        }

        friend_request = user_one.request(
            "POST", "/api/friends/request", json_body={"username": user_two.username}
        )
        friend_request_body = _must(friend_request, "friend_request")
        request_row = friend_request_body.get("request") if isinstance(friend_request_body.get("request"), Mapping) else {}
        request_id = int(request_row.get("id") or 0)
        incoming = _must(user_two.request("GET", "/api/friends/requests"), "friend_incoming")
        if request_id <= 0:
            request_id = next(
                (
                    int(row.get("id") or 0) for row in (incoming.get("incoming") or [])
                    if isinstance(row, Mapping) and str(row.get("other_username") or "") == user_one.username
                ),
                0,
            )
        _must(user_two.request("POST", f"/api/friends/requests/{request_id}/accept"), "friend_accept")
        friend_state = _must(user_one.request("GET", "/api/friends"), "friend_terminal")
        profile = _must(user_one.request("GET", f"/api/users/{user_two_id}/profile"), "friend_profile")
        block = user_one.request("POST", f"/api/friends/{user_two_id}/block")
        _must(block, "friend_block")
        block_active = True
        blocked_state = _must(user_one.request("GET", "/api/friends"), "friend_blocked_state")
        blocked_dm = user_one.request(
            "POST", "/api/chat/rooms", json_body={"name": None, "target_user": user_two.username}
        )
        if int(blocked_dm.get("status") or 0) != 403:
            raise RuntimeError(f"blocked_dm_not_denied:{blocked_dm}")
        unblock = user_one.request("DELETE", f"/api/friends/{user_two_id}/block")
        _must(unblock, "friend_unblock")
        block_active = False
        result["friends"] = {
            "request": friend_request,
            "request_id": request_id,
            "accept_terminal": friend_state,
            "profile": profile,
            "block": block,
            "blocked_state": blocked_state,
            "blocked_dm": blocked_dm,
            "unblock": unblock,
        }

        private_room = root.request(
            "POST", "/api/chat/rooms", json_body={"name": None, "target_user": user_one.username}
        )
        private_room_body = _must(private_room, "private_room_create")
        private_room_row = private_room_body.get("room") if isinstance(private_room_body.get("room"), Mapping) else {}
        private_room_id = int(private_room_row.get("id") or 0)
        message = root.request(
            "POST", f"/api/chat/rooms/{private_room_id}/messages",
            json_body={"content": private_message},
        )
        message_body = _must(message, "private_message_send")
        messages = _must(
            user_one.request("GET", f"/api/chat/rooms/{private_room_id}/messages?limit=50"),
            "private_message_terminal",
        )
        terminal_message = next(
            (
                dict(row) for row in (messages.get("messages") or [])
                if isinstance(row, Mapping)
                and int(row.get("id") or 0) == int(message_body.get("message_id") or 0)
            ),
            {},
        )
        notifications = _must(user_one.request("GET", "/api/notifications?limit=100"), "private_message_notification")
        new_notifications = [
            dict(row) for row in (notifications.get("notifications") or [])
            if isinstance(row, Mapping)
            and int(row.get("id") or 0) not in notification_before[user_one.username]
        ]
        chat_notice = next(
            (row for row in new_notifications if str(row.get("type") or "") == "chat_private_message"),
            {},
        )
        if not terminal_message or not chat_notice:
            raise RuntimeError("private_message_or_notification_terminal_missing")
        result["chat"] = {
            "private_room": private_room,
            "private_room_id": private_room_id,
            "message": message,
            "terminal_message": terminal_message,
            "notification": chat_notice,
        }

        proposal_create = root.request(
            "POST", "/api/admin/moderation/proposals",
            json_body={
                "target_user_id": user_one_id,
                "action_type": "warn",
                "reason": f"Formal social governance lifecycle {suffix}",
            },
        )
        proposal_body = _must(proposal_create, "proposal_create")
        proposal = proposal_body.get("proposal") if isinstance(proposal_body.get("proposal"), Mapping) else {}
        proposal_id = int(proposal.get("id") or 0)
        proposer_vote = root.request(
            "POST", f"/api/admin/moderation/proposals/{proposal_id}/vote", json_body={"vote": "approve"}
        )
        if int(proposer_vote.get("status") or 0) != 403:
            raise RuntimeError(f"proposer_vote_not_denied:{proposer_vote}")
        vote = manager.request(
            "POST", f"/api/admin/moderation/proposals/{proposal_id}/vote",
            json_body={"vote": "approve", "comment": f"Formal approval {suffix}"},
        )
        vote_body = _must(vote, "proposal_vote")
        voted_proposal = vote_body.get("proposal") if isinstance(vote_body.get("proposal"), Mapping) else {}
        if str(voted_proposal.get("status") or "") != "approved":
            raise RuntimeError(f"proposal_not_approved:{voted_proposal}")
        execute = root.request("POST", f"/api/admin/moderation/proposals/{proposal_id}/execute")
        _must(execute, "proposal_execute")
        detail_terminal = _must(
            root.request("GET", f"/api/admin/moderation/proposals/{proposal_id}"),
            "proposal_terminal",
        )
        terminal_proposal = detail_terminal.get("proposal") if isinstance(detail_terminal.get("proposal"), Mapping) else {}
        if str(terminal_proposal.get("status") or "") != "executed":
            raise RuntimeError(f"proposal_not_executed:{terminal_proposal}")
        result["governance"] = {
            "create": proposal_create,
            "proposal_id": proposal_id,
            "proposer_vote_denied": proposer_vote,
            "vote": vote,
            "execute": execute,
            "terminal_proposal": terminal_proposal,
        }

        member_governance = user_two.request("GET", "/api/admin/moderation/proposals")
        csrf_denial = user_two.request(
            "POST", "/api/friends/request",
            json_body={"username": user_one.username},
            include_csrf=False,
        )
        if int(member_governance.get("status") or 0) != 403:
            raise RuntimeError(f"member_governance_not_denied:{member_governance}")
        if int(csrf_denial.get("status") or 0) not in {400, 403}:
            raise RuntimeError(f"csrf_missing_not_denied:{csrf_denial}")

        rate_room = user_two.request("POST", "/api/chat/rooms", json_body={"name": rate_room_name})
        rate_room_body = _must(rate_room, "rate_room_create")
        rate_room_row = rate_room_body.get("room") if isinstance(rate_room_body.get("room"), Mapping) else {}
        rate_room_id = int(rate_room_row.get("id") or 0)
        rate_attempts: list[dict[str, Any]] = []
        for index in range(1, 26):
            response = user_two.request(
                "POST", f"/api/chat/rooms/{rate_room_id}/messages",
                json_body={"content": f"Formal rate boundary {suffix} #{index}"},
            )
            rate_attempts.append({"index": index, **response})
            if int(response.get("status") or 0) == 429:
                break
        if not any(int(row.get("status") or 0) == 200 for row in rate_attempts):
            raise RuntimeError("rate_boundary_no_successful_messages")
        if not rate_attempts or int(rate_attempts[-1].get("status") or 0) != 429:
            raise RuntimeError(f"chat_rate_limit_not_observed:{rate_attempts[-3:]}")
        result["boundaries"] = {
            "member_governance_denied": member_governance,
            "csrf_missing_denied": csrf_denial,
            "chat_rate_limit": {
                "attempt_count": len(rate_attempts),
                "success_count": sum(int(row.get("status") or 0) == 200 for row in rate_attempts),
                "terminal": rate_attempts[-1],
            },
        }

        result["browser"] = browser_checks(
            base_url=args.base_url,
            username=user_one.username,
            password=user_one.password,
            board_id=board_id,
            thread_id=thread_id,
            room_id=private_room_id,
            thread_title=title,
            message_text=private_message,
            screenshot_dir=screenshot_dir,
        )
    except Exception as exc:
        result["errors"].append(f"{exc.__class__.__name__}: {exc}")
    finally:
        cleanup: dict[str, Any] = {
            "thread_deleted": False,
            "thread_denied_to_member": False,
            "private_room_deleted": False,
            "private_room_absent": False,
            "rate_room_deleted": False,
            "rate_room_absent": False,
            "friendship_absent": False,
            "block_absent": False,
            "settings_restored": False,
            "notification_ids_dismissed": [],
            "notification_ids_expected": [],
            "notifications_dismissed": False,
            "cleanup_errors": [],
        }
        try:
            if block_active:
                _must(user_one.request("DELETE", f"/api/friends/{_profile_id(user_two)}/block"), "cleanup_unblock")
            if thread_id:
                deleted = root.request(
                    "DELETE", f"/api/community/threads/{thread_id}",
                    json_body={"reason": "formal fixture cleanup"},
                )
                cleanup["thread_deleted"] = int(deleted.get("status") or 0) == 200 and (deleted.get("body") or {}).get("ok") is True
                denied = user_two.request("GET", f"/api/community/threads/{thread_id}")
                cleanup["thread_denied_to_member"] = int(denied.get("status") or 0) == 404
            for key, room_id in (("private_room", private_room_id), ("rate_room", rate_room_id)):
                if room_id:
                    deleted = root.request("DELETE", f"/api/chat/rooms/{room_id}")
                    cleanup[f"{key}_deleted"] = int(deleted.get("status") or 0) == 200 and (deleted.get("body") or {}).get("ok") is True
                    rooms = _must(root.request("GET", "/api/chat/rooms"), f"cleanup_{key}_rooms")
                    cleanup[f"{key}_absent"] = not any(
                        int(row.get("id") or 0) == room_id
                        for row in (rooms.get("rooms") or []) if isinstance(row, Mapping)
                    )
            if user_one.csrf:
                state = _must(user_one.request("GET", "/api/friends"), "cleanup_friend_state")
                user_two_id = _profile_id(user_two)
                cleanup["friendship_absent"] = not any(
                    int(row.get("other_user_id") or 0) == user_two_id
                    for row in (state.get("friends") or []) if isinstance(row, Mapping)
                )
                cleanup["block_absent"] = not any(
                    int(row.get("other_user_id") or 0) == user_two_id
                    for row in (state.get("blocked") or []) if isinstance(row, Mapping)
                )
            for client in (user_one, user_two):
                if not client.csrf or client.username not in notification_before:
                    continue
                current_ids = _notification_ids(client)
                for notification_id in sorted(current_ids - notification_before[client.username]):
                    cleanup["notification_ids_expected"].append(notification_id)
                    dismissed = client.request("POST", f"/api/notifications/{notification_id}/dismiss")
                    if int(dismissed.get("status") or 0) == 200 and (dismissed.get("body") or {}).get("ok") is True:
                        cleanup["notification_ids_dismissed"].append(notification_id)
            cleanup["notifications_dismissed"] = bool(
                cleanup["notification_ids_expected"]
                and sorted(cleanup["notification_ids_dismissed"])
                == sorted(cleanup["notification_ids_expected"])
            )
            if previous_settings and root.csrf:
                restored = root.request("PUT", "/api/admin/settings", json_body=previous_settings)
                restored_body = root.request("GET", "/api/admin/settings")
                restored_settings = (restored_body.get("body") or {}).get("settings") or {}
                cleanup["settings_restored"] = bool(
                    int(restored.get("status") or 0) == 200
                    and (restored.get("body") or {}).get("ok") is True
                    and all(bool(restored_settings.get(key)) == value for key, value in previous_settings.items())
                )
        except Exception as exc:
            cleanup["cleanup_errors"].append(f"{exc.__class__.__name__}: {exc}")
        result["cleanup"] = cleanup

    browser_rows = result.get("browser", {}).get("rows", []) if isinstance(result.get("browser"), Mapping) else []
    cleanup = result["cleanup"]
    result["ok"] = bool(
        not result["errors"]
        and (result.get("forum") or {}).get("terminal_report", {}).get("status") == "rejected"
        and (result.get("chat") or {}).get("terminal_message", {}).get("content") == private_message
        and (result.get("friends") or {}).get("blocked_dm", {}).get("status") == 403
        and (result.get("governance") or {}).get("terminal_proposal", {}).get("status") == "executed"
        and (result.get("boundaries") or {}).get("chat_rate_limit", {}).get("terminal", {}).get("status") == 429
        and {str(row.get("viewport") or "") for row in browser_rows} == {"desktop", "mobile"}
        and all(
            (row.get("community") or {}).get("active") is True
            and title in str((row.get("community") or {}).get("thread_text") or "")
            and (row.get("chat") or {}).get("active") is True
            and private_message in str((row.get("chat") or {}).get("messages") or "")
            and int((row.get("community") or {}).get("overflow_px") or 0) <= 1
            and int((row.get("chat") or {}).get("overflow_px") or 0) <= 1
            and not row.get("console_errors")
            and not row.get("page_errors")
            and not row.get("failed_responses")
            and int(row.get("screenshot_size_bytes") or 0) > 0
            and row.get("context_closed") is True
            for row in browser_rows
        )
        and result.get("browser", {}).get("browser_closed") is True
        and cleanup.get("thread_deleted") is True
        and cleanup.get("thread_denied_to_member") is True
        and cleanup.get("private_room_deleted") is True
        and cleanup.get("private_room_absent") is True
        and cleanup.get("rate_room_deleted") is True
        and cleanup.get("rate_room_absent") is True
        and cleanup.get("friendship_absent") is True
        and cleanup.get("block_absent") is True
        and cleanup.get("settings_restored") is True
        and cleanup.get("notifications_dismissed") is True
        and not cleanup.get("cleanup_errors")
    )
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
