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
from pathlib import Path

STORE_NAME = "jarvis_webpush.json"
_VAPID_SUB = "mailto:jarvis@localhost"
_MAX_SUBS = 12
_MAX_PAYLOAD = 2800
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)
_lock = threading.RLock()
_logs = []
_deliver_impl = None


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        return {"subscriptions": []}
    if not isinstance(data, dict):
        return {"subscriptions": []}
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
        st = _load()
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


def ensure() -> dict:
    """Ключи создаются один раз. Наружу из этой функции приватный ключ не печатается."""
    with _lock:
        st = _load()
        if st.get("private_key") and st.get("public_key"):
            return st
        fresh = _generate()
        st["private_key"] = fresh["private_key"]
        st["public_key"] = fresh["public_key"]
        st.setdefault("subscriptions", [])
        _save(st)
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
    return len(_load().get("subscriptions") or [])


def _endpoint_ok(url: str) -> bool:
    if not url or len(url) > 2000:
        return False
    if url.startswith("https://"):
        return True
    return url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")


def save_subscription(payload: dict) -> dict:
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
        subs = [s for s in (st.get("subscriptions") or []) if s.get("endpoint") != endpoint]
        subs.append({"endpoint": endpoint, "p256dh": p256dh, "auth": auth})
        st["subscriptions"] = subs[-_MAX_SUBS:]
        _save(st)
        n = len(st["subscriptions"])
    _log(f"подписка сохранена, всего {n}")
    return {"ok": True, "subscriptions": n}


def drop_subscription(endpoint: str) -> dict:
    endpoint = str(endpoint or "").strip()
    with _lock:
        st = _load()
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
        vapid_claims={"sub": _VAPID_SUB},
        ttl=120,
        timeout=15,
        verbose=False,
    )


def _gone(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code in (404, 410):
        return True
    text = redact(str(exc))
    return " 404" in text or " 410" in text or text.startswith("404") or text.startswith("410")


def send_text(title: str, body: str, tag: str = "") -> dict:
    """Пуш во все подписки. Тело длиннее лимита Web Push обрезается здесь, не в журнале."""
    st = _load()
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
    for sub in subs:
        try:
            _deliver(sub, data, private_key)
            sent += 1
        except Exception as e:
            errors += 1
            _log("отправка не удалась: " + redact(str(e)))
            if _gone(e):
                dead.append(sub.get("endpoint"))
    if dead:
        with _lock:
            cur = _load()
            cur["subscriptions"] = [s for s in (cur.get("subscriptions") or []) if s.get("endpoint") not in dead]
            if cur.get("private_key"):
                _save(cur)
    if sent:
        return {"ok": True, "sent": sent, "errors": errors}
    if not subscription_count():
        return {"ok": False, "sent": 0, "reason": "no_subscriptions", "errors": errors,
                "message": "Пуш некуда доставить: подписок нет."}
    return {"ok": False, "sent": 0, "errors": errors, "reason": "send_failed",
            "message": "Пуш не доставился."}
