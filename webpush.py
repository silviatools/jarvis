"""
webpush.py — Web Push для приложения Джарвис (iPhone с экрана «Домой», iOS 16.4+).

VAPID и подписки лежат в DATA_DIR/jarvis_webpush.json (права 600).
Приватный ключ, auth и endpoint в логи и в ответы API не попадают.
Публичный ключ отдаётся браузеру: без него PushManager не подпишется.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

STORE_NAME = "jarvis_webpush.json"
# Запасной sub, если нет своего https-адреса. Apple отклоняет mailto на localhost
# ответом 403 {"reason":"BadJwtToken"}. Путь в URL py_vapid не принимает — только origin.
_VAPID_SUB_FALLBACK = "https://github.com"
_MAX_SUBS = 12
_MAX_PAYLOAD = 2800
_EXP_SECONDS = 12 * 60 * 60
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)
_lock = threading.RLock()
_logs = []
_deliver_impl = None
_jwt_logged = False
_key_announced = False


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def _load():
    """Словарь из файла, None если файла нет, False если файл есть, но не читается.

    Сломанный файл нельзя затирать новыми ключами: старая подписка на телефоне
    останется привязана к прежнему публичному ключу.
    """
    path = store_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("subscriptions"), list):
        data["subscriptions"] = []
    return data


def _save(st: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def redact(text, extra=()) -> str:
    """Убирает ключи и подписки из строки, которую можно печатать."""
    out = str(text or "")
    out = _PEM_RE.sub("***", out)
    secrets = [s for s in extra if s]
    try:
        st = _load() or {}
    except Exception:
        st = {}
    for key in ("private_key", "public_key"):
        if st.get(key):
            secrets.append(st[key])
    for sub in st.get("subscriptions") or []:
        if not isinstance(sub, dict):
            continue
        for key in ("endpoint", "p256dh", "auth"):
            if sub.get(key):
                secrets.append(sub[key])
    for secret in secrets:
        secret = str(secret)
        if len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


def _log(msg: str) -> None:
    line = "push: " + redact(msg)[:400]
    _logs.append(line)
    print(line, flush=True)


def _b64(data: bytes) -> str:
    from py_vapid.utils import b64urlencode
    raw = b64urlencode(data)
    if isinstance(raw, bytes):
        raw = raw.decode()
    return raw.rstrip("=")


def _generate() -> dict:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    vapid = Vapid()
    vapid.generate_keys()
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private_key": _b64(private), "public_key": _b64(public)}


def _announce(created: bool) -> None:
    global _key_announced
    if _key_announced:
        return
    _key_announced = True
    _log("ключ создан" if created else "ключ загружен")


def ensure() -> dict:
    """Ключи создаются один раз и лежат в DATA_DIR. Повторный вызов и рестарт их не меняют."""
    global _key_announced
    with _lock:
        st = _load()
        if st is False:
            _log("файл ключа не читается, новый не создаю")
            return {"subscriptions": []}
        if isinstance(st, dict) and st.get("private_key") and st.get("public_key"):
            _announce(False)
            return st
        if st is not None:
            _log("файл ключа без пары ключей, новый не создаю")
            return st if isinstance(st, dict) else {"subscriptions": []}
        fresh = _generate()
        st = {
            "private_key": fresh["private_key"],
            "public_key": fresh["public_key"],
            "subscriptions": [],
        }
        _save(st)
        _announce(True)
        return st


def public_status() -> dict:
    try:
        st = ensure()
    except Exception as e:
        _log("ключ пуша: " + redact(str(e)))
        return {"ok": False, "error": "Не удалось подготовить ключ пуша.", "subscriptions": 0}
    return {
        "ok": True,
        "publicKey": st.get("public_key") or "",
        "subscriptions": len(st.get("subscriptions") or []),
    }


def subscription_count() -> int:
    st = _load()
    if not isinstance(st, dict):
        return 0
    return len(st.get("subscriptions") or [])


def https_origin(url: str) -> str:
    """https-origin без localhost. Путь отбрасывается: py_vapid ждёт только scheme://host."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return ""
    if host in ("localhost", "127.0.0.1") or host.endswith(".localhost") or host.endswith(".local"):
        return ""
    if "." not in host:
        return ""
    return "https://" + host


def _mailto_ok(value: str) -> bool:
    match = re.match(r"^mailto:([^@\s]+)@([^@\s]+)$", (value or "").strip(), re.I)
    if not match:
        return False
    host = match.group(2).lower().rstrip(".")
    if host in ("localhost", "127.0.0.1") or host.endswith(".localhost") or host.endswith(".local"):
        return False
    return "." in host and " " not in host


def subject_ok(value: str) -> bool:
    raw = (value or "").strip()
    if not raw or "localhost" in raw.lower():
        return False
    if raw.lower().startswith("mailto:"):
        return _mailto_ok(raw)
    return bool(https_origin(raw)) and raw.lower().startswith("https://")


def remember_origin(url: str) -> None:
    """Запоминает https-адрес сайта как VAPID sub. Уже годный адрес не затирает."""
    origin = https_origin(url)
    if not origin:
        return
    with _lock:
        st = _load()
        if not isinstance(st, dict) or not st.get("private_key"):
            return
        if subject_ok(st.get("subject") or ""):
            return
        st["subject"] = origin
        _save(st)


def vapid_subject() -> str:
    for raw in (os.environ.get("VAPID_SUBJECT") or "", os.environ.get("PUSH_SUBJECT") or ""):
        if subject_ok(raw):
            return raw.strip() if raw.strip().lower().startswith("mailto:") else https_origin(raw)
    st = _load()
    stored = (st or {}).get("subject") if isinstance(st, dict) else ""
    if subject_ok(stored or ""):
        text = str(stored).strip()
        return text if text.lower().startswith("mailto:") else https_origin(text)
    return _VAPID_SUB_FALLBACK


def claims_for(endpoint: str, now=None) -> dict:
    """aud — origin endpoint'а, exp не дальше 12 часов (лимит Apple — 24), sub без localhost."""
    now = int(time.time() if now is None else now)
    parts = urlparse(endpoint or "")
    aud = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return {"sub": vapid_subject(), "aud": aud, "exp": now + _EXP_SECONDS}


def _endpoint_ok(url: str) -> bool:
    if not url or len(url) > 2000:
        return False
    if url.startswith("https://"):
        return True
    return url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")


def save_subscription(payload: dict, origin: str = "") -> dict:
    endpoint = str((payload or {}).get("endpoint") or "").strip()
    keys = (payload or {}).get("keys") or {}
    if not isinstance(keys, dict):
        keys = {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not _endpoint_ok(endpoint) or not p256dh or not auth:
        return {"ok": False, "error": "Некорректная подписка."}
    if len(p256dh) > 200 or len(auth) > 200:
        return {"ok": False, "error": "Некорректная подписка."}
    with _lock:
        st = ensure()
        if not st.get("private_key"):
            return {"ok": False, "error": "Ключ пуша недоступен."}
        subs = [s for s in (st.get("subscriptions") or []) if s.get("endpoint") != endpoint]
        subs.append({"endpoint": endpoint, "p256dh": p256dh, "auth": auth})
        st["subscriptions"] = subs[-_MAX_SUBS:]
        _save(st)
        n = len(st["subscriptions"])
    remember_origin(origin)
    _log(f"подписка сохранена, всего {n}")
    return {"ok": True, "subscriptions": n}


def drop_subscription(endpoint: str) -> dict:
    endpoint = str(endpoint or "").strip()
    with _lock:
        st = _load()
        if not isinstance(st, dict):
            return {"ok": True, "subscriptions": 0}
        subs = [s for s in (st.get("subscriptions") or []) if s.get("endpoint") != endpoint]
        st["subscriptions"] = subs
        if st.get("private_key"):
            _save(st)
        n = len(subs)
    return {"ok": True, "subscriptions": n}


def _payload(title: str, body: str, tag: str) -> str:
    title = (title or "Джарвис")[:180]
    body = body or ""
    tag = (tag or "")[:80]

    def dump(text: str) -> str:
        return json.dumps({"title": title, "body": text, "tag": tag}, ensure_ascii=False, separators=(",", ":"))

    if len(dump(body).encode()) <= _MAX_PAYLOAD:
        return dump(body)
    lo, hi, best = 0, len(body), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = body[:mid].rstrip() + "…"
        if len(dump(cand).encode()) <= _MAX_PAYLOAD:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return dump(best or "…")


def set_deliver(fn):
    """Подмена отправки в тестах. None — снова pywebpush."""
    global _deliver_impl
    _deliver_impl = fn


def _deliver(sub: dict, data: str, private_key: str) -> None:
    if _deliver_impl is not None:
        _deliver_impl(sub, data, private_key)
        return
    from pywebpush import webpush
    webpush(
        subscription_info={
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        },
        data=data,
        vapid_private_key=private_key,
        vapid_claims=claims_for(sub.get("endpoint") or ""),
        ttl=120,
        timeout=15,
        verbose=False,
    )


def _error_kind(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = response.text or ""
        except Exception:
            body = ""
    text = f"{code or ''} {exc} {body}"
    if code in (404, 410) or " 404" in text or " 410" in text:
        return "gone"
    if "BadJwtToken" in text or (code == 403 and "Jwt" in text):
        return "bad_jwt"
    return "send_failed"


def _log_send_error(kind: str, exc: Exception) -> None:
    """403 BadJwtToken — одна строка на процесс. Остальные ошибки тоже не тащат ключ в лог."""
    global _jwt_logged
    if kind == "bad_jwt":
        if _jwt_logged:
            return
        _jwt_logged = True
        _log("отправка не удалась: BadJwtToken")
        return
    _log("отправка не удалась: " + redact(str(exc))[:240])


def send_text(title: str, body: str, tag: str = "") -> dict:
    """Пуш во все подписки. Тело длиннее лимита Web Push обрезается здесь, не в журнале."""
    st = _load()
    if not isinstance(st, dict):
        st = {}
    subs = list(st.get("subscriptions") or [])
    private_key = st.get("private_key") or ""
    if not subs or not private_key:
        return {
            "ok": False, "sent": 0, "reason": "no_subscriptions",
            "message": "Пуш некуда доставить: подписок нет.",
        }
    data = _payload(title, body, tag)
    sent = 0
    errors = 0
    dead = []
    saw_jwt = False
    for sub in subs:
        try:
            _deliver(sub, data, private_key)
            sent += 1
        except Exception as e:
            errors += 1
            kind = _error_kind(e)
            _log_send_error(kind, e)
            if kind == "gone":
                dead.append(sub.get("endpoint"))
            elif kind == "bad_jwt":
                saw_jwt = True
    if dead:
        with _lock:
            cur = _load()
            if isinstance(cur, dict):
                cur["subscriptions"] = [s for s in (cur.get("subscriptions") or []) if s.get("endpoint") not in dead]
                if cur.get("private_key"):
                    _save(cur)
    if sent:
        return {"ok": True, "sent": sent, "errors": errors}
    if not subscription_count():
        return {"ok": False, "sent": 0, "reason": "no_subscriptions", "errors": errors,
                "message": "Пуш некуда доставить: подписок нет."}
    if saw_jwt:
        return {"ok": False, "sent": 0, "errors": errors, "reason": "bad_jwt",
                "message": "Пуш не дошёл: Apple отклонил подпись (BadJwtToken)."}
    return {"ok": False, "sent": 0, "errors": errors, "reason": "send_failed",
            "message": "Пуш не дошёл."}
