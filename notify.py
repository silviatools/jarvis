#!/usr/bin/env python3
"""
Jarvis — Telegram notifier for cleaning chores.

Any user who sends ANY message (or /start) to the bot gets subscribed
automatically. Notifications are broadcast to all subscribers.

Setup:
  1. pip3 install requests
  2. In the app: Settings → General → export "jarvis_notify_config.json"
     and place it next to this script.
  3. Run: python3 notify.py
     Or via cron (runs every minute, script manages its own loop):
       * * * * * /usr/bin/python3 /path/to/notify.py --once
"""

import json
import sys
import time
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed.  Run: pip3 install requests")
    sys.exit(1)

CONFIG_FILE     = Path(__file__).parent / "jarvis_notify_config.json"
SUBSCRIBERS_FILE = Path(__file__).parent / "jarvis_subscribers.json"

FREQ_DAYS = {
    "daily":    1,
    "every2":   2,
    "every3":   3,
    "weekly":   7,
    "biweekly": 14,
    "monthly":  30,
}

WELCOME_TEXT = (
    "👋 <b>Jarvis подключён!</b>\n\n"
    "Вы будете получать напоминания об уборке в настроенное время."
)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Config not found: {CONFIG_FILE}\n"
            "Export it in the app: Настройки → Общее → «Скачать конфиг для notify.py»"
        )
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        with SUBSCRIBERS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0, "chat_ids": []}


def save_subscribers(subs: dict):
    with SUBSCRIBERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


def get_frequency_days(chore: dict) -> int:
    if chore.get("frequency") == "custom":
        return max(1, int(chore.get("customDays") or 7))
    return FREQ_DAYS.get(chore.get("frequency", "weekly"), 7)


def is_due_today(chore: dict) -> bool:
    last_done = chore.get("lastDone")
    if not last_done:
        return True
    last_date = date.fromisoformat(last_done)
    return last_date + timedelta(days=get_frequency_days(chore)) <= date.today()


def api_post(token: str, method: str, payload: dict) -> dict | None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return r.json()
        print(f"  [{method}] error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [{method}] request failed: {e}")
    return None


def send_message(token: str, chat_id: int, text: str):
    api_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


# ── core logic ────────────────────────────────────────────────────────────────

def poll_updates(token: str, subs: dict) -> bool:
    """Fetch new Telegram updates, register new subscribers. Returns True if subs changed."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, params={"offset": subs["offset"], "timeout": 0}, timeout=15)
        if not r.ok:
            return False
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"  getUpdates failed: {e}")
        return False

    changed = False
    for upd in updates:
        subs["offset"] = upd["update_id"] + 1

        # extract chat_id from message or channel_post
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]

        if chat_id not in subs["chat_ids"]:
            subs["chat_ids"].append(chat_id)
            changed = True
            print(f"  New subscriber: {chat_id}")
            send_message(token, chat_id, WELCOME_TEXT)

    return changed


def send_notifications(token: str, subs: dict, config: dict):
    now_str = datetime.now().strftime("%H:%M")
    due_chores = [
        c for c in config.get("chores", [])
        if c.get("notify")
        and c.get("notifyTime", "") == now_str
        and is_due_today(c)
    ]
    if not due_chores or not subs["chat_ids"]:
        return

    for chore in due_chores:
        text = f"🧹 <b>Уборка — напоминание</b>\n\n{chore.get('name', 'Дело')}"
        print(f"[{now_str}] Notifying {len(subs['chat_ids'])} subscriber(s): {chore['name']}")
        for chat_id in subs["chat_ids"]:
            send_message(token, chat_id, text)


def tick(once: bool = False):
    try:
        config = load_config()
    except FileNotFoundError as e:
        print(e)
        return
    except json.JSONDecodeError as e:
        print(f"Config parse error: {e}")
        return

    token = config.get("telegram", {}).get("token", "").strip()
    if not token:
        print("Telegram token not set in jarvis_notify_config.json")
        return

    subs = load_subscribers()
    changed = poll_updates(token, subs)
    if not once:
        send_notifications(token, subs, config)
    if changed or not SUBSCRIBERS_FILE.exists():
        save_subscribers(subs)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single tick (for cron usage) and exit"
    )
    args = parser.parse_args()

    if args.once:
        tick(once=False)
        return

    print(f"Jarvis Notifier started.")
    print(f"  Config:      {CONFIG_FILE}")
    print(f"  Subscribers: {SUBSCRIBERS_FILE}")
    print("Any user who messages the bot will be auto-subscribed.")
    print("Press Ctrl+C to stop.\n")

    while True:
        tick()
        now = datetime.now()
        time.sleep(60 - now.second)


if __name__ == "__main__":
    main()
