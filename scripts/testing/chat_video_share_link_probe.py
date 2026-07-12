#!/usr/bin/env python3
"""Live probe: video share links remain usable inside chat messages."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(base_url: str, username: str, password: str) -> dict:
    from playwright.sync_api import sync_playwright

    link = f"{base_url.rstrip('/')}/shared/videos/probeToken_ABC-123#vk=probe-fragment_456"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(ignore_https_errors=True, viewport={"width": 1280, "height": 860})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded")
        result = page.evaluate(
            """async ({username, password, link}) => {
              const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
              };
              const api = async (method, path, payload = null) => {
                if (!cookieValue('csrf_token')) {
                  await fetch('/api/csrf-token', {credentials: 'same-origin'});
                }
                const opts = {
                  method,
                  credentials: 'same-origin',
                  headers: {'Accept': 'application/json', 'X-CSRF-Token': cookieValue('csrf_token') || ''}
                };
                if (payload !== null) {
                  opts.headers['Content-Type'] = 'application/json';
                  opts.body = JSON.stringify(payload);
                }
                const response = await fetch(path, opts);
                const text = await response.text();
                let body = {};
                try { body = text ? JSON.parse(text) : {}; } catch (err) { body = {raw: text.slice(0, 500)}; }
                return {status: response.status, ok: response.ok, body};
              };
              const login = await api('POST', '/api/login', {username, password});
              if (!login.ok || !login.body.ok) return {ok: false, step: 'login', login};
              await api('GET', '/api/csrf-token');
              const settings = await api('GET', '/api/admin/settings');
              const previousChatEnabled = Boolean(settings.body?.settings?.feature_chat_enabled);
              const enabled = await api('PUT', '/api/admin/settings', {feature_chat_enabled: true});
              if (!enabled.ok || !enabled.body.ok) return {ok: false, step: 'enable_chat', enabled};
              const rooms = await api('GET', '/api/chat/rooms');
              if (!rooms.ok || !rooms.body.ok) return {ok: false, step: 'rooms', rooms};
              let room = (rooms.body.rooms || [])[0];
              if (!room) {
                const created = await api('POST', '/api/chat/rooms', {name: `share link probe ${Date.now()}`});
                if (!created.ok || !created.body.ok) return {ok: false, step: 'create_room', created};
                room = created.body.room;
              }
              const sent = await api('POST', `/api/chat/rooms/${encodeURIComponent(room.id)}/messages`, {
                content: `影音分享 ${link}`
              });
              if (!sent.ok || !sent.body.ok) return {ok: false, step: 'send', sent};
              const messages = await api('GET', `/api/chat/rooms/${encodeURIComponent(room.id)}/messages?limit=20`);
              if (!messages.ok || !messages.body.ok) return {ok: false, step: 'read', messages};
              const message = (messages.body.messages || []).find(item => Number(item.id) === Number(sent.body.message_id));
              if (!message || !String(message.content || '').includes('/shared/videos/')) {
                return {ok: false, step: 'message_content', message};
              }
              let target = document.getElementById('chat-room-messages');
              if (!target) {
                target = document.createElement('div');
                target.id = 'chat-room-messages';
                document.body.appendChild(target);
              }
              renderChatMessages([message]);
              const anchor = target.querySelector('a.chat-inline-link');
              const href = anchor ? anchor.href : '';
              const text = anchor ? anchor.textContent : '';
              if (!previousChatEnabled) {
                await api('PUT', '/api/admin/settings', {feature_chat_enabled: false});
              }
              return {
                ok: Boolean(anchor && href.includes('/shared/videos/probeToken_ABC-123') && href.includes('#vk=probe-fragment_456')),
                step: 'render',
                href,
                text,
                message_id: sent.body.message_id,
                room_id: room.id,
              };
            }""",
            {"username": username, "password": password, "link": link},
        )
        browser.close()
        result["browser_errors"] = errors
        result["ok"] = bool(result.get("ok")) and not errors
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:54347")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser, "--password")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = run(args.base_url, args.username, args.password)
    result["generated_at"] = int(time.time())
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"CHAT VIDEO SHARE LINK PROBE: {'PASS' if result.get('ok') else 'FAIL'}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
