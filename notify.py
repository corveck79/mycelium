import logging

import requests

from config import (
    DISCORD_WEBHOOK_URL,
    NOTIFY_ON_FAILURE,
    NOTIFY_ON_SUCCESS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

log = logging.getLogger(__name__)


def send(title: str, message: str, success: bool = True) -> None:
    if success and not NOTIFY_ON_SUCCESS:
        return
    if not success and not NOTIFY_ON_FAILURE:
        return
    if DISCORD_WEBHOOK_URL:
        _discord(title, message, success)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _telegram(title, message, success)


def _discord(title: str, message: str, success: bool) -> None:
    color = 0x4ADE80 if success else 0xF87171
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
        }],
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as exc:
        log.warning("Discord notify failed: %s", exc)


def _telegram(title: str, message: str, success: bool) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    emoji = "✅" if success else "❌"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{emoji} *{title}*\n{message}",
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        log.warning("Telegram notify failed: %s", exc)


def test() -> dict:
    results = {}
    if DISCORD_WEBHOOK_URL:
        try:
            r = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": "🧪 Test notification from seerr-torbox-webhook"},
                timeout=10,
            )
            results["discord"] = "ok" if r.status_code < 400 else f"http {r.status_code}"
        except Exception as exc:
            results["discord"] = str(exc)[:100]
    else:
        results["discord"] = "not configured"

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            r = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": "🧪 Test notification from seerr-torbox-webhook"},
                timeout=10,
            )
            results["telegram"] = "ok" if r.status_code < 400 else f"http {r.status_code}"
        except Exception as exc:
            results["telegram"] = str(exc)[:100]
    else:
        results["telegram"] = "not configured"
    return results
