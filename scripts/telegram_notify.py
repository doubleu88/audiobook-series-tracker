#!/usr/bin/env python3
"""Sends a plain-text message to a Telegram chat via the Bot API.

Shared by pr-telegram-notify.yml (new-PR notice) and gemini-pr-review.yml
(review-verdict notice), so the two workflows don't duplicate the same
curl/urllib call.

Usage:
  telegram_notify.py <message>

Required env vars:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Exits 1 on a non-200 response from Telegram rather than swallowing the
failure, so a broken notification shows up as a failed workflow step.
"""
import json
import os
import sys
import urllib.request

TELEGRAM_URL_TMPL = "https://api.telegram.org/bot{token}/sendMessage"


def main() -> None:
    message = sys.argv[1]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    req = urllib.request.Request(
        TELEGRAM_URL_TMPL.format(token=token),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 -- must fail loudly, not silently drop the notification
        print(f"Telegram notification failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
