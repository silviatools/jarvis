"""WB on a user's Chrome: outbound polling, scoped device key, read-only jobs.

Only the key hash is persisted. Jobs/results live in memory, expire, and never
contain cookies. The device key cannot authenticate to any other Jarvis API.
"""

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

STORE_NAME = "jarvis_market_pc.json"
ONLINE_SECONDS = 100
JOB_SECONDS = 90
SECTIONS = {"status", "orders", "delivery"}
DEVICE_ROUTES = {"/api/market/pc/poll", "/api/market/pc/result"}
ERRORS = {
    "offline": "ПК с Wildberries не на связи. Откройте Chrome с расширением Джарвиса на Windows.",
    "busy": "ПК уже читает кабинет WB. Повторите запрос немного позже.",
    "timeout": "ПК не успел прочитать WB. Проверьте Chrome и повторите запрос.",
    "revoked": "Подключение ПК отключено или ключ заменён.",
    "login": "Войдите в Wildberries в профиле Chrome с расширением Джарвиса на ПК.",
    "blocked": "Wildberries проверяет или блокирует браузер. Откройте кабинет на ПК.",
    "unreadable": "Кабинет WB не удалось прочитать. Откройте покупки или доставки на ПК и повторите запрос.",
    "browser": "Не удалось открыть вкладку WB на ПК. Проверьте Chrome и расширение.",
}
_lock = threading.RLock()
_jobs = {}
_live = {"seen": 0, "session": "unknown", "checked": 0}


def _path():
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / STORE_NAME


def _config():
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled():
    with _lock:
        return bool(_config().get("key_hash"))


def public_status():
    with _lock:
        on = enabled()
        return {
            "enabled": on,
            "online": on and time.time() - _live["seen"] < ONLINE_SECONDS,
            "last_seen": int(_live["seen"]) if on else 0,
            "session": _live["session"] if on else "unknown",
            "checked": int(_live["checked"]) if on else 0,
        }


def _cancel_jobs():
    for job in _jobs.values():
        job["result"] = _error("revoked")
        job["event"].set()
    _jobs.clear()
    _live.update(seen=0, session="unknown", checked=0)


def pair():
    token = "wbpc_" + secrets.token_urlsafe(32)
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"key_hash": hashlib.sha256(token.encode()).hexdigest()}, out)
        os.replace(tmp, path)
        _cancel_jobs()
    return {"ok": True, "token": token, "message": "Ключ создан. Вставьте его в расширение на ПК. Старый ключ отключён."}


def disconnect():
    with _lock:
        _path().unlink(missing_ok=True)
        _cancel_jobs()
    return {"ok": True, "message": "ПК отключён. WB снова использует серверный способ подключения."}


def _valid(token):
    digest = _config().get("key_hash")
    return (isinstance(token, str) and token.startswith("wbpc_") and len(token) < 100
            and isinstance(digest, str)
            and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), digest))


def _error(code):
    return {"ok": False, "shop": "wb", "source": "pc", "code": code, "error": ERRORS[code]}


def request(section, timeout=JOB_SECONDS):
    if not isinstance(section, str) or section not in SECTIONS:
        return {"ok": False, "error": "На ПК пока доступны только покупки, доставки и проверка входа WB."}
    with _lock:
        if not public_status()["online"]:
            return _error("offline")
        if _jobs:
            return _error("busy")
        jid = secrets.token_urlsafe(18)
        job = {"section": section, "expires": time.time() + timeout, "event": threading.Event(),
               "claimed": False, "result": None, "key_hash": _config().get("key_hash")}
        _jobs[jid] = job
    try:
        if not job["event"].wait(timeout):
            return _error("timeout")
        return job["result"] or _error("unreadable")
    finally:
        with _lock:
            _jobs.pop(jid, None)


def device_call(route, token, payload):
    """Authenticate here even when the request also carries an owner cookie."""
    with _lock:
        if not _valid(token):
            return 401, {"ok": False, "error": "invalid device key"}
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "invalid payload"}
        _live["seen"] = time.time()
        if route == "/api/market/pc/poll":
            for jid, job in _jobs.items():
                if not job["claimed"] and job["expires"] > time.time() and not job["event"].is_set():
                    job["claimed"] = True
                    return 200, {"ok": True, "job": {"id": jid, "section": job["section"]}}
            return 200, {"ok": True, "job": None}
        if route != "/api/market/pc/result":
            return 404, {"ok": False, "error": "not found"}
        jid = payload.get("id")
        job = _jobs.get(jid) if isinstance(jid, str) else None
        if (not job or not job["claimed"] or job["expires"] <= time.time()
                or job["key_hash"] != _config().get("key_hash")):
            return 409, {"ok": False, "error": "job expired"}
        if job["event"].is_set():
            return 200, {"ok": True}  # delivery retry, don't overwrite the result
        result = _clean_result(job["section"], payload.get("result"))
        job["result"] = result
        _live["session"] = result.get("session", "unknown")
        _live["checked"] = time.time()
        job["event"].set()
        return 200, {"ok": True}


def _clean_result(section, raw):
    # Import lazily: market_skill routes reads here, and owns the existing
    # phone/secret redaction and the common order presentation.
    import market_skill
    def image_url(value):
        try:
            u = urlparse(value if isinstance(value, str) else "")
            host = (u.hostname or "").lower()
            if u.scheme == "https" and not u.username and not u.password and any(
                    host == domain or host.endswith("." + domain)
                    for domain in ("wildberries.ru", "wb.ru", "wbbasket.ru", "wbstatic.net", "wbcontent.net")):
                return ("https://" + u.netloc + u.path)[:600]
        except ValueError:
            pass
        return ""
    if not isinstance(raw, dict):
        return _error("unreadable")
    if raw.get("ok") is not True:
        code = raw.get("code")
        code = code if isinstance(code, str) and code in {"login", "blocked", "unreadable", "browser"} else "unreadable"
        return {**_error(code), "session": "expired" if code == "login" else "blocked" if code == "blocked" else "unknown"}
    url = raw.get("url")
    try:
        parsed = urlparse(url if isinstance(url, str) else "")
        expected = "/lk/myorders/archive" if section == "orders" else "/lk/myorders/delivery"
        if (parsed.scheme != "https" or parsed.netloc != "www.wildberries.ru"
                or parsed.path.rstrip("/") != expected or raw.get("cabinet") is not True):
            return _error("unreadable")
    except ValueError:
        return _error("unreadable")
    rows = []
    candidates = raw.get("rows")
    for item in (candidates[:40] if isinstance(candidates, list) else []):
        if not isinstance(item, dict):
            continue
        row = {}
        for field, limit in {"id": 80, "name": 160, "price": 40, "status": 100, "date": 50, "pvz": 180,
                             "brand": 100, "size": 60, "quantity": 30, "payable": 60,
                             "delivery_date": 60, "pvz_hours": 200}.items():
            value = item.get(field)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                row[field] = market_skill.mask_phones(market_skill.scrub_secrets(str(value)))[:limit]
        article = str(item.get("article") or "")
        if re.fullmatch(r"\d{4,15}", article):
            row["article"] = article
            row["product_url"] = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        photo = image_url(item.get("image_url"))
        if photo:
            row["image_url"] = photo
        extras = item.get("extra_fields")
        row["extra_fields"] = []
        for extra in (extras[:100] if isinstance(extras, list) else []):
            if not isinstance(extra, dict):
                continue
            path, value = extra.get("path"), extra.get("value")
            if (not isinstance(path, str) or not isinstance(value, str) or len(path) > 180
                    or not re.fullmatch(r"(?:order|item|delivery|office|product|status|page)(?:\.[A-Za-z0-9_*]+)+", path)
                    or re.search(r"token|password|secret|cookie|authorization|(?:^|\.)(?:sign|signature|tracker_key|trackerkey|session_id|sessionid|shard_key|user_id|client_id|phone|email|card_number|bank_account)(?:\.|$)", path, re.I)):
                continue
            row["extra_fields"].append({"path": path, "value": market_skill.mask_phones(market_skill.scrub_secrets(value))[:600]})
        if row.get("id") and (row.get("name") or row.get("status")):
            rows.append(row)
    if section != "status" and not rows and raw.get("empty") is not True:
        return _error("unreadable")
    text = "Кабинет WB на ПК открыт." if section == "status" else market_skill.format_wb_rows(rows)
    if not text:
        text = "Доставок нет." if section == "delivery" else "Покупок нет."
    page_products = []
    observed = raw.get("page_products")
    for item in (observed[:40] if isinstance(observed, list) else []):
        if not isinstance(item, dict) or not re.fullmatch(r"\d{4,15}", str(item.get("article") or "")):
            continue
        article = str(item["article"])
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        product = {"article": article, "name": market_skill.mask_phones(market_skill.scrub_secrets(name))[:160],
                   "product_url": f"https://www.wildberries.ru/catalog/{article}/detail.aspx"}
        photo = image_url(item.get("image_url"))
        if photo:
            product["image_url"] = photo
        page_products.append(product)
    if page_products:
        text += "\n\nТовары со страницы доставок, ещё НЕ сопоставленные со статусами (порядок не устанавливает связь):\n"
        for product in page_products:
            text += (product["name"] or "Товар") + " · артикул " + product["article"] + " · " + product["product_url"] + "\n"
    labels = raw.get("page_amount_due_labels")
    labels = [market_skill.mask_phones(market_skill.scrub_secrets(value))[:180]
              for value in (labels[:12] if isinstance(labels, list) else []) if isinstance(value, str)]
    if labels:
        text += "\nПодписи об оплате на всей странице (не итог и не сумма для каждого товара): " + "; ".join(labels)
    return {"ok": True, "shop": "wb", "source": "pc", "section": section, "session": "active",
            "url": "https://www.wildberries.ru" + parsed.path, "rows": rows, "text": text[:12000],
            "page_products": page_products, "page_amount_due_labels": labels,
            "truncated": raw.get("truncated") is True, "details_truncated": raw.get("details_truncated") is True}


def extension_zip():
    root = Path(__file__).parent / "windows" / "wb-extension"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name in ("manifest.json", "background.js", "field-utils.js", "page-data.js", "capture.js", "popup.html", "popup.js", "fields.html", "fields.js", "README.txt"):
            out.write(root / name, name)
    return buf.getvalue()
