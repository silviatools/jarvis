"""
max_user.py — личный аккаунт MAX (не бот канала).

Вход: телефон → SMS-код → при необходимости пароль 2FA. Сессия лежит в
DATA_DIR/jarvis_max/session.db и копией (base64) в DATA_DIR/jarvis_max_user.json.
Оба файла с правами 600, в бэкап не входят и по HTTP не отдаются.

Пока код не подтверждён, аккаунт не считается подключённым: одного session.db
мало (он появляется ещё до SMS). Состояние входа — один и тот же dict:
фоновый connect и HTTP-опрос не должны получить разные объекты.

Протокол неофициальный (пакет maxapi-python, import pymax). История только
с interactive=True. При лимите частоты клиент не рвём: отдаём последний кэш.
Текст переписки в лог не пишется.
"""

import asyncio
import base64
import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
SECRET_NAME = "jarvis_max_user.json"
WORK_NAME = "jarvis_max"
SESSION_FILE = "session.db"

SMS_WAIT = 10
CONFIRM_WAIT = 20
CALL_TIMEOUT = 25
CLOSE_TIMEOUT = 5
CACHE_TTL = 4
RATE_COOLDOWN = 45
_RATE_MARKERS = ("too.many.requests", "too many requests", "limit reached", "rate limit")
_LONG_SECRET = re.compile(r"(?i)bearer\s+\S+|(?:token|session|password|authorization)\s*[=:]\s*\S+|\b[A-Za-z0-9_\-]{24,}\b")

_lock = threading.RLock()
_loop = None
_client = None
_factory = None
# Один объект на весь процесс. Не переприсваивать и не подменять через clear()+новый dict.
_login = {"gen": 0, "stage": "", "phone": "", "hint": "", "error": "", "code_q": None, "password_q": None, "client": None, "fut": None}
_cache = {}
_cool = {}


class MaxError(Exception):
    def __init__(self, message, rate=False):
        super().__init__(message)
        self.rate = rate


def login_state() -> dict:
    """Тот же dict, в который пишет фоновый вход. Для проверки, что объект не подменили."""
    return _login


def set_client_factory(factory) -> None:
    """Тесты подставляют клиент вместо pymax. factory(phone, work_dir, code_provider, password_provider)."""
    global _factory
    _factory = factory


def library_ok() -> bool:
    if _factory is not None:
        return True
    try:
        import pymax  # noqa: F401
        return True
    except Exception:
        return False


def redact(value) -> str:
    return _LONG_SECRET.sub("…", str(value or ""))[:300]


def _log(message: str) -> None:
    print("max: " + redact(message), flush=True)


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or Path(__file__).parent)


def _secret_path() -> Path:
    return _data_dir() / SECRET_NAME


def _work_dir() -> Path:
    return _data_dir() / WORK_NAME


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def is_private_file(path: Path) -> bool:
    """Сессия и её blob не раздаются как статика."""
    name = path.name
    if name == SECRET_NAME or name.startswith(SECRET_NAME):
        return True
    return WORK_NAME in path.parts


def _load() -> dict:
    try:
        data = json.loads(_secret_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save(meta: dict) -> None:
    """На диск попадает только то, без чего не войти снова. Пароль и SMS-код — нет."""
    keep = {
        "phone": str(meta.get("phone") or ""),
        "user_id": meta.get("user_id"),
        "name": str(meta.get("name") or "")[:80],
        "stage": "done" if meta.get("stage") == "done" else "",
        "session": str(meta.get("session") or ""),
    }
    path = _secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    _chmod(tmp, 0o600)
    os.replace(tmp, path)
    _chmod(path, 0o600)


def _account_ready(meta: dict) -> bool:
    return meta.get("stage") == "done" and bool(meta.get("user_id")) and bool(meta.get("session"))


def connected() -> bool:
    """Подключён только завершённый вход с сохранённым blob. Файл session.db сам по себе не считается."""
    if _login.get("stage") in ("starting", "code", "password"):
        return False
    return _account_ready(_load())


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return ""
    return "+" + digits[:1] + "***" + digits[-2:]


def _phone(raw: str) -> str:
    s = re.sub(r"[\s\-()]", "", raw or "")
    if re.fullmatch(r"8\d{10}", s):
        s = "+7" + s[1:]
    if not s.startswith("+"):
        s = "+" + s
    if not re.fullmatch(r"\+\d{10,15}", s):
        raise MaxError("Номер телефона в формате +79991234567.")
    return s


def _ensure_loop():
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="max-user-loop").start()
    return _loop


def _run(coro, timeout: float):
    fut = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    try:
        return fut.result(timeout)
    except MaxError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        fut.cancel()
        raise MaxError("MAX не ответил вовремя.")
    except Exception as e:
        raise MaxError(redact(e), rate=_is_rate(e)) from None


def _is_rate(exc) -> bool:
    text = str(exc or "").lower()
    return any(mark in text for mark in _RATE_MARKERS)


def _make_client(phone: str, work: Path, code_provider, password_provider):
    if _factory is not None:
        return _factory(phone, work, code_provider, password_provider)
    try:
        from pymax import Client, SmsAuthFlow
        from pymax.config import ExtraConfig
    except Exception as e:
        raise MaxError("Библиотека maxapi-python не установлена.") from e
    return Client(phone=phone, session_name=SESSION_FILE, work_dir=str(work),
                  extra_config=ExtraConfig(log_level="WARNING"),
                  auth_flow=SmsAuthFlow(code_provider, password_provider))


def _prepare_work(reset_incomplete: bool) -> Path:
    work = _work_dir()
    work.mkdir(parents=True, exist_ok=True)
    _chmod(work, 0o700)
    db = work / SESSION_FILE
    if reset_incomplete and not _account_ready(_load()) and db.exists():
        try:
            db.unlink()
        except OSError:
            pass
    return work


def _restore_blob(meta: dict) -> None:
    blob = meta.get("session") or ""
    if not blob:
        return
    try:
        raw = base64.b64decode(blob)
    except Exception as e:
        raise MaxError("Сохранённая сессия MAX повреждена — войдите заново.") from e
    work = _prepare_work(reset_incomplete=False)
    db = work / SESSION_FILE
    if not db.exists() or db.read_bytes() != raw:
        db.write_bytes(raw)
    _chmod(db, 0o600)


def _persist(client, phone: str) -> dict:
    db = _work_dir() / SESSION_FILE
    if not db.is_file():
        raise MaxError("MAX не сохранил сессию.")
    uid = _user_id(getattr(client, "me", None))
    if not uid:
        raise MaxError("MAX не подтвердил вход.")
    name = _person_name(getattr(client, "me", None))
    meta = {"phone": phone, "user_id": uid, "name": name, "stage": "done",
            "session": base64.b64encode(db.read_bytes()).decode("ascii")}
    _save(meta)
    _chmod(db, 0o600)
    _log(f"session saved user={uid}")
    return meta


class _CodeProvider:
    def __init__(self, gen: int):
        self.gen = gen

    async def get_code(self, phone):
        _login["stage"] = "code"
        _login["phone"] = str(phone or _login.get("phone") or "")
        q = _login.get("code_q")
        if q is None:
            raise MaxError("Очередь кода не готова.")
        code = await q.get()
        if _login.get("gen") != self.gen:
            return ""
        return str(code or "")


class _PasswordProvider:
    def __init__(self, gen: int):
        self.gen = gen

    async def get_password(self, hint):
        _login["stage"] = "password"
        _login["hint"] = str(hint or "")[:80]
        q = _login.get("password_q")
        if q is None:
            raise MaxError("Очередь пароля не готова.")
        password = await q.get()
        if _login.get("gen") != self.gen:
            return ""
        return str(password or "")


class _RejectCode:
    async def get_code(self, phone):
        raise MaxError("Сессия MAX недействительна — войдите заново в Настройки → MAX.")


class _RejectPassword:
    async def get_password(self, hint):
        raise MaxError("Сессия MAX недействительна — войдите заново в Настройки → MAX.")


async def _auth_task(phone: str, gen: int):
    work = _prepare_work(reset_incomplete=True)
    _login["code_q"] = asyncio.Queue()
    _login["password_q"] = asyncio.Queue()
    client = _make_client(phone, work, _CodeProvider(gen), _PasswordProvider(gen))
    _login["client"] = client
    try:
        await client.connect()
        if _login.get("gen") != gen:
            return
        if not getattr(client, "is_connected", False):
            raise MaxError("MAX не подтвердил вход.")
        _persist(client, phone)
        global _client
        _client = client
        _login["stage"] = "done"
        _login["error"] = ""
    except asyncio.CancelledError:
        if _login.get("gen") == gen:
            _login["stage"] = "error"
            _login["error"] = "Вход отменён."
        raise
    except Exception as e:
        if _login.get("gen") != gen:
            return
        _login["stage"] = "error"
        _login["error"] = redact(e) or "Не удалось войти в MAX."
        _log("auth failed " + type(e).__name__)
        try:
            await asyncio.wait_for(client.close(), CLOSE_TIMEOUT)
        except Exception:
            pass


def _wait_stage(ok: set, seconds: float) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        stage = _login.get("stage") or ""
        if stage in ok or stage == "error":
            return stage
        time.sleep(0.05)
    return _login.get("stage") or ""


def _put(queue, value: str) -> None:
    if queue is None:
        raise MaxError("Вход ещё не готов.")
    asyncio.run_coroutine_threadsafe(queue.put(value), _ensure_loop()).result(5)


def auth_start(phone: str) -> dict:
    if not library_ok():
        raise MaxError("Библиотека maxapi-python не установлена.")
    phone = _phone(phone)
    _login["gen"] = int(_login.get("gen") or 0) + 1
    gen = _login["gen"]
    _login["stage"] = "starting"
    _login["phone"] = phone
    _login["hint"] = ""
    _login["error"] = ""
    _login["client"] = None
    fut = asyncio.run_coroutine_threadsafe(_auth_task(phone, gen), _ensure_loop())
    _login["fut"] = fut
    stage = _wait_stage({"code", "password", "done"}, SMS_WAIT)
    if stage == "error":
        raise MaxError(_login.get("error") or "Не удалось запросить код.")
    if stage not in ("code", "password", "done"):
        raise MaxError("MAX не запросил код. Повторите вход.")
    _log("auth stage=" + stage)
    return {"ok": True, "step": "" if stage == "done" else stage, "me": status().get("me") or {}}


def auth_code(code: str) -> dict:
    if _login.get("stage") != "code":
        raise MaxError("Сначала запросите код.")
    code = re.sub(r"\D", "", code or "")
    if not code:
        raise MaxError("Введите код из SMS.")
    _put(_login.get("code_q"), code)
    stage = _wait_stage({"password", "done"}, CONFIRM_WAIT)
    if stage == "error":
        raise MaxError(_login.get("error") or "Неверный код.")
    if stage == "password":
        return {"ok": True, "step": "password"}
    if stage != "done":
        raise MaxError("MAX не подтвердил код.")
    return {"ok": True, "step": "", "me": status().get("me") or {}}


def auth_password(password: str) -> dict:
    if _login.get("stage") != "password":
        raise MaxError("Сначала введите код из SMS.")
    if not str(password or ""):
        raise MaxError("Введите пароль двухфакторной защиты.")
    _put(_login.get("password_q"), password)
    stage = _wait_stage({"done"}, CONFIRM_WAIT)
    if stage == "error":
        raise MaxError(_login.get("error") or "Неверный пароль.")
    if stage != "done":
        raise MaxError("MAX не подтвердил пароль.")
    return {"ok": True, "step": "", "me": status().get("me") or {}}


def logout() -> dict:
    global _client
    client = _client
    _client = None
    _cache.clear()
    _cool.clear()
    if client is not None:
        try:
            _run(client.close(), CLOSE_TIMEOUT)
        except MaxError:
            pass
    _login["gen"] = int(_login.get("gen") or 0) + 1
    _login["stage"] = ""
    _login["error"] = ""
    _login["client"] = None
    work = _work_dir()
    db = work / SESSION_FILE
    if db.exists():
        try:
            db.unlink()
        except OSError:
            pass
    path = _secret_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    return {"ok": True}


def status() -> dict:
    meta = _load()
    step = _login.get("stage") if _login.get("stage") in ("code", "password") else ""
    out = {"library": library_ok(), "connected": False, "step": step, "phone": mask_phone(meta.get("phone") or ("" if step else ""))}
    if step:
        out["phone"] = mask_phone(_login.get("phone") or "")
        return out
    if not _account_ready(meta):
        if _login.get("error"):
            out["error"] = _login["error"]
        return out
    out["connected"] = True
    out["me"] = {"id": meta.get("user_id"), "name": meta.get("name") or ""}
    out["phone"] = mask_phone(meta.get("phone") or "")
    return out


def epoch_seconds(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if n > 10 ** 11:
        n = n / 1000.0
    return n


def _fmt(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(MSK).strftime("%Y-%m-%d %H:%M")


def _g(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _user_id(user):
    if user is None:
        return None
    contact = _g(user, "contact")
    if contact is not None and _g(contact, "id") is not None:
        return int(_g(contact, "id"))
    if _g(user, "id") is not None:
        return int(_g(user, "id"))
    return None


def _person_name(user) -> str:
    if user is None:
        return ""
    contact = _g(user, "contact") or user
    names = _g(contact, "names") or []
    for item in names:
        full = str(_g(item, "name") or "").strip()
        if full:
            return full
        parts = [str(_g(item, "first_name") or "").strip(), str(_g(item, "last_name") or "").strip()]
        joined = " ".join(p for p in parts if p)
        if joined:
            return joined
    uid = _user_id(contact)
    return str(uid or "")


def _kind(chat) -> str:
    raw = _g(chat, "type", "") or ""
    raw = _g(raw, "value", raw)
    raw = str(raw).split(".")[-1].upper()
    if raw in ("DIALOG", "USER", "PRIVATE"):
        return "user"
    if raw in ("CHANNEL", "BROADCAST"):
        return "channel"
    return "group"


def _muted(chat) -> bool:
    raw = _g(chat, "mute")
    if raw is None:
        opts = _g(chat, "options")
        if isinstance(opts, dict):
            raw = opts.get("mute", opts.get("muted"))
    if raw in (None, 0, False):
        return False
    if raw in (-1, True):
        return True
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return False
    if n < 0:
        return True
    if n > 10 ** 11:
        n /= 1000.0
    return n > time.time()


def _counterpart(chat, me_id):
    parts = _g(chat, "participants") or {}
    ids = []
    if isinstance(parts, dict):
        for key in parts:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
    elif isinstance(parts, (list, tuple)):
        for item in parts:
            try:
                ids.append(int(_g(item, "id", item)))
            except (TypeError, ValueError):
                continue
    for uid in ids:
        if me_id is None or uid != int(me_id):
            return uid
    return None


def _reply_id(link):
    if not link:
        return None
    mid = _g(link, "message_id")
    if mid is None:
        message = _g(link, "message")
        mid = _g(message, "id") if message is not None else None
    try:
        return int(mid) if mid is not None else None
    except (TypeError, ValueError):
        return None


def _media(msg) -> dict:
    attaches = _g(msg, "attaches") or []
    if not attaches:
        return {}
    first = attaches[0]
    raw = _g(first, "type", "") or _g(first, "_type", "") or ""
    raw = _g(raw, "value", raw)
    kind = str(raw).split(".")[-1].lower() or "file"
    return {"media": kind}


def _mentioned(msg, me_id) -> bool:
    if not me_id:
        return False
    for el in _g(msg, "elements") or []:
        et = str(_g(el, "type") or "").lower()
        eid = _g(el, "entity_id", _g(el, "user_id"))
        try:
            if "mention" in et and eid is not None and int(eid) == int(me_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _normalize_message(msg, me_id, names: dict) -> dict:
    text = str(_g(msg, "text") or "")
    sender = _g(msg, "sender")
    try:
        sender = int(sender) if sender is not None else None
    except (TypeError, ValueError):
        sender = None
    ts = epoch_seconds(_g(msg, "time"))
    mine = sender is not None and me_id is not None and sender == int(me_id)
    who = "я" if mine else (names.get(sender) or (str(sender) if sender else ""))
    out = {
        "id": _g(msg, "id"),
        "date": _fmt(ts),
        "ts": ts,
        "from": who,
        "from_id": sender,
        "text": text,
        "mine": mine,
    }
    out.update(_media(msg))
    reply = _reply_id(_g(msg, "link"))
    if reply:
        out["reply_to"] = reply
    url = _g(msg, "url") or _g(msg, "link_url")
    if isinstance(url, str) and url.startswith("http"):
        out["link"] = url
    if _mentioned(msg, me_id):
        out["mentioned"] = True
    return out


async def _name_map(client, chats, me_id) -> dict:
    need = []
    for chat in chats:
        if _kind(chat) != "user":
            continue
        if str(_g(chat, "title") or "").strip():
            continue
        other = _counterpart(chat, me_id)
        if other:
            need.append(other)
    names = {}
    if need and hasattr(client, "get_users"):
        got = await client.get_users(list(dict.fromkeys(need)))
        for user in got or []:
            uid = _user_id(user)
            if uid:
                names[uid] = _person_name(user)
    return names


def _chat_row(chat, me_id, names: dict) -> dict:
    kind = _kind(chat)
    title = str(_g(chat, "title") or "").strip()
    other = _counterpart(chat, me_id) if kind == "user" else None
    if not title and kind == "user":
        title = names.get(other) or (str(other) if other else "")
    cid = int(_g(chat, "id"))
    if not title:
        title = str(cid)
    last = _g(chat, "last_message")
    preview = str(_g(last, "text") or "").replace("\n", " ")[:120] if last else ""
    return {
        "id": cid,
        "title": title,
        "type": kind,
        "unread": int(_g(chat, "new_messages") or 0),
        "last_message": preview,
        "last_ts": epoch_seconds(_g(chat, "last_event_time")),
        "muted": _muted(chat),
    }


def _folder_rows(raw) -> list:
    if isinstance(raw, list):
        rows = raw
    else:
        rows = _g(raw, "folders") or []
    out = []
    for folder in rows:
        include = []
        for item in _g(folder, "include") or []:
            try:
                include.append(int(item))
            except (TypeError, ValueError):
                continue
        out.append({"id": str(_g(folder, "id") or ""), "title": str(_g(folder, "title") or ""), "include": include})
    return out


async def _open_client():
    global _client
    meta = _load()
    if not _account_ready(meta):
        raise MaxError("Личный MAX не подключён: Настройки → MAX.")
    if _client is not None and getattr(_client, "is_connected", False):
        return _client
    _restore_blob(meta)
    client = _make_client(str(meta.get("phone") or ""), _work_dir(), _RejectCode(), _RejectPassword())
    await client.connect()
    if not getattr(client, "is_connected", False):
        raise MaxError("Сессия MAX недействительна — войдите заново в Настройки → MAX.")
    _client = client
    return client


def _close_client() -> None:
    global _client
    client = _client
    _client = None
    if client is None:
        return
    try:
        _run(client.close(), CLOSE_TIMEOUT)
    except MaxError:
        pass


def _guard(key: str, producer):
    with _lock:
        now = time.time()
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return copy.deepcopy(hit[1])
        if _cool.get(key, 0) > now:
            if hit:
                data = copy.deepcopy(hit[1])
                if isinstance(data, dict):
                    data["stale"] = True
                return data
            raise MaxError("MAX временно ограничил запросы к этому чату.")
        try:
            data = producer()
        except MaxError as e:
            if e.rate or _is_rate(e):
                _cool[key] = now + RATE_COOLDOWN
                _log(f"rate limit resource={key}")
                if hit:
                    cached = copy.deepcopy(hit[1])
                    if isinstance(cached, dict):
                        cached["stale"] = True
                    return cached
                raise MaxError("MAX временно ограничил запросы к этому чату.")
            _close_client()
            raise
        _cache[key] = (now, copy.deepcopy(data))
        return copy.deepcopy(data)


def snapshot() -> dict:
    async def _go():
        client = await _open_client()
        chats = list(await client.fetch_chats() or [])
        folders = _folder_rows(await client.get_folders())
        me = getattr(client, "me", None)
        me_id = _user_id(me)
        names = await _name_map(client, chats, me_id)
        _log(f"snapshot chats={len(chats)} folders={len(folders)}")
        return {
            "me": {"id": me_id, "name": _person_name(me)},
            "chats": [_chat_row(chat, me_id, names) for chat in chats],
            "folders": folders,
        }

    return _guard("snapshot", lambda: _run(_go(), CALL_TIMEOUT))


def history(chat_id, backward: int = 40, from_ms: int = None) -> dict:
    """История только interactive=True, backward в диапазоне 1…200."""
    n = max(1, min(int(backward or 40), 200))
    chat_id = int(chat_id)

    async def _go():
        client = await _open_client()
        me_id = _user_id(getattr(client, "me", None))
        kw = {"backward": n, "interactive": True}
        if from_ms:
            # В pymax аргумент называется from_ и это миллисекунды.
            kw["from_"] = int(from_ms)
        rows = await client.fetch_history(chat_id, **kw) or []
        names = {}
        sender_ids = []
        for msg in rows:
            sender = _g(msg, "sender")
            try:
                if sender is not None:
                    sender_ids.append(int(sender))
            except (TypeError, ValueError):
                pass
        missing = [i for i in dict.fromkeys(sender_ids) if i != me_id]
        if missing and hasattr(client, "get_users"):
            for user in await client.get_users(missing) or []:
                uid = _user_id(user)
                if uid:
                    names[uid] = _person_name(user)
        _log(f"history chat={chat_id} n={len(rows)}")
        return {"messages": [_normalize_message(msg, me_id, names) for msg in rows]}

    return _guard(f"history:{chat_id}:{n}:{int(from_ms or 0)}", lambda: _run(_go(), CALL_TIMEOUT))
