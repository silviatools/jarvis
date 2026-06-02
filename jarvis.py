#!/usr/bin/env python3
"""
jarvis.py — HTTP server + Telegram notifier in one script.

Local:
  python3 jarvis.py              → http://localhost:8000

Railway:
  Set env vars in Railway dashboard:
    TELEGRAM_TOKEN   — bot token (optional; can also be set in the app UI)
    DATA_DIR         — path to a Railway Volume mount (e.g. /data)
                       for persistent subscribers list

The app auto-syncs its Telegram config via POST /api/config.
Any Telegram user who messages the bot is auto-subscribed.

Requirements: pip3 install requests
"""

import json
import os
import time
import threading
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Moscow time is UTC+3, no DST (since 2014) — reliable without tzdata
MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(MSK)

def today_msk() -> date:
    return now_msk().date()

try:
    import requests
except ImportError:
    requests = None
    print("NOTE: 'requests' not installed — Telegram disabled. Run: pip3 install requests\n")

DIR      = Path(__file__).parent
HTML_FILE = DIR / "index (9).html"

# Persistent data lives in DATA_DIR (Railway Volume) if set, else next to the script
DATA_DIR         = Path(os.environ.get("DATA_DIR", str(DIR)))
CONFIG_FILE      = DATA_DIR / "jarvis_notify_config.json"
SUBSCRIBERS_FILE = DATA_DIR / "jarvis_subscribers.json"
APP_DATA_FILE    = DATA_DIR / "jarvis_app_data.json"

FREQ_DAYS = {
    "daily": 1, "every2": 2, "every3": 3,
    "weekly": 7, "biweekly": 14, "monthly": 30,
}

WELCOME_TEXT = (
    "👋 <b>Jarvis подключён!</b>\n\n"
    "Вы будете получать напоминания об уборке в настроенное время."
)


# ── helpers ────────────────────────────────────────────────────────────────

def get_token() -> str:
    """Token priority: env var → app data file → config file."""
    env_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if env_token:
        return env_token
    if APP_DATA_FILE.exists():
        try:
            app_data = json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
            token = app_data.get("settings", {}).get("telegramToken", "").strip()
            if token:
                return token
        except Exception:
            pass
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("telegram", {}).get("token", "").strip()
        except Exception:
            pass
    return ""


def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        try:
            with SUBSCRIBERS_FILE.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"offset": 0, "chat_ids": []}


def save_subscribers(subs: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SUBSCRIBERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


def freq_days(chore: dict) -> int:
    if chore.get("frequency") == "custom":
        return max(1, int(chore.get("customDays") or 7))
    return FREQ_DAYS.get(chore.get("frequency", "weekly"), 7)


def is_due_today(chore: dict) -> bool:
    last = chore.get("lastDone")
    if not last:
        return True
    return date.fromisoformat(last) + timedelta(days=freq_days(chore)) <= today_msk()


# ── telegram ───────────────────────────────────────────────────────────────

def tg_post(token: str, method: str, payload: dict):
    if not requests:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload, timeout=10,
        )
        return r.json() if r.ok else None
    except Exception as e:
        print(f"  [{method}] {e}")
        return None


def send_message(token: str, chat_id: int, text: str):
    tg_post(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
    })


def poll_updates(token: str, subs: dict) -> bool:
    if not requests:
        return False
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": subs["offset"], "timeout": 0},
            timeout=15,
        )
        updates = r.json().get("result", []) if r.ok else []
    except Exception as e:
        print(f"  getUpdates: {e}")
        return False

    changed = False
    for upd in updates:
        subs["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        cid = msg["chat"]["id"]
        if cid not in subs["chat_ids"]:
            subs["chat_ids"].append(cid)
            changed = True
            print(f"  New subscriber: {cid}")
            send_message(token, cid, WELCOME_TEXT)

    return changed


# ── notifier loop ──────────────────────────────────────────────────────────

def notifier_loop():
    print("Notifier thread started.")
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"Notifier error: {e}")
        now = datetime.now()
        time.sleep(60 - now.second)


def _tick():
    token = get_token()
    if not token:
        return

    subs = load_subscribers()
    changed = poll_updates(token, subs)

    # Read chores from app data (source of truth), fall back to config file
    chores = []
    if APP_DATA_FILE.exists():
        try:
            app_data = json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
            chores = [c for c in app_data.get("chores", []) if not c.get("archived")]
        except Exception:
            pass
    if not chores and CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            chores = config.get("chores", [])
        except Exception:
            pass

    now_str = now_msk().strftime("%H:%M")
    for chore in chores:
        if not chore.get("notify"):
            continue
        if chore.get("notifyTime", "") != now_str:
            continue
        if not is_due_today(chore):
            continue
        text = f"🏠 <b>По дому — напоминание</b>\n\n{chore.get('name', 'Дело')}"
        print(f"[{now_str} MSK] → {chore['name']} ({len(subs['chat_ids'])} subscriber(s))")
        for cid in subs["chat_ids"]:
            send_message(token, cid, text)

    # Boss tasks: days stored as JS getDay() (0=Sun,1=Mon..6=Sat)
    today_js = today_msk().isoweekday() % 7
    boss_tasks = []
    if APP_DATA_FILE.exists():
        try:
            app_data_raw = json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
            boss_tasks = [t for t in app_data_raw.get("bossTasks", []) if not t.get("archived")]
        except Exception:
            pass
    for task in boss_tasks:
        if not task.get("notify"):
            continue
        if task.get("notifyTime", "") != now_str:
            continue
        if today_js not in (task.get("days") or []):
            continue
        text = f"💼 <b>Босс — напоминание</b>\n\n{task.get('name', 'Задача')}"
        print(f"[{now_str} MSK] → boss: {task['name']} ({len(subs['chat_ids'])} subscriber(s))")
        for cid in subs["chat_ids"]:
            send_message(token, cid, text)

    if changed:
        save_subscribers(subs)


# ── HTTP handler ───────────────────────────────────────────────────────────

class JarvisHandler(SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/photos/"):
            filename = self.path[len("/api/photos/"):]
            if "/" in filename or ".." in filename or not filename:
                self._json(400, {"error": "invalid"})
                return
            photo_path = DATA_DIR / "photos" / filename
            if photo_path.exists():
                photo_path.unlink()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path in ("/english", "/english.html"):
            p = Path(__file__).parent / "english.html"
            content = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/subscribers":
            subs = load_subscribers()
            self._json(200, {"count": len(subs["chat_ids"])})
        elif self.path == "/api/debug":
            token = get_token()
            subs = load_subscribers()
            chores = []
            if CONFIG_FILE.exists():
                try:
                    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                    chores = cfg.get("chores", [])
                except Exception:
                    pass
            now = now_msk()
            self._json(200, {
                "server_time": now.strftime("%H:%M:%S"),
                "server_date": now.strftime("%Y-%m-%d"),
                "timezone": "Europe/Moscow (UTC+3, hardcoded)",
                "token_present": bool(token),
                "token_prefix": token[:10] + "..." if token else "",
                "subscribers": len(subs["chat_ids"]),
                "config_file_exists": CONFIG_FILE.exists(),
                "app_data_file_exists": APP_DATA_FILE.exists(),
                "chores": [{"name": c.get("name"), "notifyTime": c.get("notifyTime"), "notify": c.get("notify"), "lastDone": c.get("lastDone")} for c in chores],
            })
        elif self.path.startswith("/api/photos/"):
            filename = self.path[len("/api/photos/"):]
            if "/" in filename or ".." in filename or not filename:
                self._json(400, {"error": "invalid"})
                return
            photo_path = DATA_DIR / "photos" / filename
            if photo_path.exists():
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                ct = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
                      "webp":"image/webp","heic":"image/heic","gif":"image/gif"}.get(ext, "application/octet-stream")
                content = photo_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "public, max-age=31536000")
                self._cors()
                self.end_headers()
                self.wfile.write(content)
            else:
                self._json(404, {"error": "not found"})
        elif self.path == "/api/data":
            if APP_DATA_FILE.exists():
                try:
                    content = APP_DATA_FILE.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self._json(200, {})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                config = json.loads(body)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif self.path.startswith("/api/photos"):
            import uuid as _uuid
            ext = "jpg"
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("ext="):
                        raw = part[4:].lower()[:5]
                        if raw in ("jpg", "jpeg", "png", "webp", "heic", "gif"):
                            ext = raw
                        break
            length = int(self.headers.get("Content-Length", 0))
            MAX_PHOTO = 20 * 1024 * 1024  # 20 MB hard cap
            if length > MAX_PHOTO:
                self._json(413, {"error": "file too large"})
                return
            body = self.rfile.read(length) if length else self.rfile.read(MAX_PHOTO)
            if not body:
                self._json(400, {"error": "empty body"})
                return
            if len(body) > MAX_PHOTO:
                self._json(413, {"error": "file too large"})
                return
            filename = str(_uuid.uuid4()) + "." + ext
            photos_dir = DATA_DIR / "photos"
            photos_dir.mkdir(parents=True, exist_ok=True)
            (photos_dir / filename).write_bytes(body)
            self._json(200, {"filename": filename})
        elif self.path == "/api/data":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                app_data = json.loads(body)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                APP_DATA_FILE.write_text(
                    json.dumps(app_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        try:
            content = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"index (9).html not found")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if args and len(args) > 1 and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Jarvis — server + Telegram notifier")
    p.add_argument(
        "--port", type=int,
        default=int(os.environ.get("PORT", 8000)),
        help="HTTP port (default: $PORT or 8000)",
    )
    args = p.parse_args()

    os.chdir(DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(target=notifier_loop, daemon=True).start()

    server = HTTPServer(("0.0.0.0", args.port), JarvisHandler)
    print(f"Jarvis is running → http://localhost:{args.port}")
    print(f"Data dir:    {DATA_DIR}")
    print(f"Config:      {CONFIG_FILE}")
    print(f"Subscribers: {SUBSCRIBERS_FILE}")
    if os.environ.get("TELEGRAM_TOKEN"):
        print("Telegram:    token loaded from TELEGRAM_TOKEN env var")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
