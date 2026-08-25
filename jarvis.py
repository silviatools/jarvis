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

import hashlib
import html
import json
import os
import re
import sys
import time
import threading
import io
import uuid
import zipfile
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, quote
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

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

# Clean-URL deep links (e.g. /mybody) → serve the SPA, which reads the path
# client-side and jumps straight to the matching tab. Keep in sync with
# PATH_TAB_MAP in index (9).html.
SPA_ROUTES = {
    "/mybody", "/budget", "/supplements", "/meals", "/weather",
    "/house", "/cars", "/holidays", "/settings", "/planner", "/health",
}

# ── Парольный доступ на весь сайт ───────────────────────────────────────────
# Один общий код на семью. Не гейтим гостевые ссылки (/e/<token>, /trip/<token>
# и их API) — по ним заходят друзья, которые кода не знают и знать не должны.
# Cookie живёт 10 лет — «запомнить устройство», код спрашивается один раз.
AUTH_PASSWORD      = "2004"
AUTH_COOKIE_NAME   = "jarvis_auth"
AUTH_COOKIE_VALUE  = hashlib.sha256(f"jarvis-site-auth-v1:{AUTH_PASSWORD}".encode()).hexdigest()
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 10
AUTH_PUBLIC_FILES  = {
    "/manifest.json", "/favicon.ico", "/icon-192.png", "/icon-512.png",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
}


def route_is_public(route: str) -> bool:
    """Маршруты, доступные без пароля: страница логина, статика PWA-манифеста
    и гостевые ссылки на события/поездки (их получают люди вне семьи)."""
    if route == "/login" or route in AUTH_PUBLIC_FILES:
        return True
    if token_from_route(route, "/e/") or token_from_route(route, "/trip/"):
        return True
    if token_from_route(route, "/api/event/") or token_from_route(route, "/api/camping-trip/"):
        return True
    return False

# ── Планировщик дел: гостевые ссылки на событие ────────────────────────────
# Друг открывает /e/<token> — ОТДЕЛЬНУЮ страницу event.html, которая ходит
# только в /api/event/<token>. Ни SPA, ни /api/data ей не нужны: по ссылке
# видно одно событие (и только имена участников), а больше ничего с сайта.
EVENT_PAGE_FILE   = DIR / "event.html"
# ── Кемпинг: гостевая ссылка на чек-лист поездки ────────────────────────────
# Друг открывает /trip/<token> — тоже отдельную статичную страницу, читающую
# только /api/camping-trip/<token>: список вещей и сумок одной поездки,
# без доступа к остальному справочнику или другим поездкам.
TRIP_PAGE_FILE    = DIR / "camping-trip.html"
PLANNER_TOKEN_RE  = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PLANNER_STATUSES  = {"yes", "probably", "maybe", "probably_not", "no"}
MAX_EVENT_COMMENT_LEN   = 2000
MAX_EVENT_NOTE_LEN      = 300
MAX_EVENT_COMMENTS      = 500   # на одно событие — защита от залива мусором
MAX_EVENT_GUEST_BODY    = 64 * 1024
MAX_EVENT_GUEST_NAME    = 60
MAX_EVENT_GUESTS        = 300   # сколько человек могут вписать себя сами
MAX_EVENT_EXPENSES      = 500   # трат на одно событие
MAX_EVENT_EXPENSE_TEXT  = 200   # «за что» и реквизиты
MAX_EVENT_MONEY_CENTS   = 100_000_000_000  # 1 млрд ₽ — потолок вменяемой суммы
EXPENSE_SPLIT_MODES     = {"equal", "custom"}
MAX_EVENT_SHOPPING_ITEMS = 300   # позиций в списке покупок одного события
MAX_EVENT_SHOPPING_NAME  = 120   # «Молоко 2.5%»
MAX_EVENT_SHOPPING_QTY   = 40    # «2 л», «1 пачка» — свободный текст, не число

# Generic file uploads (e.g. training programs attached to «Режим»)
ALLOWED_FILE_EXT = {
    "pdf", "doc", "docx", "xls", "xlsx", "txt", "rtf", "csv",
    "png", "jpg", "jpeg", "webp", "heic", "gif",
}
FILE_CONTENT_TYPES = {
    "pdf": "application/pdf", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain; charset=utf-8", "rtf": "application/rtf", "csv": "text/csv",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "heic": "image/heic", "gif": "image/gif",
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


def subscriber_name(subs: dict, chat_id) -> str:
    profile = (subs.get("profiles") or {}).get(str(chat_id)) or {}
    name = " ".join(x for x in (profile.get("firstName"), profile.get("lastName")) if x).strip()
    if name:
        return name
    if profile.get("username"):
        return f"@{profile['username']}"
    return ""


# Which Telegram section each background reminder belongs to. Keep the ids in
# sync with NOTIFICATION_CATEGORIES in index (9).html — a subscriber with no
# entry in data.settings.notifyRouting receives every category (default-on,
# so nobody currently relying on notifications silently loses them).
NOTIFICATION_CATEGORIES = {"chores", "boss", "holidays", "debts", "diet", "checklist", "tasks", "backup"}


def recipients_for(app_data: dict, subs: dict, category: str) -> list:
    routing = (app_data.get("settings") or {}).get("notifyRouting") or {}
    result = []
    for cid in subs.get("chat_ids", []):
        allowed = routing.get(str(cid))
        if allowed is None or category in allowed:
            result.append(cid)
    return result


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


# ── diet compliance (Соблюдение) ─────────────────────────────────────────────

DIET_LABELS = {
    "much_below": "Ниже", "below": "Чуть ниже", "on_plan": "По плану",
    "above": "Чуть выше", "much_above": "Выше",
    "mini_cheat": "Мини чит мил", "cheat": "Чит мил",
}

MONTHS_RU_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]

def human_date(date_iso: str) -> str:
    """'2026-07-06' → '6 июля'."""
    try:
        y, m, d = date_iso.split("-")
        return f"{int(d)} {MONTHS_RU_GEN[int(m)]}"
    except Exception:
        return date_iso

def diet_keyboard(date_iso: str) -> dict:
    def btn(level):
        return {"text": DIET_LABELS[level], "callback_data": f"diet:{level}:{date_iso}"}
    return {"inline_keyboard": [
        [btn("much_below"), btn("below")],
        [btn("on_plan")],
        [btn("above"), btn("much_above")],
        [btn("mini_cheat"), btn("cheat")],
    ]}


def save_diet_entry(date_iso: str, level: str):
    """Записать/обновить оценку питания за день прямо в файл БД."""
    import uuid as _uuid
    with APP_DATA_LOCK:
        app = load_app_data()
        log = [e for e in app.get("dietLog", []) if e.get("date") != date_iso]
        log.append({"id": str(_uuid.uuid4()), "date": date_iso, "level": level,
                    "updatedAt": int(time.time() * 1000)})
        log.sort(key=lambda e: e.get("date", ""), reverse=True)
        app["dietLog"] = log
        save_app_data(app)


def handle_diet_callback(token: str, cq: dict):
    cq_id = cq.get("id")
    data_str = cq.get("data", "") or ""
    if not data_str.startswith("diet:"):
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    parts = data_str.split(":")
    level = parts[1] if len(parts) > 1 else ""
    date_iso = parts[2] if len(parts) > 2 else today_msk().isoformat()
    if level not in DIET_LABELS:
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    save_diet_entry(date_iso, level)
    label = DIET_LABELS[level]
    tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id, "text": f"✅ Записано: {label}"})
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if chat_id and mid:
        tg_post(token, "editMessageText", {
            "chat_id": chat_id, "message_id": mid,
            "text": f"🍽 <b>Питание за {human_date(date_iso)}</b>\n\n✅ Записано: <b>{label}</b>",
            "parse_mode": "HTML",
        })
    print(f"  diet callback: {date_iso} → {level}")


# ── app data store ─────────────────────────────────────────────────────────────
# Three threads mutate APP_DATA_FILE (HTTP handler, Telegram updates_loop,
# notifier_loop). Every read-modify-write MUST hold APP_DATA_LOCK, and writes
# are atomic (tmp file + os.replace) so a concurrent reader can never observe
# a truncated/partial JSON file.

APP_DATA_LOCK = threading.RLock()


def load_app_data() -> dict:
    with APP_DATA_LOCK:
        if APP_DATA_FILE.exists():
            try:
                return json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


def save_app_data(app: dict):
    with APP_DATA_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = APP_DATA_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, APP_DATA_FILE)


# ── backup (full project + data, sent to Telegram) ──────────────────────────

MAX_TG_FILE = 45 * 1024 * 1024  # stay safely under Telegram's ~50 MB bot upload limit

RESTORE_README = """\
Jarvis — резервная копия проекта и данных
==========================================

Что внутри:
  code/   — все файлы сайта (index (9).html, jarvis.py, notify.py,
            requirements.txt, иконки и т.д.) — то же самое, что лежит
            в GitHub-репозитории. Достаточно для полного передеплоя
            без доступа к GitHub.
  data/   — данные приложения:
              jarvis_app_data.json       — вся база (планы, БАДДы,
                                            бюджет, режимы и т.д.)
              jarvis_subscribers.json    — подписчики ТГ-бота
              jarvis_notify_config.json  — старый конфиг уведомлений
              photos/                    — загруженные фото («Моё тело»)
              files/                     — загруженные файлы («Режим»)

Как восстановить с нуля (если Railway и GitHub недоступны):
  1. Создайте новый репозиторий на GitHub, скопируйте туда всё
     содержимое папки code/ (как есть, с сохранением имён файлов).
  2. Разверните его на Railway (или любом хостинге с Python 3):
       pip install -r requirements.txt
       python3 jarvis.py
  3. Если используете Railway Volume — смонтируйте его и укажите путь
     через переменную окружения DATA_DIR. Скопируйте на этот volume
     всё содержимое папки data/ (файлы jarvis_app_data.json,
     jarvis_subscribers.json, jarvis_notify_config.json и
     папки photos/, files/ — как есть).
  4. Если DATA_DIR не используется — просто положите содержимое data/
     рядом с кодом (в ту же папку, где jarvis.py).
  5. В Settings → Общее укажите токен Telegram-бота (или переменная
     окружения TELEGRAM_TOKEN) — бот подхватит подписчиков из
     jarvis_subscribers.json автоматически.

Если бэкап пришёл несколькими файлами (part001, part002, ...) —
склейте их по порядку перед распаковкой:
  Linux/macOS:  cat jarvis_backup_*.zip.part* > jarvis_backup.zip
  Windows (cmd): copy /b part001+part002+part003 jarvis_backup.zip
"""


def build_backup_zip() -> bytes:
    """Zips the whole project (code) + all app data (data) into one archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen_arcnames = set()

        def add_file(path: Path, arcname: str):
            if arcname in seen_arcnames:
                return  # DATA_DIR can coincide with DIR (no volume) — avoid double-zipping
            seen_arcnames.add(arcname)
            zf.write(path, arcname=arcname)

        data_names = {"jarvis_app_data.json", "jarvis_subscribers.json", "jarvis_notify_config.json"}

        # 1. Project code — every file directly in the script's directory
        #    (skips subdirectories, e.g. .git, so no VCS history is dragged in;
        #    skips data JSONs — when DATA_DIR == DIR they belong under data/ only)
        for p in sorted(DIR.iterdir()):
            if p.is_file() and p.name not in data_names and p.suffix != ".tmp":
                add_file(p, f"code/{p.name}")

        # 2. Core data files
        for fname in data_names:
            fp = DATA_DIR / fname
            if fp.exists():
                add_file(fp, f"data/{fname}")

        # 3. User-uploaded content (body photos, mode training-program files)
        for sub in ("photos", "files"):
            d = DATA_DIR / sub
            if d.exists():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        add_file(f, f"data/{sub}/{f.relative_to(d)}")

        zf.writestr("README.txt", RESTORE_README)
    return buf.getvalue()


def send_document(token: str, chat_id: int, filename: str, data: bytes, caption: str = "") -> bool:
    if not requests:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, data)},
            timeout=120,
        )
        return r.ok
    except Exception as e:
        print(f"  [sendDocument] {e}")
        return False


def record_backup_sent():
    with APP_DATA_LOCK:
        app = load_app_data()
        settings = dict(app.get("settings") or {})
        settings["lastBackupSentAt"] = now_msk().isoformat()
        app["settings"] = settings
        save_app_data(app)


def send_backup_to(token: str, chat_ids: list) -> dict:
    """Builds the backup once and sends it (chunked if needed) to every chat_id.
    Returns a small status dict for the manual 'send now' API response."""
    if not chat_ids:
        return {"ok": False, "error": "no recipients"}
    zip_bytes = build_backup_zip()
    stamp = now_msk().strftime("%Y%m%d_%H%M")
    base_name = f"jarvis_backup_{stamp}.zip"

    chunks = [zip_bytes[i:i + MAX_TG_FILE] for i in range(0, len(zip_bytes), MAX_TG_FILE)] or [b""]
    ok_count = 0
    for cid in chat_ids:
        all_sent = True
        for i, chunk in enumerate(chunks):
            if len(chunks) == 1:
                fname, caption = base_name, f"📦 Бэкап Jarvis · {stamp}"
            else:
                fname = f"{base_name}.part{i + 1:03d}"
                caption = f"📦 Бэкап Jarvis · {stamp} · часть {i + 1}/{len(chunks)}"
            if not send_document(token, cid, fname, chunk, caption):
                all_sent = False
        if all_sent:
            ok_count += 1

    record_backup_sent()
    return {
        "ok": ok_count > 0,
        "recipients": len(chat_ids),
        "sentTo": ok_count,
        "sizeBytes": len(zip_bytes),
        "parts": len(chunks),
    }


# Backup uploads can take minutes (45 MB × recipients, 120 s timeouts). They
# must never run on the notifier thread (blocking it skips every reminder due
# in those minutes) nor on the single HTTP thread (freezing the whole site) —
# always fire them on a dedicated worker thread. The flag prevents a second
# backup from piling on while one is still uploading.
_backup_in_progress = threading.Event()


def start_backup_async(token: str, chat_ids: list) -> bool:
    """Kick off a backup send in the background. False if one is already running."""
    if _backup_in_progress.is_set():
        return False

    def _run():
        try:
            result = send_backup_to(token, chat_ids)
            print(f"  backup finished: {result}")
        except Exception as e:
            print(f"  backup failed: {e}")
        finally:
            _backup_in_progress.clear()

    _backup_in_progress.set()
    threading.Thread(target=_run, daemon=True).start()
    return True


DATE_LOG_KEYS = frozenset({"dietLog", "dailyChecklistLog"})


def _option_label(opt) -> str:
    if isinstance(opt, dict):
        return str(opt.get("label") or opt.get("text") or "").strip()
    return str(opt).strip()


def _is_plain_object(v) -> bool:
    return isinstance(v, dict)


def _is_id_array(a) -> bool:
    return isinstance(a, list) and len(a) > 0 and all(isinstance(e, dict) and "id" in e for e in a)


def _prefer_local_for_key(key: str, mode: str) -> bool:
    if mode == "push":
        return True
    return key not in DATE_LOG_KEYS


def _looks_like_id_array(a) -> bool:
    return any(isinstance(e, dict) and e.get("id") is not None for e in (a if isinstance(a, list) else []))


TOMBSTONE_TTL_MS = 90 * 24 * 60 * 60 * 1000  # prune tombstones after 90 days


def _merge_deleted_ids_maps(local_all: dict | None, server_all: dict | None) -> dict:
    """Merge per-collection {id: deletedAtMs} tombstone maps, keeping the
    newest timestamp for any id present on both sides. Tombstones older than
    TOMBSTONE_TTL_MS are dropped so the map can't grow forever."""
    cutoff = int(time.time() * 1000) - TOMBSTONE_TTL_MS
    merged: dict = {}
    for ck in set((local_all or {}).keys()) | set((server_all or {}).keys()):
        l = (local_all or {}).get(ck) or {}
        s = (server_all or {}).get(ck) or {}
        out = {}
        for tid in set(l.keys()) | set(s.keys()):
            ts = max(l.get(tid, 0) or 0, s.get(tid, 0) or 0)
            if ts >= cutoff:
                out[tid] = ts
        merged[ck] = out
    return merged


def _merge_id_arrays(local_arr, server_arr, prefer_local: bool, deleted_for_key: dict | None = None) -> list:
    la = local_arr if isinstance(local_arr, list) else []
    sa = server_arr if isinstance(server_arr, list) else []

    if not _looks_like_id_array(la) and not _looks_like_id_array(sa):
        # Plain-value array (category name strings, id-order lists, etc.) —
        # nothing has an `.id` to merge by. Union unique values instead of
        # collapsing to [] (which is what the old id-based logic always did
        # for these), while still respecting explicit deletions by value.
        seen: set = set()
        out = []
        for v in [*la, *sa]:
            k = json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
            if k not in seen and not (deleted_for_key and str(k) in deleted_for_key):
                seen.add(k)
                out.append(v)
        return out

    # Last-write-wins by `updatedAt` when both sides have a real timestamp
    # for the same id — makes edits made on ANY device/client show up
    # everywhere, instead of always losing to whichever copy the server
    # happens to already have. Falls back to the old bias (prefer_local)
    # when timestamps are missing, for legacy data.
    by_id: dict = {}

    def _consider(e, is_preferred_pass):
        if not isinstance(e, dict) or e.get("id") is None:
            return
        key = str(e["id"])
        cur = by_id.get(key)
        if cur is None:
            by_id[key] = e
            return
        cur_ts = cur.get("updatedAt") if isinstance(cur.get("updatedAt"), (int, float)) else None
        e_ts = e.get("updatedAt") if isinstance(e.get("updatedAt"), (int, float)) else None
        if cur_ts is None and e_ts is None:
            if is_preferred_pass:
                by_id[key] = e
            return
        if e_ts is not None and (cur_ts is None or e_ts > cur_ts):
            by_id[key] = e

    first = sa if prefer_local else la
    second = la if prefer_local else sa
    for e in first:
        _consider(e, False)
    for e in second:
        _consider(e, True)
    if deleted_for_key:
        # A tombstone only removes copies NOT NEWER than the deletion moment:
        # an item re-created after its deletion (fresher updatedAt) survives —
        # otherwise a once-deleted record could never be entered again.
        for tid, del_ts in deleted_for_key.items():
            item = by_id.get(str(tid))
            if item is not None:
                ts = item.get("updatedAt")
                if not (isinstance(ts, (int, float)) and ts > (del_ts or 0)):
                    by_id.pop(str(tid), None)

    # Mirror of the JS ordering rule: user-arranged order (checklist fields,
    # body fields) follows the side edited most recently; ties → `la`.
    # Without this, a reorder pushed by the client was re-emitted in the old
    # file order and reverted on the next pull.
    def _max_ts(arr):
        best = 0
        for e in arr:
            ts = e.get("updatedAt") if isinstance(e, dict) else None
            if isinstance(ts, (int, float)) and ts > best:
                best = ts
        return best

    order_sides = (la, sa) if _max_ts(la) >= _max_ts(sa) else (sa, la)
    out = []
    emitted = set()
    for side in order_sides:
        for e in side:
            if not isinstance(e, dict) or e.get("id") is None:
                continue
            key = str(e["id"])
            if key in by_id and key not in emitted:
                emitted.add(key)
                out.append(by_id[key])
    return out


def _merge_date_log_entries(local_arr, server_arr, prefer_local: bool, deleted_for_key: dict | None = None) -> list:
    def _norm(e):
        if not isinstance(e, dict) or not e.get("date"):
            return None
        out = dict(e)
        if "answers" in e or any(isinstance(v, dict) for v in [e.get("answers")]):
            out["answers"] = {**(e.get("answers") or {})}
        return out

    def _ts(e):
        v = e.get("updatedAt")
        return v if isinstance(v, (int, float)) else None

    def _combine(base, over):
        """Merge two same-date entries with `over` taking precedence — unless
        `base` carries a strictly newer updatedAt, in which case the newer
        edit wins wholesale (fixes the pull reverting a just-made edit)."""
        b_ts, o_ts = _ts(base), _ts(over)
        if b_ts is not None and (o_ts is None or b_ts > o_ts):
            base, over = over, base
        merged = {**base, **over}
        if "answers" in base or "answers" in over:
            merged["answers"] = {**(base.get("answers") or {}), **(over.get("answers") or {})}
        if "level" in over:
            merged["level"] = over["level"]
        elif "level" in base:
            merged["level"] = base["level"]
        merged["id"] = over.get("id") or base.get("id")
        return merged

    local = [x for x in ((_norm(e) for e in (local_arr or []))) if x]
    server = [x for x in ((_norm(e) for e in (server_arr or []))) if x]

    # UNION by date in both modes: an entry present on only one side is kept
    # (e.g. a bot answer written seconds ago that the pushing client hasn't
    # pulled yet). Deletions are enforced exclusively by date-tombstones, so
    # "missing from the preferred list" no longer implies "deleted". On a
    # same-date conflict `_combine` gives the strictly newer updatedAt the
    # win; with no timestamps the preferred side's entry wins.
    first = server if prefer_local else local
    second = local if prefer_local else server
    by_date: dict[str, dict] = {}
    for e in first:
        by_date[e["date"]] = dict(e)
    for e in second:
        prev = by_date.get(e["date"])
        by_date[e["date"]] = _combine(prev, e) if prev else dict(e)
    result = list(by_date.values())

    if deleted_for_key:
        # Same rule as id-arrays: the tombstone only drops entries not newer
        # than the deletion — a manual re-entry for a once-deleted date sticks.
        def _survives(e):
            del_ts = deleted_for_key.get(str(e.get("date")))
            if del_ts is None:
                return True
            ts = e.get("updatedAt")
            return isinstance(ts, (int, float)) and ts > (del_ts or 0)
        result = [e for e in result if _survives(e)]
    return sorted(result, key=lambda e: e.get("date", ""), reverse=True)


def merge_app_data(local: dict, server: dict, mode: str = "pull") -> dict:
    """Merge app-data dicts. mode='push' → incoming (local) wins; mode='pull' → local wins except bot logs."""
    if not server:
        return local or {}
    if not local:
        return server or {}
    merged = {**local, **server}
    merged_deleted_ids = _merge_deleted_ids_maps(local.get("deletedIds"), server.get("deletedIds"))
    keys = set(local.keys()) | set(server.keys())
    for key in keys:
        if key == "deletedIds":
            merged[key] = merged_deleted_ids
            continue
        l = local.get(key)
        s = server.get(key)
        if s is None:
            merged[key] = l
        elif l is None:
            merged[key] = s
        else:
            prefer_local = _prefer_local_for_key(key, mode)
            if key == "kanban" and _is_plain_object(l) and _is_plain_object(s):
                # Mirror of the JS rule: the board object merges per-key, but its
                # columns merge AS ID-RECORDS (newest updatedAt wins per column) —
                # otherwise one device's board wholesale-clobbered the other's.
                base = {**s, **l} if prefer_local else {**l, **s}
                base["columns"] = _merge_id_arrays(
                    l.get("columns") or [], s.get("columns") or [],
                    prefer_local, merged_deleted_ids.get("kanbanColumns"))
                merged[key] = base
            elif key in DATE_LOG_KEYS and (isinstance(l, list) or isinstance(s, list)):
                merged[key] = _merge_date_log_entries(l, s, prefer_local, merged_deleted_ids.get(key))
            elif isinstance(l, list) or isinstance(s, list):
                merged[key] = _merge_id_arrays(l, s, prefer_local, merged_deleted_ids.get(key))
            elif _is_plain_object(l) and _is_plain_object(s):
                # Mirror of the JS rule: a plain object carrying an updatedAt
                # stamp (dietReminder, dailyChecklistReminder, settings…) merges
                # newest-wins WHOLESALE — otherwise a device holding a stale
                # copy forever clobbered a freshly changed reminder time.
                l_ts = l.get("updatedAt") if isinstance(l.get("updatedAt"), (int, float)) else None
                s_ts = s.get("updatedAt") if isinstance(s.get("updatedAt"), (int, float)) else None
                if l_ts is not None or s_ts is not None:
                    merged[key] = s if (s_ts is not None and (l_ts is None or s_ts > l_ts)) else l
                else:
                    merged[key] = {**s, **l} if prefer_local else {**l, **s}
            else:
                merged[key] = l if prefer_local else s
    return merged


def get_checklist_entry(app: dict, date_iso: str) -> dict | None:
    for e in app.get("dailyChecklistLog", []):
        if e.get("date") == date_iso:
            return e
    return None


def active_checklist_fields(app: dict) -> list:
    """Checklist fields, excluding archived ones (bot never asks about those)."""
    return [f for f in (app.get("dailyChecklistFields") or []) if not f.get("archived")]


def save_checklist_answer(date_iso: str, field_idx: int, opt_idx: int) -> tuple[str, str] | None:
    """Save one checklist answer. Returns (field_label, option_text) or None."""
    import uuid as _uuid
    with APP_DATA_LOCK:
        app = load_app_data()
        fields = active_checklist_fields(app)
        if field_idx < 0 or field_idx >= len(fields):
            return None
        field = fields[field_idx]
        options = field.get("options") or []
        if opt_idx < 0 or opt_idx >= len(options):
            return None
        option_text = _option_label(options[opt_idx])
        field_id = field.get("id", str(field_idx))
        now_ms = int(time.time() * 1000)
        entry = get_checklist_entry(app, date_iso)
        if entry:
            answers = {**(entry.get("answers") or {}), field_id: option_text}
            entry = {**entry, "answers": answers, "updatedAt": now_ms}
            log = [e for e in app.get("dailyChecklistLog", []) if e.get("date") != date_iso]
        else:
            entry = {"id": str(_uuid.uuid4()), "date": date_iso,
                     "answers": {field_id: option_text}, "updatedAt": now_ms}
            log = list(app.get("dailyChecklistLog", []))
        log.append(entry)
        log.sort(key=lambda e: e.get("date", ""), reverse=True)
        app["dailyChecklistLog"] = log
        save_app_data(app)
        return field.get("label", ""), option_text


def next_unanswered_field_idx(app: dict, date_iso: str) -> int | None:
    fields = active_checklist_fields(app)
    entry = get_checklist_entry(app, date_iso)
    answered = set((entry or {}).get("answers", {}).keys())
    for i, f in enumerate(fields):
        if f.get("id") not in answered:
            return i
    return None


def checklist_keyboard(date_iso: str, field_idx: int, field: dict) -> dict:
    row = []
    for oi, opt in enumerate(field.get("options") or []):
        label = _option_label(opt) or "?"
        row.append({"text": label, "callback_data": f"chk:{date_iso}:{field_idx}:{oi}"})
    return {"inline_keyboard": [row]}


def send_checklist_question(token: str, chat_id: int, date_iso: str, field_idx: int | None = None):
    app = load_app_data()
    fields = active_checklist_fields(app)
    if not fields:
        return
    if field_idx is None:
        field_idx = next_unanswered_field_idx(app, date_iso)
    if field_idx is None:
        return
    field = fields[field_idx]
    kb = checklist_keyboard(date_iso, field_idx, field)
    tg_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"📋 <b>{field.get('label', 'Чек-лист')}</b>\n📅 {human_date(date_iso)}",
        "parse_mode": "HTML",
        "reply_markup": kb,
    })


def handle_checklist_callback(token: str, cq: dict):
    cq_id = cq.get("id")
    data_str = cq.get("data", "") or ""
    if not data_str.startswith("chk:"):
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    parts = data_str.split(":")
    if len(parts) < 4:
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    date_iso = parts[1]
    try:
        field_idx = int(parts[2])
        opt_idx = int(parts[3])
    except ValueError:
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    result = save_checklist_answer(date_iso, field_idx, opt_idx)
    if not result:
        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id})
        return
    field_label, option_text = result
    tg_post(token, "answerCallbackQuery", {"callback_query_id": cq_id, "text": f"✅ {field_label}: {option_text}"})
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if chat_id and mid:
        tg_post(token, "editMessageText", {
            "chat_id": chat_id, "message_id": mid,
            "text": f"📋 <b>{field_label}</b>\n📅 {human_date(date_iso)}\n\n✅ <b>{option_text}</b>",
            "parse_mode": "HTML",
        })
    print(f"  checklist callback: {date_iso} → {field_label}: {option_text}")
    app = load_app_data()
    next_idx = next_unanswered_field_idx(app, date_iso)
    if next_idx is not None and chat_id:
        send_checklist_question(token, chat_id, date_iso, next_idx)
    elif chat_id:
        send_message(token, chat_id, f"✅ <b>Чек-лист за {human_date(date_iso)} заполнен!</b>")


# ── update-poller loop ───────────────────────────────────────────────────────
# Long-polls Telegram continuously so inline-button presses (diet answers) and new
# subscribers are handled within ~1s, independent of the minute-aligned notifier.

def updates_loop():
    print("Updates poller thread started.")
    while True:
        token = get_token()
        if not token or not requests:
            time.sleep(5)
            continue
        subs = load_subscribers()
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": subs["offset"], "timeout": 25},
                timeout=30,
            )
            updates = r.json().get("result", []) if r.ok else []
        except Exception as e:
            print(f"  getUpdates(long): {e}")
            time.sleep(3)
            continue

        changed = False
        for upd in updates:
            subs["offset"] = upd["update_id"] + 1
            changed = True
            cq = upd.get("callback_query")
            if cq:
                data_str = cq.get("data", "") or ""
                try:
                    if data_str.startswith("diet:"):
                        handle_diet_callback(token, cq)
                    elif data_str.startswith("chk:"):
                        handle_checklist_callback(token, cq)
                    else:
                        tg_post(token, "answerCallbackQuery", {"callback_query_id": cq.get("id")})
                except Exception as e:
                    print(f"  callback error: {e}")
                continue
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            cid = msg["chat"]["id"]
            from_user = msg.get("from") or {}
            profiles = subs.setdefault("profiles", {})
            profiles[str(cid)] = {
                "firstName": from_user.get("first_name", ""),
                "lastName": from_user.get("last_name", ""),
                "username": from_user.get("username", ""),
            }
            if cid not in subs["chat_ids"]:
                subs["chat_ids"].append(cid)
                print(f"  New subscriber: {cid}")
                send_message(token, cid, WELCOME_TEXT)
        if changed:
            save_subscribers(subs)


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


# Guards chores/boss/backup against double-fire when the minute loop happens
# to run twice inside one clock-minute (sleep jitter near a second boundary).
# Keys look like "chore:<name>:<date> <HH:MM>"; pruned when the date changes.
_fired_reminders: set = set()
_fired_reminders_day = [""]


def _already_fired(kind: str, ident, today_iso: str, now_str: str) -> bool:
    if _fired_reminders_day[0] != today_iso:
        _fired_reminders.clear()
        _fired_reminders_day[0] = today_iso
    key = f"{kind}:{ident}:{today_iso} {now_str}"
    if key in _fired_reminders:
        return True
    _fired_reminders.add(key)
    return False


def _tick():
    token = get_token()
    if not token:
        return

    subs = load_subscribers()
    now_str = now_msk().strftime("%H:%M")
    today_js = today_msk().isoweekday() % 7  # 0=Sun..6=Sat, matches JS getDay()
    today_iso = today_msk().isoformat()
    today_date = today_msk()

    # Single read of app data — reused by every block below. Each block runs
    # in its own try/except so a bug or malformed entry in one reminder type
    # (e.g. a bad chore/holiday date) can NEVER prevent the other reminder
    # types (in particular the daily checklist) from firing on this tick.
    app_data_raw = {}
    if APP_DATA_FILE.exists():
        try:
            app_data_raw = json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[{now_str} MSK] app-data read error: {e}")

    # ── Chores ───────────────────────────────────────────────────────────
    try:
        chores = [c for c in app_data_raw.get("chores", []) if not c.get("archived")]
        if not chores and CONFIG_FILE.exists():
            try:
                config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                chores = config.get("chores", [])
            except Exception:
                chores = []
        for chore in chores:
            if not chore.get("notify"):
                continue
            if chore.get("notifyTime", "") != now_str:
                continue
            if not is_due_today(chore):
                continue
            if _already_fired("chore", chore.get("id") or chore.get("name"), today_iso, now_str):
                continue
            text = f"🏠 <b>По дому — напоминание</b>\n\n{chore.get('name', 'Дело')}"
            recipients = recipients_for(app_data_raw, subs, "chores")
            print(f"[{now_str} MSK] → {chore['name']} ({len(recipients)} subscriber(s))")
            for cid in recipients:
                send_message(token, cid, text)
    except Exception as e:
        print(f"[{now_str} MSK] chores reminder error: {e}")

    # ── Boss tasks: days stored as JS getDay() (0=Sun,1=Mon..6=Sat) ───────
    try:
        boss_tasks = [t for t in app_data_raw.get("bossTasks", []) if not t.get("archived")]
        for task in boss_tasks:
            if not task.get("notify"):
                continue
            if task.get("notifyTime", "") != now_str:
                continue
            if today_js not in (task.get("days") or []):
                continue
            if _already_fired("boss", task.get("id") or task.get("name"), today_iso, now_str):
                continue
            text = f"💼 <b>Босс — напоминание</b>\n\n{task.get('name', 'Задача')}"
            recipients = recipients_for(app_data_raw, subs, "boss")
            print(f"[{now_str} MSK] → boss: {task['name']} ({len(recipients)} subscriber(s))")
            for cid in recipients:
                send_message(token, cid, text)
    except Exception as e:
        print(f"[{now_str} MSK] boss tasks reminder error: {e}")

    # ── Holidays: date stored as "MM-DD", reminders have daysBefore + time ─
    try:
        holidays = [h for h in app_data_raw.get("holidays", []) if not h.get("archived")]
        TYPE_EMOJI = {"birthday": "🎂", "anniversary": "💑", "other": "🎉"}
        for holiday in holidays:
            if not holiday.get("notify"):
                continue
            date_str = holiday.get("date", "")
            if not date_str or date_str.count("-") != 1:
                continue
            try:
                mm, dd = int(date_str.split("-")[0]), int(date_str.split("-")[1])
            except (ValueError, IndexError):
                continue
            for reminder in holiday.get("reminders") or []:
                if reminder.get("time", "") != now_str:
                    continue
                try:
                    days_before = int(reminder.get("daysBefore", 0) or 0)
                except (ValueError, TypeError):
                    continue
                # Check current year and next year to handle cross-year notifications
                for year_offset in (0, 1):
                    try:
                        holiday_date = date(today_date.year + year_offset, mm, dd)
                        notify_date = holiday_date - timedelta(days=days_before)
                        if notify_date != today_date:
                            continue
                        emoji = TYPE_EMOJI.get(holiday.get("type", "other"), "🎉")
                        if days_before == 0:
                            when = "сегодня!"
                        elif days_before == 1:
                            when = "завтра"
                        elif 2 <= days_before <= 4:
                            when = f"через {days_before} дня"
                        else:
                            when = f"через {days_before} дней"
                        text = f"{emoji} <b>Праздник — напоминание</b>\n\n{holiday.get('name', 'Событие')}\n<i>{when}</i>"
                        recipients = recipients_for(app_data_raw, subs, "holidays")
                        print(f"[{now_str} MSK] → holiday: {holiday['name']} in {days_before}d ({len(recipients)} subscriber(s))")
                        for cid in recipients:
                            send_message(token, cid, text)
                    except (ValueError, OverflowError):
                        pass
    except Exception as e:
        print(f"[{now_str} MSK] holidays reminder error: {e}")

    # ── Debts: one-off reminder at notifyDate + notifyTime (MSK) ──────────
    try:
        debts = list(app_data_raw.get("budgetDebts") or [])
        # Per-user budget slices (budgetByUser) — collect debts from every user.
        by_user = app_data_raw.get("budgetByUser") or {}
        if isinstance(by_user, dict):
            for slice_data in by_user.values():
                if isinstance(slice_data, dict):
                    debts.extend(slice_data.get("budgetDebts") or [])
        seen_debt_ids = set()
        for debt in debts:
            debt_id = debt.get("id")
            if debt_id:
                if debt_id in seen_debt_ids:
                    continue
                seen_debt_ids.add(debt_id)
            if not debt.get("notify") or debt.get("closed"):
                continue
            if debt.get("notifyDate", "") != today_iso:
                continue
            if debt.get("notifyTime", "") != now_str:
                continue
            amount = debt.get("amount", 0)
            debtor = debt.get("debtor", "")
            comment = debt.get("comment", "")
            text = f"💰 <b>Долг — напоминание</b>\n\n{debtor} должен вернуть {amount} ₽"
            if comment:
                text += f"\n<i>{comment}</i>"
            recipients = recipients_for(app_data_raw, subs, "debts")
            print(f"[{now_str} MSK] → debt: {debtor} {amount} ({len(recipients)} subscriber(s))")
            for cid in recipients:
                send_message(token, cid, text)
    except Exception as e:
        print(f"[{now_str} MSK] debts reminder error: {e}")

    # ── Kanban tasks: one-off reminder at notifyDate + notifyTime (MSK) ────
    try:
        kanban_columns = (app_data_raw.get("kanban") or {}).get("columns") or []
        for col in kanban_columns:
            for task in (col.get("tasks") or []):
                if not task.get("notify") or task.get("closed"):
                    continue
                if task.get("notifyDate", "") != today_iso:
                    continue
                if task.get("notifyTime", "") != now_str:
                    continue
                if _already_fired("task", task.get("id") or task.get("title"), today_iso, now_str):
                    continue
                title = task.get("title", "Задача")
                text = f"📋 <b>Задача — напоминание</b>\n\n{title}"
                due = task.get("dueDate", "")
                if due:
                    text += f"\n<i>Дедлайн: {human_date(due)}</i>"
                recipients = recipients_for(app_data_raw, subs, "tasks")
                print(f"[{now_str} MSK] → task: {title} ({len(recipients)} subscriber(s))")
                for cid in recipients:
                    send_message(token, cid, text)
    except Exception as e:
        print(f"[{now_str} MSK] tasks reminder error: {e}")

    # ── Diet compliance: recurring «Как ты кушал сегодня?» ────────────────
    try:
        reminder = app_data_raw.get("dietReminder") or {}
        diet_log = app_data_raw.get("dietLog", [])
        if reminder.get("enabled") and str(reminder.get("time", "")).strip() == now_str:
            days = reminder.get("days", [0, 1, 2, 3, 4, 5, 6]) or []
            already = any(e.get("date") == today_iso for e in diet_log)
            if today_js in days and not already:
                kb = diet_keyboard(today_iso)
                recipients = recipients_for(app_data_raw, subs, "diet")
                print(f"[{now_str} MSK] → diet ask ({len(recipients)} subscriber(s))")
                for cid in recipients:
                    tg_post(token, "sendMessage", {
                        "chat_id": cid,
                        "text": f"🍽 <b>Как ты кушал сегодня?</b>\n📅 {human_date(today_iso)}",
                        "parse_mode": "HTML",
                        "reply_markup": kb,
                    })
    except Exception as e:
        print(f"[{now_str} MSK] diet reminder error: {e}")

    # ── Daily checklist reminder ───────────────────────────────────────────
    try:
        checklist_reminder = app_data_raw.get("dailyChecklistReminder") or {}
        checklist_fields = active_checklist_fields(app_data_raw)
        cfg_time = str(checklist_reminder.get("time", "")).strip()

        # Diagnostic: log a near-miss (configured time within ±2 min of now
        # but not an exact string match) so time-format bugs are visible in
        # the server logs instead of silently never firing.
        def _to_mins(hhmm):
            try:
                h, m = hhmm.split(":")
                return int(h) * 60 + int(m)
            except Exception:
                return None
        if checklist_reminder.get("enabled") and cfg_time and cfg_time != now_str:
            cfg_mins, now_mins = _to_mins(cfg_time), _to_mins(now_str)
            if cfg_mins is not None and now_mins is not None and abs(cfg_mins - now_mins) <= 2:
                print(f"[{now_str} MSK] checklist reminder near-miss: configured time '{cfg_time}' != now '{now_str}'")

        if checklist_reminder.get("enabled") and cfg_time == now_str:
            days = checklist_reminder.get("days", [0, 1, 2, 3, 4, 5, 6]) or []
            recipients = recipients_for(app_data_raw, subs, "checklist")
            if today_js not in days:
                print(f"[{now_str} MSK] checklist reminder: today ({today_js}) not in days {days}")
            elif not checklist_fields:
                print(f"[{now_str} MSK] checklist reminder: no dailyChecklistFields configured")
            elif not recipients:
                print(f"[{now_str} MSK] checklist reminder: no subscribers routed to checklist")
            else:
                idx = next_unanswered_field_idx(app_data_raw, today_iso)
                if idx is None:
                    print(f"[{now_str} MSK] checklist reminder: all fields already answered for {today_iso}")
                else:
                    print(f"[{now_str} MSK] → checklist ask ({len(recipients)} subscriber(s))")
                    for cid in recipients:
                        send_checklist_question(token, cid, today_iso)
    except Exception as e:
        print(f"[{now_str} MSK] checklist reminder error: {e}")

    # ── Scheduled backup: full project + data zipped and sent to Telegram ──
    try:
        backup_reminder = (app_data_raw.get("settings") or {}).get("backupReminder") or {}
        cfg_time = str(backup_reminder.get("time", "")).strip()
        if backup_reminder.get("enabled") and cfg_time == now_str:
            freq = backup_reminder.get("frequency", "weekly")
            should_fire = False
            if freq == "daily":
                should_fire = True
            elif freq == "weekly":
                should_fire = today_js == int(backup_reminder.get("dayOfWeek", 0) or 0)
            elif freq == "monthly":
                should_fire = today_date.day == int(backup_reminder.get("dayOfMonth", 1) or 1)
            if should_fire and _already_fired("backup", "scheduled", today_iso, now_str):
                should_fire = False
            if should_fire:
                recipients = recipients_for(app_data_raw, subs, "backup")
                if not recipients:
                    print(f"[{now_str} MSK] backup: no subscribers routed to backup")
                else:
                    started = start_backup_async(token, recipients)
                    print(f"[{now_str} MSK] → scheduled backup {'started' if started else 'skipped (already running)'} ({len(recipients)} recipient(s))")
    except Exception as e:
        print(f"[{now_str} MSK] backup reminder error: {e}")


# ── Alice (Yandex Dialogs) voice skill ──────────────────────────────────────
# Webhook for a Yandex Dialogs skill: on "доброе утро" (or any invocation of
# the skill) it reads back today's tasks from the «Босс» section, using the
# exact same morning/evening split as the BossWidget on the site
# (index (9).html: getBossPeriod / BossWidget) so the voice answer always
# matches what's shown on the homepage at that moment.
# Point the skill's webhook URL at POST /api/alice — see https://dialogs.yandex.ru/developer

WEEKDAYS_RU_ACCUSATIVE = ["понедельник", "вторник", "среду", "четверг",
                          "пятницу", "субботу", "воскресенье"]


def get_boss_period() -> str | None:
    """Mirrors getBossPeriod() in index (9).html: 5–13 → утро, 13–24 → вечер."""
    h = now_msk().hour
    if 5 <= h < 13:
        return "morning"
    if 13 <= h < 24:
        return "evening"
    return None


def build_boss_brief(app: dict) -> str:
    period = get_boss_period()
    weekday = WEEKDAYS_RU_ACCUSATIVE[today_msk().weekday()]

    if period is None:
        return "Сейчас не время утренних или вечерних дел по разделу «Босс» — загляните после пяти утра."

    period_label = "Утренние" if period == "morning" else "Вечерние"
    today_js = today_msk().isoweekday() % 7  # 0=Sun..6=Sat, matches JS getDay()
    tasks = [t for t in (app.get("bossTasks") or [])
             if not t.get("archived")
             and t.get("period") == period
             and today_js in (t.get("days") or [])]

    if not tasks:
        return f"{period_label} дела по разделу «Босс» на {weekday} не запланированы."

    names = ", ".join(t.get("name") or "задача" for t in tasks)
    return f"{period_label} дела на {weekday}: {names}."


def handle_alice_request(payload: dict) -> dict:
    session = payload.get("session") or {}
    request_data = payload.get("request") or {}
    command = (request_data.get("command") or "").strip().lower()
    is_new_session = bool(session.get("new"))

    trigger_words = ("утр", "вечер", "дела", "задач", "босс")
    if is_new_session or any(w in command for w in trigger_words):
        text = build_boss_brief(load_app_data())
    else:
        text = "Скажите «доброе утро» или «добрый вечер», чтобы услышать свои дела по разделу «Босс»."

    return {
        "version": payload.get("version", "1.0"),
        "session": session,
        "response": {"text": text, "tts": text, "end_session": True},
    }


# ── Планировщик дел ────────────────────────────────────────────────────────

def token_from_route(route: str, prefix: str, suffix: str = "") -> str | None:
    """Извлекает токен события из пути вида <prefix><token><suffix>.
    Возвращает None, если путь не подходит или токен не проходит валидацию —
    так в поиск по данным никогда не попадает произвольная строка из URL."""
    if not route.startswith(prefix):
        return None
    rest = route[len(prefix):]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[:-len(suffix)]
    if not PLANNER_TOKEN_RE.match(rest):
        return None
    return rest


def find_planner_event(app: dict, token: str) -> dict | None:
    """Событие с активной гостевой ссылкой. Выключенная ссылка (shareEnabled
    = false) — то же самое, что несуществующая: отзыв ссылки должен работать."""
    for ev in (app.get("plannerEvents") or []):
        if not isinstance(ev, dict):
            continue
        if str(ev.get("shareToken") or "") != token:
            continue
        return ev if ev.get("shareEnabled", True) else None
    return None


def event_mode(ev: dict) -> str:
    """'open' — гость сам вписывает своё имя (событие не только для друзей),
    'list' — отвечать можно только от имени заранее выбранного участника."""
    return "open" if ev.get("participantMode") == "open" else "list"


def event_guests(app: dict, ev: dict) -> list:
    """Те, кто вписал себя сам на этом событии. В режиме списка — никого."""
    if event_mode(ev) != "open":
        return []
    eid = ev.get("id")
    return [
        g for g in (app.get("plannerGuests") or [])
        if isinstance(g, dict) and g.get("id") and g.get("eventId") == eid
    ]


def planner_public_payload(app: dict, ev: dict) -> dict:
    """Публичный срез ОДНОГО события: поля самого события, имена участников,
    их ответы и комментарии. Ничего больше из базы сюда не попадает — ни
    заметки о друзьях (контакты), ни другие события, ни прочие разделы."""
    eid = ev.get("id")
    friends = {}
    for f in (app.get("plannerFriends") or []):
        if isinstance(f, dict) and f.get("id"):
            friends[f["id"]] = f
    participants = [
        {"id": fid, "name": (friends[fid].get("name") or "Без имени"), "self": False}
        for fid in (ev.get("participantIds") or [])
        if fid in friends
    ]
    # Вписавшие себя идут после приглашённых, в порядке присоединения.
    participants += [
        {"id": g["id"], "name": g.get("name") or "Гость", "self": True}
        for g in sorted(event_guests(app, ev), key=lambda g: g.get("createdAt") or 0)
    ]
    known = {p["id"] for p in participants}
    responses = [
        {
            "friendId": r.get("friendId"),
            "status": r.get("status"),
            "note": r.get("note") or "",
            "updatedAt": r.get("updatedAt") or 0,
        }
        for r in (app.get("plannerResponses") or [])
        if isinstance(r, dict) and r.get("eventId") == eid and r.get("friendId") in known
    ]
    comments = sorted(
        (
            {
                "id": c.get("id"),
                "friendId": c.get("friendId"),
                "text": c.get("text") or "",
                "createdAt": c.get("createdAt") or 0,
            }
            for c in (app.get("plannerComments") or [])
            if isinstance(c, dict) and c.get("eventId") == eid
        ),
        key=lambda c: c["createdAt"],
    )
    expenses = sorted(
        (
            {
                "id": e.get("id"),
                "payerId": e.get("payerId"),
                "amount": e.get("amount"),
                "title": e.get("title") or "",
                "payTo": e.get("payTo") or "",
                "splitMode": "custom" if e.get("splitMode") == "custom" else "equal",
                "participantIds": [x for x in (e.get("participantIds") or []) if x],
                "shares": [
                    {"participantId": sh.get("participantId"), "amount": sh.get("amount")}
                    for sh in (e.get("shares") or []) if isinstance(sh, dict)
                ],
                "eventId": eid,
                "createdAt": e.get("createdAt") or 0,
                "updatedAt": e.get("updatedAt") or 0,
            }
            for e in (app.get("plannerExpenses") or [])
            if isinstance(e, dict) and e.get("eventId") == eid
        ),
        key=lambda e: e["createdAt"],
    )
    payments = [
        {
            "id": p.get("id"),
            "eventId": eid,
            "fromId": p.get("fromId"),
            "toId": p.get("toId"),
            "paid": bool(p.get("paid")),
            "amount": p.get("amount") or 0,
            "paidAt": p.get("paidAt") or 0,
            "paidBy": p.get("paidBy") or "",
        }
        for p in (app.get("plannerPayments") or [])
        if isinstance(p, dict) and p.get("eventId") == eid
    ]
    # «Организатор» — тот же псевдо-участник, что и в paidBy у переводов:
    # владелец приложения отмечает покупки прямо из своей копии, не будучи
    # обычным участником события.
    participant_names = {p["id"]: p["name"] for p in participants}
    shopping_items = sorted(
        (
            {
                "id": it.get("id"),
                "name": it.get("name") or "",
                "qty": it.get("qty") or "",
                "takenBy": it.get("takenBy") or None,
                "takenByName": (
                    "Организатор" if it.get("takenBy") == "organizer"
                    else participant_names.get(it.get("takenBy"))
                ) if it.get("takenBy") else None,
                "takenAt": it.get("takenAt") or 0,
                "createdAt": it.get("createdAt") or 0,
                "updatedAt": it.get("updatedAt") or 0,
            }
            for it in (app.get("plannerShoppingItems") or [])
            if isinstance(it, dict) and it.get("eventId") == eid and it.get("id")
        ),
        key=lambda it: it["createdAt"],
    )
    return {
        "event": {
            "id": eid,
            "title": ev.get("title") or "Событие",
            "description": ev.get("description") or "",
            "place": ev.get("place") or "",
            "address": ev.get("address") or "",
            "startDate": ev.get("startDate") or "",
            "endDate": ev.get("endDate") or "",
            "startTime": ev.get("startTime") or "",
            "endTime": ev.get("endTime") or "",
            "decisionDeadline": ev.get("decisionDeadline") or "",
            "organizer": ev.get("organizer") or "",
            "mode": event_mode(ev),
        },
        "participants": participants,
        "responses": responses,
        "comments": comments,
        "expenses": expenses,
        "payments": payments,
        "shoppingItems": shopping_items,
        "serverNow": int(time.time() * 1000),
    }


def event_participant_ids(app: dict, ev: dict) -> set:
    """Кто вправе отвечать на этом событии: приглашённые организатором плюс
    (для открытого события) все, кто вписал сюда своё имя."""
    ids = {fid for fid in (ev.get("participantIds") or []) if fid}
    ids |= {g["id"] for g in event_guests(app, ev)}
    return ids


def normalize_guest_name(raw) -> str:
    return " ".join(str(raw or "").split())[:MAX_EVENT_GUEST_NAME].strip()


def money_to_cents(value):
    """Сумма в копейках или None, если это не деньги. Через Decimal, а не
    float: 1234.565 * 100 в двоичной дробной арифметике даёт 123456.49999…,
    и трата молча теряла бы копейку. Разбор совпадает с toCents() из
    planner-split.js — обе стороны считают одно и то же."""
    try:
        raw = str(value).replace(" ", "").replace(" ", "").replace(",", ".").strip()
        if not raw:
            return None
        cents = int((Decimal(raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None
    return cents


def planner_guest_write(token: str, friend_id: str, apply):
    """Общая обвязка гостевых записей (ответ / комментарий): найти событие по
    токену, убедиться, что friend_id — участник ИМЕННО этого события, и под
    общим локом применить `apply(app, event)`.
    Возвращает (http_code, payload)."""
    with APP_DATA_LOCK:
        app = load_app_data() if APP_DATA_FILE.exists() else {}
        ev = find_planner_event(app, token)
        if ev is None:
            return 404, {"error": "event not found"}
        if friend_id not in event_participant_ids(app, ev):
            return 403, {"error": "not a participant"}
        err = apply(app, ev)
        if err:
            return err
        save_app_data(app)
        return 200, planner_public_payload(app, ev)


def find_camping_trip(app: dict, token: str) -> dict | None:
    """Поездка с активной гостевой ссылкой. Та же логика отзыва ссылки, что
    и у событий: shareEnabled = false работает как «поездка не найдена»."""
    for t in (app.get("campingTrips") or []):
        if not isinstance(t, dict):
            continue
        if str(t.get("shareToken") or "") != token:
            continue
        return t if t.get("shareEnabled", True) else None
    return None


def camping_trip_public_payload(app: dict, trip: dict) -> dict:
    """Публичный срез ОДНОЙ поездки: сама поездка и её список сборов —
    сумки и вещи с именами и категориями, без доступа к остальному
    справочнику вещей или другим поездкам."""
    items_by_id = {}
    for it in (app.get("campingItems") or []):
        if isinstance(it, dict) and it.get("id"):
            items_by_id[it["id"]] = it
    cats_by_id = {}
    for c in (app.get("campingCategories") or []):
        if isinstance(c, dict) and c.get("id"):
            cats_by_id[c["id"]] = c

    packing = trip.get("packing") or {}
    bags = []
    for b in (packing.get("bags") or []):
        if not isinstance(b, dict) or not b.get("itemId"):
            continue
        it = items_by_id.get(b["itemId"])
        bags.append({
            "itemId": b["itemId"],
            "name": (it or {}).get("name") or "Удалённая сумка",
            "packed": bool(b.get("packed")),
        })
    goods = []
    for ref in (packing.get("items") or []):
        if not isinstance(ref, dict) or not ref.get("itemId"):
            continue
        it = items_by_id.get(ref["itemId"])
        cat = cats_by_id.get((it or {}).get("categoryId"))
        goods.append({
            "itemId": ref["itemId"],
            "name": (it or {}).get("name") or "Удалённая вещь",
            "categoryName": (cat or {}).get("name") or "",
            "bagId": ref.get("bagId") or None,
            "packed": bool(ref.get("packed")),
        })

    return {
        "trip": {
            "id": trip.get("id"),
            "name": trip.get("name") or "Поездка",
            "startDate": trip.get("startDate") or "",
            "endDate": trip.get("endDate") or "",
            "location": trip.get("location") or "",
            "description": trip.get("description") or "",
        },
        "bags": bags,
        "items": goods,
        "serverNow": int(time.time() * 1000),
    }


# ── Личные финансы: PWA быстрого ввода операций (ДДС) ──────────────────────
# Телефон открывает /pf/<token> — отдельную страницу finance.html, которая
# ходит только в /api/pf/<token>/*. Токен привязан к КОНКРЕТНОМУ пользователю
# бюджета (budgetUsers[i].financeToken), поэтому у каждого пользователя своя
# ссылка и своё приложение: чужие счета и статьи по ней не видны.
#
# Всё, что записано через приложение, ложится в срез этого пользователя
# (budgetByUser[uid]) в те же массивы, что читает вкладка «ДДС» на сайте:
#   budgetCashflowAccounts — счета (имя + первоначальный баланс),
#   budgetCashflowOps      — операции расхода/дохода,
#   budgetCashflowSettings — счёт по умолчанию для быстрого ввода.

FINANCE_PAGE_FILE = DIR / "finance.html"

MAX_PF_BODY          = 16 * 1024
MAX_PF_PURPOSE       = 200
MAX_PF_OPS           = 20000          # потолок истории на одного пользователя
MAX_PF_AMOUNT_CENTS  = 100_000_000_000  # 1 млрд ₽ — потолок вменяемой суммы
PF_DIRECTIONS        = {"in", "out"}

# Зеркало BUDGET_RECURRING_CATS из index (9).html: подписи авто-строк
# «Постоянные», пока пользователь не завёл свой список категорий.
# ДЕРЖАТЬ В СИНХРОНЕ с фронтендом.
PF_DEFAULT_RECURRING_CATS = [
    {"id": "subscriptions", "emoji": "📺", "label": "Подписки"},
    {"id": "internet",      "emoji": "🌐", "label": "Интернет"},
    {"id": "mobile",        "emoji": "📱", "label": "Мобильная связь"},
    {"id": "rent",          "emoji": "🏠", "label": "Аренда"},
    {"id": "insurance",     "emoji": "🛡️", "label": "Страховка"},
    {"id": "utilities",     "emoji": "💡", "label": "Коммуналка"},
    {"id": "gym",           "emoji": "💪", "label": "Фитнес"},
    {"id": "bank",          "emoji": "🏦", "label": "Банк / карта"},
    {"id": "transport",     "emoji": "🚇", "label": "Транспорт"},
    {"id": "other",         "emoji": "📌", "label": "Другое"},
]


def find_budget_user(app: dict, token: str):
    """(uid, user) пользователя бюджета с активной ссылкой на приложение.
    Отключённая ссылка (financeShareEnabled = false) — то же самое, что
    несуществующая: отзыв ссылки должен работать."""
    for u in (app.get("budgetUsers") or []):
        if not isinstance(u, dict):
            continue
        if str(u.get("financeToken") or "") != token:
            continue
        if not u.get("financeShareEnabled", True):
            return None, None
        return str(u.get("id") or ""), u
    return None, None


def budget_slice_of(app: dict, uid: str) -> dict:
    by_user = app.get("budgetByUser")
    if not isinstance(by_user, dict):
        return {}
    sl = by_user.get(uid)
    return sl if isinstance(sl, dict) else {}


def _pf_list(sl: dict, key: str) -> list:
    v = sl.get(key)
    return v if isinstance(v, list) else []


def pf_accounts(sl: dict) -> list:
    """Счета пользователя с балансом: первоначальный остаток + доходы − расходы."""
    ops = _pf_list(sl, "budgetCashflowOps")
    delta = {}
    for op in ops:
        if not isinstance(op, dict):
            continue
        acc = str(op.get("accountId") or "")
        if not acc:
            continue
        amount = op.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        sign = 1 if op.get("direction") == "in" else -1
        delta[acc] = delta.get(acc, 0.0) + sign * float(amount)

    out = []
    for a in _pf_list(sl, "budgetCashflowAccounts"):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        aid = str(a["id"])
        initial = a.get("initialBalance")
        initial = float(initial) if isinstance(initial, (int, float)) else 0.0
        out.append({
            "id": aid,
            "name": str(a.get("name") or "Счёт"),
            "archived": bool(a.get("archived")),
            "initial_balance": round(initial, 2),
            "balance": round(initial + delta.get(aid, 0.0), 2),
        })
    return out


def pf_articles(sl: dict) -> list:
    """Статьи расхода и дохода — ровно те строки вкладки «По месяцу», факт по
    которым ведётся вручную (и, значит, может прийти из ДДС):
      • источники дохода            → direction=in,  id = <srcId>
      • статьи ручных категорий     → direction=out, id = <itemId>
      • категории «Постоянные»      → direction=out, id = __rec_<catId>
    Накопления и Долги сюда не попадают: их факт считается из платежей на
    своих вкладках, запись через ДДС удвоила бы суммы.
    ДЕРЖАТЬ В СИНХРОНЕ с cashflowArticles() из index (9).html."""
    arts = []

    for src in _pf_list(sl, "budgetIncomeSources"):
        if isinstance(src, dict) and src.get("id"):
            arts.append({
                "id": str(src["id"]),
                "name": str(src.get("label") or "Доход"),
                "direction": "in",
                "group_name": "Приход",
                "emoji": "💰",
            })

    for cat in _pf_list(sl, "budgetPlanCategories"):
        if not isinstance(cat, dict):
            continue
        cat_label = str(cat.get("label") or "")
        items = cat.get("items")
        for it in (items if isinstance(items, list) else []):
            if isinstance(it, dict) and it.get("id"):
                arts.append({
                    "id": str(it["id"]),
                    "name": str(it.get("label") or "Статья"),
                    "direction": "out",
                    "group_name": cat_label,
                    "emoji": str(it.get("emoji") or cat.get("emoji") or ""),
                })

    rec_cats = _pf_list(sl, "budgetRecurringCategories") or PF_DEFAULT_RECURRING_CATS
    rec_by_id = {str(c.get("id")): c for c in rec_cats if isinstance(c, dict) and c.get("id")}
    seen_rec = []
    for r in _pf_list(sl, "budgetRecurring"):
        if not isinstance(r, dict) or r.get("archived"):
            continue
        cid = str(r.get("category") or "")
        if cid and cid not in seen_rec:
            seen_rec.append(cid)
    for cid in seen_rec:
        c = rec_by_id.get(cid) or {}
        arts.append({
            "id": f"__rec_{cid}",
            "name": str(c.get("label") or cid),
            "direction": "out",
            "group_name": "Постоянные",
            "emoji": str(c.get("emoji") or "🔄"),
        })

    counts = {}
    for op in _pf_list(sl, "budgetCashflowOps"):
        if isinstance(op, dict) and op.get("categoryId"):
            key = str(op["categoryId"])
            counts[key] = counts.get(key, 0) + 1
    for a in arts:
        a["use_count"] = counts.get(a["id"], 0)
        a["code"] = ""
    return arts


def pf_reminders(sl: dict, month: str) -> list:
    """«Выводы по месяцу» — пункты, которые пользователь себе задал на этот
    месяц во вкладке «ДДС». Приложение показывает их напоминанием.
    Порядок — как во вкладке: по полю sort, затем по времени создания."""
    items = []
    for it in _pf_list(sl, "budgetMonthlyNotes"):
        if not isinstance(it, dict) or not it.get("id"):
            continue
        if str(it.get("month") or "") != month:
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        items.append({
            "id": str(it["id"]),
            "text": text,
            "_sort": it.get("sort") if isinstance(it.get("sort"), (int, float)) else 0,
            "_created": it.get("createdAt") if isinstance(it.get("createdAt"), (int, float)) else 0,
        })
    items.sort(key=lambda i: (i["_sort"], i["_created"]))
    return [{"id": i["id"], "text": i["text"]} for i in items]


def pf_settings(sl: dict) -> dict:
    s = sl.get("budgetCashflowSettings")
    s = s if isinstance(s, dict) else {}
    return {
        "quick_expense_account_id": str(s.get("quickAccountId") or ""),
        "default_currency": str(s.get("defaultCurrency") or "руб."),
    }


def pf_operation_public(op: dict) -> dict:
    date = str(op.get("date") or "")
    return {
        "id": str(op.get("id") or ""),
        "date": date,
        "payment_date": date,
        "direction": "in" if op.get("direction") == "in" else "out",
        "amount": float(op.get("amount") or 0),
        "account_id": str(op.get("accountId") or ""),
        "category_id": str(op.get("categoryId") or ""),
        "purpose": str(op.get("purpose") or ""),
        "comment": str(op.get("purpose") or ""),
        "currency": "руб.",
        "payment_status": "paid",
        "source": str(op.get("source") or ""),
    }


def pf_route(route: str):
    """(token, action) для путей /api/pf/<token>/<action>. (None, None) —
    если токен не проходит валидацию: произвольная строка из URL не должна
    доходить до поиска по данным."""
    prefix = "/api/pf/"
    if not route.startswith(prefix):
        return None, None
    token, _, action = route[len(prefix):].partition("/")
    if not PLANNER_TOKEN_RE.match(token):
        return None, None
    return token, action


PF_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def pf_parse_date(value, fallback: str) -> str:
    raw = str(value or "").strip()
    if not PF_DATE_RE.match(raw):
        return fallback
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return fallback
    return raw


def pf_write(token: str, apply):
    """Обвязка записи из приложения: найти пользователя по токену и под общим
    локом применить apply(app, uid, slice). apply возвращает (code, payload)
    при ошибке или None, если всё в порядке. Срез пользователя создаётся,
    если его ещё нет. Возвращает (http_code, payload)."""
    with APP_DATA_LOCK:
        app = load_app_data() if APP_DATA_FILE.exists() else {}
        uid, _user = find_budget_user(app, token)
        if not uid:
            return 404, {"error": "not found"}
        by_user = app.get("budgetByUser")
        if not isinstance(by_user, dict):
            by_user = {}
        sl = by_user.get(uid)
        if not isinstance(sl, dict):
            sl = {}
        err = apply(app, uid, sl)
        if err:
            return err
        by_user[uid] = sl
        app["budgetByUser"] = by_user
        save_app_data(app)
        return 200, {"ok": True}


# ── HTTP handler ───────────────────────────────────────────────────────────

class JarvisHandler(SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_PUT(self):
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not route_is_public(route) and not self._is_authed():
            self._json(401, {"error": "unauthorized"})
            return
        if route.startswith("/api/pf/"):
            self._pf_put(route)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not route_is_public(route) and not self._is_authed():
            self._json(401, {"error": "unauthorized"})
            return
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
        elif self.path.startswith("/api/files/"):
            filename = self.path[len("/api/files/"):].split("?", 1)[0]
            if "/" in filename or ".." in filename or not filename:
                self._json(400, {"error": "invalid"})
                return
            file_path = DATA_DIR / "files" / filename
            if file_path.exists():
                file_path.unlink()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/login":
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            self._send_login_page(next_path=(query.get("next") or ["/"])[0], error="error" in query)
            return
        if not route_is_public(route) and not self._is_authed():
            if route.startswith("/api/"):
                self._json(401, {"error": "unauthorized"})
            else:
                self._send_login_page(next_path=self.path or "/")
            return
        if route in ("/", "/index.html") or route in SPA_ROUTES:
            self._serve_html()
        elif token_from_route(route, "/e/"):
            # Гостевая страница события — отдельный файл, не SPA.
            self._serve_event_page()
        elif token_from_route(route, "/api/event/"):
            token = token_from_route(route, "/api/event/")
            with APP_DATA_LOCK:
                app = load_app_data() if APP_DATA_FILE.exists() else {}
                ev = find_planner_event(app, token)
                payload = planner_public_payload(app, ev) if ev else None
            if payload is None:
                self._json(404, {"error": "event not found"})
            else:
                self._json(200, payload)
        elif token_from_route(route, "/trip/"):
            # Гостевая страница чек-листа поездки — тоже отдельный файл.
            self._serve_trip_page()
        elif self.path.split("?", 1)[0].startswith("/pf/"):
            # Мобильное приложение личных финансов — отдельная страница.
            self._pf_page(self.path.split("?", 1)[0])
        elif route.startswith("/api/pf/"):
            self._pf_get(route)
        elif token_from_route(route, "/api/camping-trip/"):
            token = token_from_route(route, "/api/camping-trip/")
            with APP_DATA_LOCK:
                app = load_app_data() if APP_DATA_FILE.exists() else {}
                trip = find_camping_trip(app, token)
                payload = camping_trip_public_payload(app, trip) if trip else None
            if payload is None:
                self._json(404, {"error": "trip not found"})
            else:
                self._json(200, payload)
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
            self._json(200, {
                "count": len(subs["chat_ids"]),
                "subscribers": [
                    {"chatId": cid, "name": subscriber_name(subs, cid)}
                    for cid in subs["chat_ids"]
                ],
            })
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
        elif self.path.startswith("/api/files/"):
            filename = self.path[len("/api/files/"):].split("?", 1)[0]
            if "/" in filename or ".." in filename or not filename:
                self._json(400, {"error": "invalid"})
                return
            file_path = DATA_DIR / "files" / filename
            if file_path.exists():
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                ct = FILE_CONTENT_TYPES.get(ext, "application/octet-stream")
                content = file_path.read_bytes()
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
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self._json(200, {})
        else:
            super().do_GET()

    def do_POST(self):
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/login":
            self._handle_login_post()
            return
        if not route_is_public(route) and not self._is_authed():
            self._json(401, {"error": "unauthorized"})
            return
        if token_from_route(route, "/api/event/", "/rsvp"):
            self._event_rsvp(token_from_route(route, "/api/event/", "/rsvp"))
        elif token_from_route(route, "/api/event/", "/comment"):
            self._event_comment(token_from_route(route, "/api/event/", "/comment"))
        elif token_from_route(route, "/api/event/", "/join"):
            self._event_join(token_from_route(route, "/api/event/", "/join"))
        elif token_from_route(route, "/api/event/", "/expense"):
            self._event_expense(token_from_route(route, "/api/event/", "/expense"))
        elif token_from_route(route, "/api/event/", "/expense-delete"):
            self._event_expense_delete(token_from_route(route, "/api/event/", "/expense-delete"))
        elif token_from_route(route, "/api/event/", "/payment"):
            self._event_payment(token_from_route(route, "/api/event/", "/payment"))
        elif token_from_route(route, "/api/event/", "/shopping-add"):
            self._event_shopping_add(token_from_route(route, "/api/event/", "/shopping-add"))
        elif token_from_route(route, "/api/event/", "/shopping-delete"):
            self._event_shopping_delete(token_from_route(route, "/api/event/", "/shopping-delete"))
        elif token_from_route(route, "/api/event/", "/shopping-toggle"):
            self._event_shopping_toggle(token_from_route(route, "/api/event/", "/shopping-toggle"))
        elif route.startswith("/api/pf/"):
            self._pf_post(route)
        elif self.path == "/api/config":
            length = self._content_length()
            if length is None:
                self._json(411, {"error": "Content-Length required"})
                return
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
            length = self._content_length()
            MAX_PHOTO = 20 * 1024 * 1024  # 20 MB hard cap
            if length is None:
                self._json(411, {"error": "Content-Length required"})
                return
            if length > MAX_PHOTO:
                self._json(413, {"error": "file too large"})
                return
            body = self.rfile.read(length)
            if not body:
                self._json(400, {"error": "empty body"})
                return
            filename = str(_uuid.uuid4()) + "." + ext
            photos_dir = DATA_DIR / "photos"
            photos_dir.mkdir(parents=True, exist_ok=True)
            (photos_dir / filename).write_bytes(body)
            self._json(200, {"filename": filename})
        elif self.path.startswith("/api/files"):
            import uuid as _uuid
            ext = "bin"
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("ext="):
                        raw = part[4:].lower()[:5]
                        if raw in ALLOWED_FILE_EXT:
                            ext = raw
                        break
            length = self._content_length()
            MAX_FILE = 25 * 1024 * 1024  # 25 MB hard cap
            if length is None:
                self._json(411, {"error": "Content-Length required"})
                return
            if length > MAX_FILE:
                self._json(413, {"error": "file too large"})
                return
            body = self.rfile.read(length)
            if not body:
                self._json(400, {"error": "empty body"})
                return
            filename = str(_uuid.uuid4()) + "." + ext
            files_dir = DATA_DIR / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            (files_dir / filename).write_bytes(body)
            self._json(200, {"filename": filename})
        elif self.path == "/api/data":
            length = self._content_length()
            if length is None:
                self._json(411, {"error": "Content-Length required"})
                return
            body = self.rfile.read(length)
            try:
                incoming = json.loads(body)
                with APP_DATA_LOCK:
                    existing = load_app_data() if APP_DATA_FILE.exists() else {}
                    # ARG ORDER MATTERS: the first («local», preferred) side must
                    # be the INCOMING payload — the client already merged before
                    # pushing, so its snapshot is the fresh one. Passing the file
                    # first made the server prefer its own stale copy for every
                    # plain-object key: changed reminder times (checklist etc.)
                    # were silently discarded on arrival, forever.
                    merged = merge_app_data(incoming, existing, mode="push")
                    save_app_data(merged)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/api/data/delete-log-entry":
            # Dedicated, immediate deletion endpoint for date-log entries
            # (dailyChecklistLog / dietLog). Bypasses the generic merge logic
            # so a deleted entry can never be resurrected by a racing pull
            # that fetches a server snapshot taken just before this delete.
            length = self._content_length()
            if length is None:
                self._json(411, {"error": "Content-Length required"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                key = payload.get("key")
                date = payload.get("date")
                if key not in DATE_LOG_KEYS or not date:
                    self._json(400, {"error": "invalid key/date"})
                    return
                with APP_DATA_LOCK:
                    app_data = load_app_data() if APP_DATA_FILE.exists() else {}
                    app_data[key] = [
                        e for e in (app_data.get(key) or [])
                        if not (isinstance(e, dict) and e.get("date") == date)
                    ]
                    # Date-keyed tombstone so a pull-merge with a stale server
                    # snapshot (or another device) can't resurrect this day.
                    deleted = dict(app_data.get("deletedIds") or {})
                    coll = dict(deleted.get(key) or {})
                    coll[str(date)] = int(time.time() * 1000)
                    deleted[key] = coll
                    app_data["deletedIds"] = deleted
                    save_app_data(app_data)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/api/alice":
            length = self._content_length()
            body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                payload = {}
            try:
                result = handle_alice_request(payload)
            except Exception as e:
                print(f"  alice webhook error: {e}")
                result = {
                    "version": payload.get("version", "1.0"),
                    "session": payload.get("session") or {},
                    "response": {"text": "Извините, не получилось загрузить дела.", "end_session": True},
                }
            self._json(200, result)
        elif self.path == "/api/backup/send-now":
            token = get_token()
            if not token:
                self._json(400, {"error": "Telegram bot token not configured"})
                return
            try:
                app_data = load_app_data()
                subs = load_subscribers()
                recipients = recipients_for(app_data, subs, "backup")
                if not recipients:
                    self._json(400, {"error": "no subscribers routed to backup"})
                    return
                # Upload runs on a worker thread — a multi-minute send must not
                # freeze the single-threaded HTTP server for every other client.
                started = start_backup_async(token, recipients)
                if started:
                    self._json(200, {"ok": True, "started": True, "recipients": len(recipients)})
                else:
                    self._json(409, {"error": "backup already in progress"})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    # ── Планировщик: гостевые запросы по ссылке ────────────────────────────

    def _guest_json_body(self):
        """Тело гостевого POST-а. Возвращает dict или None (ответ уже отправлен).
        Лимит длины жёсткий: страница события открыта всем, у кого есть ссылка."""
        length = self._content_length()
        if length is None:
            self._json(411, {"error": "Content-Length required"})
            return None
        if length > MAX_EVENT_GUEST_BODY:
            self._json(413, {"error": "payload too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "invalid json"})
            return None
        return payload

    def _event_rsvp(self, token: str):
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        status = str(payload.get("status") or "")
        note = str(payload.get("note") or "")[:MAX_EVENT_NOTE_LEN].strip()
        if status not in PLANNER_STATUSES:
            self._json(400, {"error": "invalid status"})
            return

        def apply(app, ev):
            # id ответа детерминированный (событие + участник): один ответ на
            # человека, а слияние между устройствами разрулит его по updatedAt.
            rid = f"{ev.get('id')}:{friend_id}"
            now_ms = int(time.time() * 1000)
            responses = [r for r in (app.get("plannerResponses") or []) if isinstance(r, dict)]
            existing = next((r for r in responses if r.get("id") == rid), None)
            entry = {
                "id": rid,
                "eventId": ev.get("id"),
                "friendId": friend_id,
                "status": status,
                "note": note,
                "createdAt": (existing or {}).get("createdAt") or now_ms,
                "updatedAt": now_ms,
            }
            app["plannerResponses"] = [r for r in responses if r.get("id") != rid] + [entry]
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_comment(self, token: str):
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        text = str(payload.get("text") or "").strip()[:MAX_EVENT_COMMENT_LEN]
        if not text:
            self._json(400, {"error": "empty comment"})
            return

        def apply(app, ev):
            comments = [c for c in (app.get("plannerComments") or []) if isinstance(c, dict)]
            if sum(1 for c in comments if c.get("eventId") == ev.get("id")) >= MAX_EVENT_COMMENTS:
                return 429, {"error": "too many comments"}
            now_ms = int(time.time() * 1000)
            comments.append({
                "id": str(uuid.uuid4()),
                "eventId": ev.get("id"),
                "friendId": friend_id,
                "text": text,
                "createdAt": now_ms,
                "updatedAt": now_ms,
            })
            app["plannerComments"] = comments
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_join(self, token: str):
        """Открытое событие: гость вписывает своё имя и получает id, под
        которым дальше отвечает и комментирует. Одно и то же имя не плодит
        людей — повторный вход с того же имени возвращает прежний id, так что
        человек со стёртым браузером снова попадает в свой же ответ."""
        payload = self._guest_json_body()
        if payload is None:
            return
        name = normalize_guest_name(payload.get("name"))
        if not name:
            self._json(400, {"error": "empty name"})
            return

        with APP_DATA_LOCK:
            app = load_app_data() if APP_DATA_FILE.exists() else {}
            ev = find_planner_event(app, token)
            if ev is None:
                self._json(404, {"error": "event not found"})
                return
            if event_mode(ev) != "open":
                self._json(403, {"error": "closed guest list"})
                return

            eid = ev.get("id")
            lowered = name.casefold()

            # Уже есть такой участник — приглашённый другом или вписавшийся ранее?
            friends_by_id = {f["id"]: f for f in (app.get("plannerFriends") or [])
                             if isinstance(f, dict) and f.get("id")}
            for fid in (ev.get("participantIds") or []):
                f = friends_by_id.get(fid)
                if f and (f.get("name") or "").casefold() == lowered:
                    self._json(200, {"guestId": fid, **planner_public_payload(app, ev)})
                    return
            for g in event_guests(app, ev):
                if (g.get("name") or "").casefold() == lowered:
                    self._json(200, {"guestId": g["id"], **planner_public_payload(app, ev)})
                    return

            if len(event_guests(app, ev)) >= MAX_EVENT_GUESTS:
                self._json(429, {"error": "too many guests"})
                return

            now_ms = int(time.time() * 1000)
            guest = {
                "id": str(uuid.uuid4()),
                "eventId": eid,
                "name": name,
                "createdAt": now_ms,
                "updatedAt": now_ms,
            }
            guests = [g for g in (app.get("plannerGuests") or []) if isinstance(g, dict)]
            guests.append(guest)
            app["plannerGuests"] = guests
            save_app_data(app)
            self._json(200, {"guestId": guest["id"], **planner_public_payload(app, ev)})

    def _event_expense(self, token: str):
        """Участник заносит свою трату: сумма, за что, куда переводить и
        между кем делить (поровну или вручную). Редактировать и удалять
        трату может только тот, кто её внёс."""
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        cents = money_to_cents(payload.get("amount"))
        if cents is None or cents <= 0 or cents > MAX_EVENT_MONEY_CENTS:
            self._json(400, {"error": "invalid amount"})
            return
        title = " ".join(str(payload.get("title") or "").split())[:MAX_EVENT_EXPENSE_TEXT]
        pay_to = " ".join(str(payload.get("payTo") or "").split())[:MAX_EVENT_EXPENSE_TEXT]
        split_mode = str(payload.get("splitMode") or "equal")
        if split_mode not in EXPENSE_SPLIT_MODES:
            self._json(400, {"error": "invalid split mode"})
            return
        expense_id = str(payload.get("expenseId") or "") or None

        def apply(app, ev):
            allowed = event_participant_ids(app, ev)
            ids = [x for x in (payload.get("participantIds") or []) if x in allowed]
            shares = []
            if split_mode == "custom":
                total = 0
                for sh in (payload.get("shares") or []):
                    if not isinstance(sh, dict):
                        continue
                    pid = sh.get("participantId")
                    c = money_to_cents(sh.get("amount"))
                    if pid not in allowed or c is None or c <= 0:
                        continue
                    total += c
                    shares.append({"participantId": pid, "amount": round(c / 100, 2)})
                if not shares:
                    return 400, {"error": "no shares"}
                # Сумма долей обязана сойтись с общей: иначе часть денег
                # повисает в воздухе и таблица «кто кому должен» врёт.
                if total != cents:
                    return 400, {"error": "shares do not sum to amount"}
                ids = [sh["participantId"] for sh in shares]
            elif not ids:
                return 400, {"error": "no participants"}

            all_expenses = [e for e in (app.get("plannerExpenses") or []) if isinstance(e, dict)]
            now_ms = int(time.time() * 1000)
            existing = None
            if expense_id:
                existing = next((e for e in all_expenses
                                 if e.get("id") == expense_id and e.get("eventId") == ev.get("id")), None)
                if existing is None:
                    return 404, {"error": "expense not found"}
                if existing.get("payerId") != friend_id:
                    return 403, {"error": "not your expense"}
            elif sum(1 for e in all_expenses if e.get("eventId") == ev.get("id")) >= MAX_EVENT_EXPENSES:
                return 429, {"error": "too many expenses"}

            entry = {
                "id": (existing or {}).get("id") or str(uuid.uuid4()),
                "eventId": ev.get("id"),
                "payerId": friend_id,
                "amount": round(cents / 100, 2),
                "title": title,
                "payTo": pay_to,
                "splitMode": split_mode,
                "participantIds": ids,
                "shares": shares,
                "createdAt": (existing or {}).get("createdAt") or now_ms,
                "updatedAt": now_ms,
            }
            app["plannerExpenses"] = [e for e in all_expenses if e.get("id") != entry["id"]] + [entry]
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_expense_delete(self, token: str):
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        expense_id = str(payload.get("expenseId") or "")

        def apply(app, ev):
            all_expenses = [e for e in (app.get("plannerExpenses") or []) if isinstance(e, dict)]
            target = next((e for e in all_expenses
                           if e.get("id") == expense_id and e.get("eventId") == ev.get("id")), None)
            if target is None:
                return 404, {"error": "expense not found"}
            if target.get("payerId") != friend_id:
                return 403, {"error": "not your expense"}
            app["plannerExpenses"] = [e for e in all_expenses if e.get("id") != expense_id]
            # Надгробие: иначе синхронизация с устройства, где трата ещё
            # лежит в localStorage, воскресит её при ближайшем пуше.
            deleted = dict(app.get("deletedIds") or {})
            coll = dict(deleted.get("plannerExpenses") or {})
            coll[expense_id] = int(time.time() * 1000)
            deleted["plannerExpenses"] = coll
            app["deletedIds"] = deleted
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_payment(self, token: str):
        """Отметка «перевёл». Ставит её любая из двух сторон перевода —
        и тот, кто платит, и тот, кто получает."""
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        from_id = str(payload.get("fromId") or "")
        to_id = str(payload.get("toId") or "")
        paid = bool(payload.get("paid"))
        amount = payload.get("amount")
        amount_cents = int(amount) if isinstance(amount, int) and 0 <= amount <= MAX_EVENT_MONEY_CENTS else 0
        if not from_id or not to_id or from_id == to_id:
            self._json(400, {"error": "invalid pair"})
            return
        if friend_id not in (from_id, to_id):
            self._json(403, {"error": "not your payment"})
            return

        def apply(app, ev):
            pid = f"{ev.get('id')}:{from_id}>{to_id}"
            now_ms = int(time.time() * 1000)
            payments = [p for p in (app.get("plannerPayments") or []) if isinstance(p, dict)]
            existing = next((p for p in payments if p.get("id") == pid), None)
            entry = {
                "id": pid,
                "eventId": ev.get("id"),
                "fromId": from_id,
                "toId": to_id,
                "paid": paid,
                "amount": amount_cents if paid else 0,
                "paidAt": now_ms if paid else 0,
                "paidBy": friend_id if paid else "",
                "createdAt": (existing or {}).get("createdAt") or now_ms,
                "updatedAt": now_ms,
            }
            app["plannerPayments"] = [p for p in payments if p.get("id") != pid] + [entry]
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_shopping_add(self, token: str):
        """Список покупок — общий: любой участник может дописать в него
        забытую позицию, как и заносить свою трату."""
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        name = " ".join(str(payload.get("name") or "").split())[:MAX_EVENT_SHOPPING_NAME]
        qty = " ".join(str(payload.get("qty") or "").split())[:MAX_EVENT_SHOPPING_QTY]
        if not name:
            self._json(400, {"error": "empty name"})
            return

        def apply(app, ev):
            items = [i for i in (app.get("plannerShoppingItems") or []) if isinstance(i, dict)]
            if sum(1 for i in items if i.get("eventId") == ev.get("id")) >= MAX_EVENT_SHOPPING_ITEMS:
                return 429, {"error": "too many items"}
            now_ms = int(time.time() * 1000)
            items.append({
                "id": str(uuid.uuid4()),
                "eventId": ev.get("id"),
                "name": name,
                "qty": qty,
                "takenBy": None,
                "takenAt": 0,
                "createdAt": now_ms,
                "updatedAt": now_ms,
            })
            app["plannerShoppingItems"] = items
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_shopping_delete(self, token: str):
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        item_id = str(payload.get("itemId") or "")

        def apply(app, ev):
            items = [i for i in (app.get("plannerShoppingItems") or []) if isinstance(i, dict)]
            target = next((i for i in items
                           if i.get("id") == item_id and i.get("eventId") == ev.get("id")), None)
            if target is None:
                return 404, {"error": "item not found"}
            app["plannerShoppingItems"] = [i for i in items if i.get("id") != item_id]
            deleted = dict(app.get("deletedIds") or {})
            coll = dict(deleted.get("plannerShoppingItems") or {})
            coll[item_id] = int(time.time() * 1000)
            deleted["plannerShoppingItems"] = coll
            app["deletedIds"] = deleted
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _event_shopping_toggle(self, token: str):
        """Отметка «взял(а) в магазине» — ставит и снимает любой участник,
        как и отметку о переводе денег, чтобы ошибочный тап не блокировал
        остальных до появления автора позиции."""
        payload = self._guest_json_body()
        if payload is None:
            return
        friend_id = str(payload.get("friendId") or "")
        item_id = str(payload.get("itemId") or "")
        taken = bool(payload.get("taken"))

        def apply(app, ev):
            items = [i for i in (app.get("plannerShoppingItems") or []) if isinstance(i, dict)]
            target = next((i for i in items
                           if i.get("id") == item_id and i.get("eventId") == ev.get("id")), None)
            if target is None:
                return 404, {"error": "item not found"}
            now_ms = int(time.time() * 1000)
            target["takenBy"] = friend_id if taken else None
            target["takenAt"] = now_ms if taken else 0
            target["updatedAt"] = now_ms
            app["plannerShoppingItems"] = items
            return None

        code, body = planner_guest_write(token, friend_id, apply)
        self._json(code, body)

    def _serve_event_page(self):
        try:
            content = EVENT_PAGE_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            # Ссылку рассылают в мессенджерах — она не должна попадать в поиск.
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"event.html not found")

    def _serve_trip_page(self):
        try:
            content = TRIP_PAGE_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"camping-trip.html not found")

    def _pf_page(self, raw_path: str):
        """Страница приложения и её манифест.

        Канонический адрес — С завершающим слэшем: /pf/<token>/. Только так
        относительная ссылка <link rel="manifest" href="manifest.webmanifest">
        разрешается ВНУТРИ приложения. Без слэша браузер искал бы манифест в
        /pf/manifest.webmanifest, не находил его и при добавлении на экран
        «Домой» брал общий /manifest.json сайта — со start_url «/», из-за чего
        иконка открывала главный экран Jarvis, а не приложение финансов.
        Поэтому /pf/<token> отвечает редиректом на /pf/<token>/."""
        rest = raw_path[len("/pf/"):]
        token, sep, tail = rest.partition("/")
        if not PLANNER_TOKEN_RE.match(token):
            self._json(404, {"error": "not found"})
            return
        if not sep:
            self.send_response(301)
            self.send_header("Location", f"/pf/{token}/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if tail == "":
            self._serve_finance_page()
            return
        if tail == "manifest.webmanifest":
            with APP_DATA_LOCK:
                app = load_app_data() if APP_DATA_FILE.exists() else {}
                _uid, user = find_budget_user(app, token)
            if user is None:
                self._json(404, {"error": "not found"})
                return
            self._pf_manifest(token, str(user.get("name") or ""))
            return
        self._json(404, {"error": "not found"})

    def _serve_finance_page(self):
        try:
            content = FINANCE_PAGE_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            # Личная ссылка: в поиск попадать не должна.
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"finance.html not found")

    # ── Личные финансы: API мобильного приложения ──────────────────────────

    def _pf_get(self, route: str):
        token, action = pf_route(route)
        if not token:
            self._json(404, {"error": "not found"})
            return

        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

        def one(name, default=""):
            v = query.get(name)
            return v[0] if v else default

        with APP_DATA_LOCK:
            app = load_app_data() if APP_DATA_FILE.exists() else {}
            uid, user = find_budget_user(app, token)
            if not uid:
                self._json(404, {"error": "not found"})
                return
            sl = budget_slice_of(app, uid)
            user_name = str((user or {}).get("name") or "")

        if action == "manifest.webmanifest":
            self._pf_manifest(token, user_name)
            return

        if action == "profile":
            self._json(200, {"user_name": user_name})
        elif action == "accounts":
            self._json(200, [a for a in pf_accounts(sl) if not a["archived"]])
        elif action == "categories":
            self._json(200, pf_articles(sl))
        elif action == "categories/popular":
            direction = one("direction", "out")
            if direction not in PF_DIRECTIONS:
                direction = "out"
            try:
                limit = max(1, min(50, int(one("limit", "5"))))
            except ValueError:
                limit = 5
            items = [c for c in pf_articles(sl) if c["direction"] == direction]
            # Ни одной операции ещё нет — показываем начало справочника,
            # иначе экран быстрых статей был бы пустым до первой записи.
            items.sort(key=lambda c: -c["use_count"])
            self._json(200, items[:limit])
        elif action == "settings":
            self._json(200, pf_settings(sl))
        elif action == "reminders":
            month = one("month")
            if not re.match(r"^\d{4}-\d{2}$", month):
                month = today_msk().strftime("%Y-%m")
            self._json(200, {"month": month, "items": pf_reminders(sl, month)})
        elif action == "operations":
            date_from = one("date_from")
            date_to = one("date_to")
            account_id = one("account_id")
            direction = one("direction")
            try:
                limit = max(1, min(500, int(one("limit", "50"))))
            except ValueError:
                limit = 50
            ops = [o for o in _pf_list(sl, "budgetCashflowOps") if isinstance(o, dict)]
            picked = []
            for op in ops:
                d = str(op.get("date") or "")
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue
                if account_id and str(op.get("accountId") or "") != account_id:
                    continue
                if direction in PF_DIRECTIONS and op.get("direction") != direction:
                    continue
                picked.append(op)
            # Новые сверху: внутри одного дня — по времени создания.
            picked.sort(key=lambda o: (str(o.get("date") or ""),
                                       o.get("createdAt") if isinstance(o.get("createdAt"), (int, float)) else 0),
                        reverse=True)
            self._json(200, [pf_operation_public(o) for o in picked[:limit]])
        else:
            self._json(404, {"error": "not found"})

    def _pf_manifest(self, token: str, user_name: str):
        name = f"Финансы — {user_name}" if user_name else "Личные финансы"
        manifest = {
            # id/start_url/scope — по адресу-папке и с токеном внутри: так
            # приложения разных пользователей бюджета остаются РАЗНЫМИ
            # приложениями, а не одной иконкой на телефоне.
            "id": f"/pf/{token}/",
            "name": name,
            # Подпись под иконкой: имя пользователя, чтобы отличать их на
            # экране «Домой».
            "short_name": user_name or "Финансы",
            "description": "Быстрая запись расходов и доходов",
            "start_url": f"/pf/{token}/",
            "scope": f"/pf/{token}/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f2f2fa",
            "theme_color": "#4338ca",
            "lang": "ru",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            ],
        }
        body = json.dumps(manifest, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _pf_body(self):
        """Разобранное тело запроса или (None, ответ уже отправлен)."""
        length = self._content_length()
        if length is None:
            self._json(411, {"error": "Content-Length required"})
            return None
        if length > MAX_PF_BODY:
            self._json(413, {"error": "body too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid json"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "invalid json"})
            return None
        return payload

    def _pf_put(self, route: str):
        token, action = pf_route(route)
        if not token or action != "settings":
            self._json(404, {"error": "not found"})
            return
        payload = self._pf_body()
        if payload is None:
            return
        account_id = str(payload.get("quick_expense_account_id") or "")[:64]

        def apply(app, uid, sl):
            accounts = {a["id"] for a in pf_accounts(sl)}
            if account_id and account_id not in accounts:
                return 400, {"error": "unknown account"}
            settings = sl.get("budgetCashflowSettings")
            settings = dict(settings) if isinstance(settings, dict) else {}
            settings["quickAccountId"] = account_id
            settings["updatedAt"] = int(time.time() * 1000)
            sl["budgetCashflowSettings"] = settings
            return None

        code, body = pf_write(token, apply)
        self._json(code, body)

    def _pf_post(self, route: str):
        token, action = pf_route(route)
        if not token or action != "operations":
            self._json(404, {"error": "not found"})
            return
        payload = self._pf_body()
        if payload is None:
            return

        cents = money_to_cents(payload.get("amount"))
        if cents is None or cents <= 0 or cents > MAX_PF_AMOUNT_CENTS:
            self._json(400, {"error": "invalid amount"})
            return
        amount = cents / 100
        direction = payload.get("direction")
        if direction not in PF_DIRECTIONS:
            self._json(400, {"error": "invalid direction"})
            return
        account_id = str(payload.get("account_id") or "")[:64]
        category_id = str(payload.get("category_id") or "")[:64]
        purpose = str(payload.get("purpose") or payload.get("comment") or "").strip()[:MAX_PF_PURPOSE]
        op_date = pf_parse_date(payload.get("date") or payload.get("payment_date"),
                                today_msk().isoformat())

        created = {"id": ""}

        def apply(app, uid, sl):
            ops = _pf_list(sl, "budgetCashflowOps")
            if len(ops) >= MAX_PF_OPS:
                return 409, {"error": "too many operations"}
            accounts = {a["id"] for a in pf_accounts(sl)}
            if account_id not in accounts:
                return 400, {"error": "unknown account"}
            article = next((c for c in pf_articles(sl)
                            if c["id"] == category_id and c["direction"] == direction), None)
            if article is None:
                return 400, {"error": "unknown category"}
            now_ms = int(time.time() * 1000)
            op = {
                "id": str(uuid.uuid4()),
                "date": op_date,
                "direction": direction,
                "amount": amount,
                "accountId": account_id,
                "categoryId": category_id,
                "purpose": purpose,
                "source": "pwa",
                "createdAt": now_ms,
                "updatedAt": now_ms,
            }
            sl["budgetCashflowOps"] = [*ops, op]
            created["id"] = op["id"]
            return None

        code, body = pf_write(token, apply)
        if code == 200:
            body = {"ok": True, "id": created["id"]}
        self._json(code, body)

    def _get_cookie(self, name: str):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _is_authed(self) -> bool:
        return self._get_cookie(AUTH_COOKIE_NAME) == AUTH_COOKIE_VALUE

    def _send_login_page(self, next_path: str = "/", error: bool = False):
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        err_html = '<p class="err">Неверный код, попробуйте ещё раз</p>' if error else ''
        page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Вход</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0f0f13; color: #eee;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    padding: 20px;
  }}
  form {{
    width: 100%; max-width: 320px; background: #1a1a20; border: 1px solid #2b2b33;
    border-radius: 16px; padding: 32px 28px; text-align: center;
  }}
  h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 20px; color: #eee; }}
  input {{
    width: 100%; background: #0f0f13; border: 1px solid #34343d; color: #eee;
    padding: 14px 16px; border-radius: 10px; font-size: 22px; text-align: center;
    letter-spacing: .3em; font-family: 'JetBrains Mono', monospace;
  }}
  input:focus {{ outline: none; border-color: #f0c14b; }}
  button {{
    width: 100%; margin-top: 14px; padding: 13px; border: none; border-radius: 10px;
    background: #f0c14b; color: #1a1a1f; font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  .err {{ color: #ff6b6b; font-size: 13px; margin: -8px 0 14px; }}
</style>
</head>
<body>
  <form method="POST" action="/login">
    <h1>🔒 Введите код доступа</h1>
    {err_html}
    <input type="password" inputmode="numeric" pattern="[0-9]*" name="password" maxlength="16" autofocus autocomplete="current-password" required>
    <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
    <button type="submit">Войти</button>
  </form>
</body>
</html>"""
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _handle_login_post(self):
        length = self._content_length()
        body = self.rfile.read(length) if length else b""
        fields = parse_qs(body.decode("utf-8", "replace"))
        password = (fields.get("password") or [""])[0]
        next_path = (fields.get("next") or ["/"])[0]
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        if password == AUTH_PASSWORD:
            self.send_response(302)
            self.send_header("Location", next_path)
            self.send_header(
                "Set-Cookie",
                f"{AUTH_COOKIE_NAME}={AUTH_COOKIE_VALUE}; Max-Age={AUTH_COOKIE_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax"
            )
            self._cors()
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header("Location", f"/login?next={quote(next_path)}&error=1")
            self._cors()
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

    def _content_length(self):
        """Parsed Content-Length, or None when missing/malformed. Body-reading
        endpoints must reject None — reading without a length on the
        single-threaded server would block the whole site until EOF."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            n = int(raw)
            return n if n >= 0 else None
        except (TypeError, ValueError):
            return None

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


class JarvisServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients disconnecting mid-response (broken pipe / reset) are routine,
        # not application errors — don't spam the log with a traceback for them.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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
    threading.Thread(target=updates_loop, daemon=True).start()

    server = JarvisServer(("0.0.0.0", args.port), JarvisHandler)
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
