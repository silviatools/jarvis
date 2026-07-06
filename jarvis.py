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

# Clean-URL deep links (e.g. /mybody) → serve the SPA, which reads the path
# client-side and jumps straight to the matching tab. Keep in sync with
# PATH_TAB_MAP in index (9).html.
SPA_ROUTES = {
    "/mybody", "/budget", "/supplements", "/meals", "/weather",
    "/house", "/cars", "/holidays", "/settings",
}

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
    app = load_app_data()
    log = [e for e in app.get("dietLog", []) if e.get("date") != date_iso]
    log.append({"id": str(_uuid.uuid4()), "date": date_iso, "level": level})
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


# ── daily checklist ────────────────────────────────────────────────────────────

def load_app_data() -> dict:
    if APP_DATA_FILE.exists():
        try:
            return json.loads(APP_DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_app_data(app: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APP_DATA_FILE.write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _merge_id_arrays(local_arr, server_arr, prefer_local: bool) -> list:
    by_id: dict = {}
    first = server_arr if prefer_local else local_arr
    second = local_arr if prefer_local else server_arr
    for e in (first if isinstance(first, list) else []):
        if isinstance(e, dict) and e.get("id") is not None:
            by_id[e["id"]] = e
    for e in (second if isinstance(second, list) else []):
        if isinstance(e, dict) and e.get("id") is not None:
            by_id[e["id"]] = e
    return list(by_id.values())


def _merge_date_log_entries(local_arr, server_arr, prefer_local: bool) -> list:
    def _norm(e):
        if not isinstance(e, dict) or not e.get("date"):
            return None
        out = dict(e)
        if "answers" in e or any(isinstance(v, dict) for v in [e.get("answers")]):
            out["answers"] = {**(e.get("answers") or {})}
        return out

    local = [x for x in ((_norm(e) for e in (local_arr or []))) if x]
    server = [x for x in ((_norm(e) for e in (server_arr or []))) if x]
    server_by_date = {e["date"]: e for e in server}

    if prefer_local:
        result = []
        for entry in local:
            srv = server_by_date.get(entry["date"])
            if not srv:
                result.append(entry)
                continue
            merged = {**srv, **entry}
            if "answers" in srv or "answers" in entry:
                merged["answers"] = {**(srv.get("answers") or {}), **(entry.get("answers") or {})}
            if "level" in entry:
                merged["level"] = entry["level"]
            elif "level" in srv:
                merged["level"] = srv["level"]
            merged["id"] = entry.get("id") or srv.get("id")
            result.append(merged)
        return sorted(result, key=lambda e: e.get("date", ""), reverse=True)

    by_date: dict[str, dict] = {}
    for e in local:
        by_date[e["date"]] = dict(e)
    for e in server:
        prev = by_date.get(e["date"])
        if not prev:
            by_date[e["date"]] = dict(e)
            continue
        merged = {**prev, **e}
        if "answers" in prev or "answers" in e:
            merged["answers"] = {**(prev.get("answers") or {}), **(e.get("answers") or {})}
        if "level" in e:
            merged["level"] = e["level"]
        merged["id"] = e.get("id") or prev.get("id")
        by_date[e["date"]] = merged
    return sorted(by_date.values(), key=lambda e: e.get("date", ""), reverse=True)


def merge_app_data(local: dict, server: dict, mode: str = "pull") -> dict:
    """Merge app-data dicts. mode='push' → incoming (local) wins; mode='pull' → local wins except bot logs."""
    if not server:
        return local or {}
    if not local:
        return server or {}
    merged = {**local, **server}
    keys = set(local.keys()) | set(server.keys())
    for key in keys:
        l = local.get(key)
        s = server.get(key)
        if s is None:
            merged[key] = l
        elif l is None:
            merged[key] = s
        else:
            prefer_local = _prefer_local_for_key(key, mode)
            if key in DATE_LOG_KEYS and (isinstance(l, list) or isinstance(s, list)):
                merged[key] = _merge_date_log_entries(l, s, prefer_local)
            elif isinstance(l, list) or isinstance(s, list):
                merged[key] = _merge_id_arrays(l, s, prefer_local)
            elif _is_plain_object(l) and _is_plain_object(s):
                merged[key] = {**s, **l} if prefer_local else {**l, **s}
            else:
                merged[key] = l if prefer_local else s
    return merged


def get_checklist_entry(app: dict, date_iso: str) -> dict | None:
    for e in app.get("dailyChecklistLog", []):
        if e.get("date") == date_iso:
            return e
    return None


def save_checklist_answer(date_iso: str, field_idx: int, opt_idx: int) -> tuple[str, str] | None:
    """Save one checklist answer. Returns (field_label, option_text) or None."""
    import uuid as _uuid
    app = load_app_data()
    fields = app.get("dailyChecklistFields") or []
    if field_idx < 0 or field_idx >= len(fields):
        return None
    field = fields[field_idx]
    options = field.get("options") or []
    if opt_idx < 0 or opt_idx >= len(options):
        return None
    option_text = _option_label(options[opt_idx])
    field_id = field.get("id", str(field_idx))
    entry = get_checklist_entry(app, date_iso)
    if entry:
        answers = {**(entry.get("answers") or {}), field_id: option_text}
        entry = {**entry, "answers": answers}
        log = [e for e in app.get("dailyChecklistLog", []) if e.get("date") != date_iso]
    else:
        entry = {"id": str(_uuid.uuid4()), "date": date_iso, "answers": {field_id: option_text}}
        log = list(app.get("dailyChecklistLog", []))
    log.append(entry)
    log.sort(key=lambda e: e.get("date", ""), reverse=True)
    app["dailyChecklistLog"] = log
    save_app_data(app)
    return field.get("label", ""), option_text


def next_unanswered_field_idx(app: dict, date_iso: str) -> int | None:
    fields = app.get("dailyChecklistFields") or []
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
    fields = app.get("dailyChecklistFields") or []
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
            text = f"🏠 <b>По дому — напоминание</b>\n\n{chore.get('name', 'Дело')}"
            print(f"[{now_str} MSK] → {chore['name']} ({len(subs['chat_ids'])} subscriber(s))")
            for cid in subs["chat_ids"]:
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
            text = f"💼 <b>Босс — напоминание</b>\n\n{task.get('name', 'Задача')}"
            print(f"[{now_str} MSK] → boss: {task['name']} ({len(subs['chat_ids'])} subscriber(s))")
            for cid in subs["chat_ids"]:
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
                        print(f"[{now_str} MSK] → holiday: {holiday['name']} in {days_before}d ({len(subs['chat_ids'])} subscriber(s))")
                        for cid in subs["chat_ids"]:
                            send_message(token, cid, text)
                    except (ValueError, OverflowError):
                        pass
    except Exception as e:
        print(f"[{now_str} MSK] holidays reminder error: {e}")

    # ── Debts: one-off reminder at notifyDate + notifyTime (MSK) ──────────
    try:
        debts = app_data_raw.get("budgetDebts", [])
        for debt in debts:
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
            print(f"[{now_str} MSK] → debt: {debtor} {amount} ({len(subs['chat_ids'])} subscriber(s))")
            for cid in subs["chat_ids"]:
                send_message(token, cid, text)
    except Exception as e:
        print(f"[{now_str} MSK] debts reminder error: {e}")

    # ── Diet compliance: recurring «Как ты кушал сегодня?» ────────────────
    try:
        reminder = app_data_raw.get("dietReminder") or {}
        diet_log = app_data_raw.get("dietLog", [])
        if reminder.get("enabled") and str(reminder.get("time", "")).strip() == now_str:
            days = reminder.get("days", [0, 1, 2, 3, 4, 5, 6]) or []
            already = any(e.get("date") == today_iso for e in diet_log)
            if today_js in days and not already:
                kb = diet_keyboard(today_iso)
                print(f"[{now_str} MSK] → diet ask ({len(subs['chat_ids'])} subscriber(s))")
                for cid in subs["chat_ids"]:
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
        checklist_fields = app_data_raw.get("dailyChecklistFields") or []
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
            if today_js not in days:
                print(f"[{now_str} MSK] checklist reminder: today ({today_js}) not in days {days}")
            elif not checklist_fields:
                print(f"[{now_str} MSK] checklist reminder: no dailyChecklistFields configured")
            elif not subs["chat_ids"]:
                print(f"[{now_str} MSK] checklist reminder: no subscribers")
            else:
                idx = next_unanswered_field_idx(app_data_raw, today_iso)
                if idx is None:
                    print(f"[{now_str} MSK] checklist reminder: all fields already answered for {today_iso}")
                else:
                    print(f"[{now_str} MSK] → checklist ask ({len(subs['chat_ids'])} subscriber(s))")
                    for cid in subs["chat_ids"]:
                        send_checklist_question(token, cid, today_iso)
    except Exception as e:
        print(f"[{now_str} MSK] checklist reminder error: {e}")


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
        if route in ("/", "/index.html") or route in SPA_ROUTES:
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
            length = int(self.headers.get("Content-Length", 0))
            MAX_FILE = 25 * 1024 * 1024  # 25 MB hard cap
            if length > MAX_FILE:
                self._json(413, {"error": "file too large"})
                return
            body = self.rfile.read(length) if length else self.rfile.read(MAX_FILE)
            if not body:
                self._json(400, {"error": "empty body"})
                return
            if len(body) > MAX_FILE:
                self._json(413, {"error": "file too large"})
                return
            filename = str(_uuid.uuid4()) + "." + ext
            files_dir = DATA_DIR / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            (files_dir / filename).write_bytes(body)
            self._json(200, {"filename": filename})
        elif self.path == "/api/data":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                incoming = json.loads(body)
                existing = load_app_data() if APP_DATA_FILE.exists() else {}
                merged = merge_app_data(existing, incoming, mode="push")
                save_app_data(merged)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/api/data/delete-log-entry":
            # Dedicated, immediate deletion endpoint for date-log entries
            # (dailyChecklistLog / dietLog). Bypasses the generic merge logic
            # so a deleted entry can never be resurrected by a racing pull
            # that fetches a server snapshot taken just before this delete.
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                key = payload.get("key")
                date = payload.get("date")
                if key not in DATE_LOG_KEYS or not date:
                    self._json(400, {"error": "invalid key/date"})
                    return
                app_data = load_app_data() if APP_DATA_FILE.exists() else {}
                app_data[key] = [
                    e for e in (app_data.get(key) or [])
                    if not (isinstance(e, dict) and e.get("date") == date)
                ]
                save_app_data(app_data)
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
    threading.Thread(target=updates_loop, daemon=True).start()

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
