"""
tg_user.py — личный Telegram-аккаунт пользователя (не бот) через Telethon.

Вход: api_id + api_hash (my.telegram.org) → телефон → код → пароль 2FA.
Сессия (StringSession) лежит в DATA_DIR/jarvis_tg_user.json с правами 600 и
наружу (в браузер, в /api/data) не отдаётся — только статус.

Telethon асинхронный, а сервер на потоках, поэтому клиент живёт в своём
event loop в отдельном потоке, а снаружи вызывается через _run().
"""

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from telethon import TelegramClient, errors, utils
    from telethon.sessions import StringSession
    from telethon.tl.types import User, Chat, Channel
    from telethon.tl.functions.auth import ResendCodeRequest
except ImportError:  # Telethon не установлен — вкладка покажет понятную ошибку
    TelegramClient = None

MSK = timezone(timedelta(hours=3))
MAX_TEXT = 700          # символов одного сообщения в ответе инструмента
MAX_RESULTS = 50

_lock = threading.RLock()
_loop = None
_client = None
_pending = {}           # {"api_id", "api_hash", "phone", "hash", "client"} между шагами входа
_me_cache = {}


class TgError(Exception):
    pass


def _cfg_file() -> Path:
    base = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
    return base / "jarvis_tg_user.json"


def _load_cfg() -> dict:
    cfg = {}
    try:
        cfg = json.loads(_cfg_file().read_text(encoding="utf-8"))
    except Exception:
        pass
    # Переменные окружения важнее файла — удобно для Railway.
    for env, key in (("TG_API_ID", "api_id"), ("TG_API_HASH", "api_hash"), ("TG_SESSION", "session")):
        v = (os.environ.get(env) or "").strip()
        if v:
            cfg[key] = v
    return cfg


def _save_cfg(cfg: dict) -> None:
    p = _cfg_file()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(p)


def _ensure_loop():
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="tg-user-loop").start()
    return _loop


def _run(coro, timeout: float = 40):
    if TelegramClient is None:
        coro.close()
        raise TgError("Библиотека telethon не установлена (pip install telethon).")
    fut = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    try:
        return fut.result(timeout)
    except TgError:
        raise
    except errors.RPCError as e:
        raise TgError(_rpc_text(e))
    except (asyncio.TimeoutError, TimeoutError):
        fut.cancel()
        raise TgError("Telegram не ответил вовремя.")
    except Exception as e:
        raise TgError(str(e) or e.__class__.__name__)


def _rpc_text(e) -> str:
    name = e.__class__.__name__
    known = {
        "PhoneNumberInvalidError": "Неверный номер телефона (нужен формат +79991234567).",
        "PhoneCodeInvalidError": "Неверный код.",
        "PhoneCodeExpiredError": "Код устарел — запросите новый.",
        "PasswordHashInvalidError": "Неверный пароль двухфакторной защиты.",
        "ApiIdInvalidError": "Неверная пара api_id / api_hash.",
        "FloodWaitError": f"Слишком много попыток, подождите {getattr(e, 'seconds', '?')} сек.",
        "SessionPasswordNeededError": "Нужен пароль двухфакторной защиты.",
    }
    return known.get(name) or f"{name}: {e}"


async def _get_client():
    """Авторизованный клиент или TgError. Подключается лениво и держит соединение."""
    global _client
    cfg = _load_cfg()
    if not (cfg.get("api_id") and cfg.get("api_hash") and cfg.get("session")):
        raise TgError("Личный Telegram не подключён: Настройки → Телеграм.")
    if _client is None:
        _client = TelegramClient(StringSession(cfg["session"]), int(cfg["api_id"]), cfg["api_hash"])
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise TgError("Сессия Telegram недействительна — подключитесь заново в Настройки → Телеграм.")
    return _client


# ── Вход / выход / статус ────────────────────────────────────────────────

def status() -> dict:
    cfg = _load_cfg()
    out = {
        "library": TelegramClient is not None,
        "hasCredentials": bool(cfg.get("api_id") and cfg.get("api_hash")),
        "apiId": str(cfg.get("api_id") or ""),
        "connected": False,
        "step": "code" if _pending.get("hash") else ("password" if _pending.get("need_password") else ""),
    }
    if TelegramClient is None or not cfg.get("session"):
        return out

    async def _me():
        c = await _get_client()
        return await c.get_me()

    try:
        me = _run(_me(), 20)
        out["connected"] = True
        out["me"] = {
            "id": me.id, "name": utils.get_display_name(me),
            "username": me.username or "", "phone": me.phone or "",
        }
    except TgError as e:
        out["error"] = str(e)
    return out


def auth_start(api_id, api_hash, phone) -> dict:
    api_id = str(api_id or "").strip()
    api_hash = (api_hash or "").strip()
    phone = (phone or "").strip()
    if not api_id.isdigit() or not api_hash:
        raise TgError("Укажите api_id (число) и api_hash с my.telegram.org.")
    if not re.match(r"^\+?\d{7,15}$", phone.replace(" ", "").replace("-", "")):
        raise TgError("Номер телефона в формате +79991234567.")
    phone = "+" + phone.lstrip("+").replace(" ", "").replace("-", "")

    async def _go():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        return client, sent

    client, sent = _run(_go(), 30)
    code_hash = sent.phone_code_hash
    old = _pending.get("client")
    if old is not None:
        try:
            _run(old.disconnect(), 10)
        except Exception:
            pass
    _pending.clear()
    _pending.update({"api_id": api_id, "api_hash": api_hash, "phone": phone,
                     "hash": code_hash, "client": client, "at": time.time()})
    return _code_info(sent)


def _code_info(sent) -> dict:
    """Куда Telegram отправил код: app — в приложение (чат «Telegram»), sms, call…"""
    kind = sent.type.__class__.__name__.replace("SentCodeType", "").lower() if sent.type else "?"
    nxt = sent.next_type.__class__.__name__.replace("CodeType", "").lower() if getattr(sent, "next_type", None) else ""
    length = getattr(sent.type, "length", None)
    print(f"[tg-user] код отправлен: тип={kind}, следующий={nxt or '—'}, длина={length}, таймаут={getattr(sent, 'timeout', None)}")
    return {"ok": True, "step": "code", "delivery": kind, "next": nxt, "length": length}


def _qr_svg(url: str) -> str:
    import io
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False, nl=False, dark="#111")
    return buf.getvalue().decode("utf-8")


def auth_qr_start(api_id, api_hash) -> dict:
    """Вход по QR: пользователь сканирует код в Telegram на телефоне
    (Настройки → Устройства → Подключить устройство). Код по SMS/в чат не нужен."""
    api_id = str(api_id or "").strip()
    api_hash = (api_hash or "").strip()
    if not api_id.isdigit() or not api_hash:
        raise TgError("Укажите api_id (число) и api_hash с my.telegram.org.")

    async def _go():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        qr = await client.qr_login()
        return client, qr

    client, qr = _run(_go(), 30)
    old = _pending.get("client")
    if old is not None:
        try:
            _run(old.disconnect(), 10)
        except Exception:
            pass
    _pending.clear()
    _pending.update({"api_id": api_id, "api_hash": api_hash, "client": client, "qr": qr,
                     "qr_state": "wait", "at": time.time()})

    async def _waiter(my_qr):
        # _pending — один общий словарь: работаем, пока в нём наш QR (новый вход его заменит).
        def mine():
            return _pending.get("qr") is my_qr and _pending.get("qr_state") == "wait"

        def set_state(st, err=None):
            if _pending.get("qr") is my_qr:
                _pending["qr_state"] = st
                if err:
                    _pending["qr_error"] = err

        while mine():
            try:
                await my_qr.wait(30)
                set_state("ok")
            except (asyncio.TimeoutError, TimeoutError):
                try:
                    await my_qr.recreate()
                except Exception as e:
                    set_state("error", str(e))
            except errors.SessionPasswordNeededError:
                if _pending.get("qr") is my_qr:
                    _pending["need_password"] = True
                set_state("password")
            except Exception as e:
                set_state("error", str(e))

    asyncio.run_coroutine_threadsafe(_waiter(qr), _ensure_loop())
    return auth_qr_poll()


def auth_qr_poll() -> dict:
    st = _pending.get("qr_state")
    if not _pending.get("client") or not st:
        raise TgError("Сначала запросите QR-код.")
    if st == "ok":
        return _finish_login()
    if st == "password":
        return {"ok": True, "step": "password"}
    if st == "error":
        raise TgError(_pending.get("qr_error") or "Не удалось войти по QR.")
    return {"ok": True, "step": "qr", "svg": _qr_svg(_pending["qr"].url)}


def auth_resend() -> dict:
    """Повторная отправка кода следующим способом (SMS/звонок)."""
    if not _pending.get("client") or not _pending.get("hash"):
        raise TgError("Сначала запросите код.")

    async def _go():
        return await _pending["client"](ResendCodeRequest(_pending["phone"], _pending["hash"]))

    sent = _run(_go(), 30)
    _pending["hash"] = sent.phone_code_hash
    return _code_info(sent)


def _finish_login() -> dict:
    client = _pending["client"]

    async def _me():
        return await client.get_me(), client.session.save()

    me, session = _run(_me(), 20)
    _save_cfg({"api_id": _pending["api_id"], "api_hash": _pending["api_hash"], "session": session, "user_id": me.id})
    global _client
    _client = client
    _pending.clear()
    return {"ok": True, "step": "", "me": {"id": me.id, "name": utils.get_display_name(me), "username": me.username or ""}}


def auth_code(code) -> dict:
    if not _pending.get("client"):
        raise TgError("Сначала запросите код.")
    code = re.sub(r"\D", "", code or "")
    if not code:
        raise TgError("Введите код из Telegram.")

    async def _go():
        try:
            await _pending["client"].sign_in(_pending["phone"], code, phone_code_hash=_pending["hash"])
            return "ok"
        except errors.SessionPasswordNeededError:
            return "password"

    if _run(_go(), 30) == "password":
        _pending["need_password"] = True
        _pending["hash"] = None
        return {"ok": True, "step": "password"}
    return _finish_login()


def auth_password(password) -> dict:
    if not _pending.get("client"):
        raise TgError("Сначала запросите код.")

    async def _go():
        await _pending["client"].sign_in(password=password or "")

    _run(_go(), 30)
    return _finish_login()


def logout() -> dict:
    global _client
    cfg = _load_cfg()
    if cfg.get("session"):
        try:
            async def _out():
                c = await _get_client()
                await c.log_out()
            _run(_out(), 20)
        except TgError:
            pass
    _client = None
    _pending.clear()
    keep = {k: cfg[k] for k in ("api_id", "api_hash") if cfg.get(k)}
    _save_cfg(keep)
    return {"ok": True}


def is_owner_chat(chat_id) -> bool:
    """Личный чат с ботом принадлежит владельцу подключённого аккаунта
    (в личке chat_id бота == id пользователя). Остальным доступ закрыт."""
    cfg = _load_cfg()
    if TelegramClient is None or not cfg.get("session"):
        return False
    uid = cfg.get("user_id")
    if not uid:
        try:
            async def _me():
                return (await (await _get_client()).get_me()).id
            uid = _run(_me(), 20)
            cfg_file = {k: v for k, v in _load_cfg().items() if k in ("api_id", "api_hash", "session")}
            cfg_file["user_id"] = uid
            _save_cfg(cfg_file)
        except Exception:
            return False
    try:
        return int(chat_id) == int(uid)
    except (TypeError, ValueError):
        return False
