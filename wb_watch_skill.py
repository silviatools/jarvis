"""Hourly WB delivery checks; pickup notices at 10:00 Moscow, without an AI call.

The existing Windows bridge supplies a fresh snapshot. Only successfully sent
notices advance the persistent per-position reminder clock. No account cookies
or device keys are stored here.
"""

import html
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_ID = "wb_pickup"
STORE_NAME = "jarvis_wb_pickup.json"
MSK = timezone(timedelta(hours=3))
_lock = threading.Lock()

SKILLS = [{
    "id": SKILL_ID, "title": "WB: готовность заказов к выдаче", "risk": "read", "default": True,
    "desc": (
        "Каждый час проверяет доставки Wildberries через подключённый Windows-ПК. "
        "В 10:00 по Москве сообщает в личный чат владельца с Telegram-ботом только о товарах, "
        "которые можно забирать: название, цена и статус. Готовность после 10:00 — следующим утром. "
        "Повтор — через три дня в 10:00, если товар всё ещё ждёт выдачи; после получения напоминания прекращаются. "
        "История сохраняется при перезапуске. Нужны работающие ПК и Chrome, вход в WB, навык «Маркетплейсы» "
        "и подключённый личный Telegram владельца. Ошибки чтения не вызывают оповещений."
    ), "tools": [],
}]


def skill_on(state):
    state = state if isinstance(state, dict) else {}
    return state.get(SKILL_ID, True) is not False


def _enabled(app):
    state = (app or {}).get("telegramSkills") or {}
    return skill_on(state) and state.get("marketplaces", True) is not False


def store_path():
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / STORE_NAME


def _load():
    try:
        state = json.loads(store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"orders": {}, "next_check": 0}
    # A damaged history must not silently become an empty deduplication table.
    if not isinstance(state, dict) or not isinstance(state.get("orders"), dict):
        raise ValueError("Invalid WB pickup history")
    return state


def _save(state):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(state, out, ensure_ascii=False)
    os.replace(tmp, path)


def is_ready(status):
    """Conservative positive statuses, never a generic 'delivered' substring."""
    if not isinstance(status, str):
        return False
    status = " ".join(status.lower().replace("ё", "е").split()).strip(" .!")
    return status in {
        "приходите за товаром", "можно забирать", "можно забрать", "ожидает получения",
        "доставлен в пункт выдачи", "доставлено в пункт выдачи", "ожидает в пункте выдачи",
    } or bool(re.fullmatch(r"(?:(?:товар|заказ) )?готов[аоы]? к (?:выдаче|получению)", status))


def _window(now):
    # Short recovery window for a slow PC read / transient Telegram failure.
    local = datetime.fromtimestamp(now, MSK)
    return local.hour == 10 and local.minute < 10


def _repeat_due(now, since):
    # Compare Moscow dates: a 30-second difference in PC response time must
    # not move a reminder from the third morning to the fourth.
    return datetime.fromtimestamp(now, MSK).date() >= datetime.fromtimestamp(since, MSK).date() + timedelta(days=3)


def _message(row, repeat):
    name = str(row.get("name") or "Товар Wildberries")[:300]
    price = str(row.get("price") or "")[:100]
    head = "🛒 <b>WB: товар всё ещё ждёт выдачи</b>" if repeat else "🛒 <b>WB: можно забирать</b>"
    item = html.escape(name) + (" — " + html.escape(price) if price else "")
    return head + "\n\n" + item + "\nМожно забирать."


def _run(load_app, read, notify, clock):
    """Called with _lock held; read and notify are injected by the server."""
    if not _enabled(load_app()):
        return
    now = clock()
    state = _load()
    if now < state.get("next_check", 0):
        return
    # Persist the claim before the potentially slow read; normal checks align
    # with the next full hour, so a 09:59 initial check cannot skip 10:00.
    state["next_check"] = (int(now) // 3600 + 1) * 3600
    state["last_attempt"] = now
    _save(state)
    try:
        result = read()
    except Exception as exc:
        print(f"WB pickup read failed: {type(exc).__name__}", flush=True)
        result = {"ok": False, "code": "read_failed"}
    now = clock()
    if not _enabled(load_app()):
        return
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("rows"), list):
        code = result.get("code") if isinstance(result, dict) else None
        state["last_result"] = code if code in {"offline", "busy", "timeout", "revoked", "login", "blocked", "unreadable", "browser"} else "read_failed"
        state["next_check"] = now + (60 if code == "busy" else 300)
        _save(state)
        return
    state["last_success"] = now
    state["last_result"] = "ok"
    orders = state["orders"]
    current = {}
    for row in result["rows"]:
        if not isinstance(row, dict) or not row.get("id") or not isinstance(row.get("status"), str) or not row["status"].strip():
            continue
        rid = str(row["id"])
        current[rid] = row
        previous = orders.get(rid) or {}
        ready = is_ready(row["status"])
        orders[rid] = {
            "ready": ready, "ready_since": previous.get("ready_since", now) if previous.get("ready") and ready else now,
            "last_notified": previous.get("last_notified", 0), "last_seen": now,
        }
    # Missing entries (including truncated lists) are never inferred to be
    # collected or used for sending: a notice requires a current ready row.
    orders = state["orders"] = {rid: row for rid, row in orders.items() if now - row.get("last_seen", now) < 90 * 86400}
    _save(state)
    if not _window(now):
        return
    for rid, row in current.items():
        tracked = orders[rid]
        last = tracked["last_notified"]
        if not tracked["ready"] or (last and not _repeat_due(now, max(last, tracked["ready_since"]))):
            continue
        # The toggle may change while another position is being delivered.
        if not _enabled(load_app()) or not _window(clock()):
            break
        try:
            delivered = notify(_message(row, bool(last)))
        except Exception as exc:
            print(f"WB pickup notify failed: {type(exc).__name__}", flush=True)
            delivered = False
        if delivered:
            tracked["last_notified"] = now
            _save(state)  # Commit each position separately after Telegram's ACK.
        else:
            state["next_check"] = min(state["next_check"], now + 60)
            state["last_result"] = "notify_failed"
            _save(state)


def run_once(load_app, read, notify, clock=time.time):
    """Non-overlapping check; synchronous for tests, background via tick()."""
    if not _lock.acquire(blocking=False):
        return
    try:
        _run(load_app, read, notify, clock)
    finally:
        _lock.release()


def tick(load_app, read, notify):
    """Fast minute-loop hook. Never wait for the Windows PC in that loop."""
    if not _enabled(load_app()) or not _lock.acquire(blocking=False):
        return
    try:
        if time.time() < _load().get("next_check", 0):
            _lock.release()
            return
        def worker():
            try:
                _run(load_app, read, notify, time.time)
            except Exception as exc:
                print(f"WB pickup check failed: {type(exc).__name__}", flush=True)
            finally:
                _lock.release()
        threading.Thread(target=worker, daemon=True, name="wb-pickup").start()
    except Exception:
        _lock.release()
        raise
