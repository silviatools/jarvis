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
    _save_cfg({"api_id": _pending["api_id"], "api_hash": _pending["api_hash"], "session": session})
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


# ── Данные ───────────────────────────────────────────────────────────────

def _fmt_date(dt) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M")


def _parse_date(s, end=False):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=MSK)
    except ValueError:
        raise TgError("Дата в формате YYYY-MM-DD.")
    return d + timedelta(days=1) if end else d


def _kind(entity) -> str:
    if isinstance(entity, User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "group"
    return "group"


def _msg_dict(m, me_id) -> dict:
    text = (m.message or "").strip()
    media = ""
    if m.media is not None:
        media = m.media.__class__.__name__.replace("MessageMedia", "").lower() or "media"
    sender = getattr(m, "sender", None)
    out = {
        "id": m.id,
        "date": _fmt_date(m.date),
        "from": "я" if m.out or (me_id and m.sender_id == me_id) else (utils.get_display_name(sender) if sender else ""),
        "text": text[:MAX_TEXT] + ("…" if len(text) > MAX_TEXT else ""),
    }
    if media:
        out["media"] = media
    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
        out["reply_to"] = m.reply_to.reply_to_msg_id
    return out


async def _resolve(client, peer):
    """@username, +телефон, числовой id или часть названия диалога/имени."""
    peer = (peer or "").strip()
    if not peer:
        raise TgError("Не указан собеседник.")
    if peer.lower() in ("me", "self", "saved", "избранное"):
        return await client.get_me()
    if re.match(r"^@?[A-Za-z][A-Za-z0-9_]{3,}$", peer) and (peer.startswith("@") or "_" in peer or peer.isascii()):
        try:
            return await client.get_entity(peer.lstrip("@"))
        except Exception:
            pass
    if re.match(r"^\+?\d{6,15}$", peer):
        try:
            return await client.get_entity(peer if peer.startswith("+") else int(peer))
        except Exception:
            pass
    if re.match(r"^-?\d{5,}$", peer):
        try:
            return await client.get_entity(int(peer))
        except Exception:
            pass
    q = peer.lstrip("@").lower()
    best = None
    async for d in client.iter_dialogs(limit=500):
        name = (d.name or "").lower()
        uname = (getattr(d.entity, "username", "") or "").lower()
        if q == name or q == uname:
            return d.entity
        if best is None and (q in name or (uname and q in uname)):
            best = d.entity
    if best is not None:
        return best
    raise TgError(f"Не нашёл диалог «{peer}». Проверьте @username или название.")


def list_dialogs(query="", limit=30, kind="") -> dict:
    limit = max(1, min(int(limit or 30), 100))
    query = (query or "").strip().lower().lstrip("@")

    async def _go():
        c = await _get_client()
        out = []
        async for d in c.iter_dialogs(limit=500 if query or kind else limit):
            e = d.entity
            k = _kind(e)
            uname = getattr(e, "username", "") or ""
            if kind and k != kind:
                continue
            if query and query not in (d.name or "").lower() and query not in uname.lower():
                continue
            out.append({
                "id": d.id, "title": d.name or "", "username": uname, "type": k,
                "unread": d.unread_count, "last_date": _fmt_date(d.date),
                "last_message": ((d.message.message or "")[:120] if d.message else ""),
            })
            if len(out) >= limit:
                break
        return out

    return {"dialogs": _run(_go(), 60)}


def read_history(peer, limit=30, date_from="", date_to="", offset_id=0) -> dict:
    limit = max(1, min(int(limit or 30), MAX_RESULTS))
    d_from, d_to = _parse_date(date_from), _parse_date(date_to, end=True)

    async def _go():
        c = await _get_client()
        ent = await _resolve(c, peer)
        me = await c.get_me()
        kw = {"limit": limit}
        if d_to:
            kw["offset_date"] = d_to
        if offset_id:
            kw["offset_id"] = int(offset_id)
        msgs = []
        async for m in c.iter_messages(ent, **kw):
            if d_from and m.date < d_from:
                break
            msgs.append(_msg_dict(m, me.id))
        msgs.reverse()
        return {"chat": utils.get_display_name(ent), "messages": msgs}

    return _run(_go(), 60)


def search_messages(query, peer="", limit=20, date_from="", date_to="") -> dict:
    """Поиск по тексту: внутри диалога (peer) или по всем диалогам."""
    query = (query or "").strip()
    if not query:
        raise TgError("Пустой поисковый запрос.")
    limit = max(1, min(int(limit or 20), MAX_RESULTS))
    d_from, d_to = _parse_date(date_from), _parse_date(date_to, end=True)

    async def _go():
        c = await _get_client()
        me = await c.get_me()
        ent = await _resolve(c, peer) if (peer or "").strip() else None
        kw = {"search": query, "limit": limit}
        if d_to:
            kw["offset_date"] = d_to
        out = []
        async for m in c.iter_messages(ent, **kw):
            if d_from and m.date < d_from:
                break
            d = _msg_dict(m, me.id)
            if ent is None:
                chat = await m.get_chat()
                d["chat"] = utils.get_display_name(chat) if chat else ""
            out.append(d)
        return {"chat": utils.get_display_name(ent) if ent else "все диалоги", "query": query, "found": len(out), "messages": out}

    return _run(_go(), 60)


def send_message(peer, text, reply_to=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise TgError("Пустое сообщение.")

    async def _go():
        c = await _get_client()
        ent = await _resolve(c, peer)
        m = await c.send_message(ent, text, reply_to=int(reply_to) if reply_to else None)
        return {"ok": True, "chat": utils.get_display_name(ent), "id": m.id, "date": _fmt_date(m.date)}

    return _run(_go(), 30)


# ── Инструменты ассистентов ──────────────────────────────────────────────

TG_TOOLS = [
    {
        "name": "tg_list_dialogs",
        "description": ("Список диалогов личного Telegram пользователя (чаты, группы, каналы), новые сверху. "
                        "Подтверждения не требует. query — часть имени/@username для поиска диалога."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Часть названия или @username. Пусто — последние диалоги."},
                "limit": {"type": "integer", "description": "Сколько вернуть, по умолчанию 30."},
                "type": {"type": "string", "enum": ["user", "group", "channel", "bot"], "description": "Фильтр по типу."},
            },
        },
    },
    {
        "name": "tg_read_messages",
        "description": ("Прочитать историю сообщений диалога в личном Telegram пользователя (последние N или за период). "
                        "Подтверждения не требует. peer — @username, номер телефона, id или часть названия чата. "
                        "Сообщения возвращаются от старых к новым; «я» — сообщения самого пользователя."),
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "@username / телефон / id / название чата."},
                "limit": {"type": "integer", "description": "Сколько сообщений (до 50), по умолчанию 30."},
                "from": {"type": "string", "description": "Начало периода YYYY-MM-DD."},
                "to": {"type": "string", "description": "Конец периода включительно YYYY-MM-DD."},
                "offset_id": {"type": "integer", "description": "Вернуть сообщения старше этого id (для листания назад)."},
            },
            "required": ["peer"],
        },
    },
    {
        "name": "tg_search_messages",
        "description": ("Найти сообщения по тексту в личном Telegram пользователя: внутри одного диалога (peer) или по "
                        "всем диалогам (peer пуст). Подтверждения не требует. Пример: «найди в диалоге с @ray_of_sun_v "
                        "информацию про зарплату» → peer=\"@ray_of_sun_v\", query=\"зарплата\". Поиск Telegram идёт по "
                        "словам и их формам; если ничего не нашлось — попробуй синонимы/корень слова или прочитай "
                        "историю через tg_read_messages и ответь по ней."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слово или фраза для поиска."},
                "peer": {"type": "string", "description": "@username / телефон / id / название чата; пусто — по всем."},
                "limit": {"type": "integer", "description": "Сколько результатов (до 50), по умолчанию 20."},
                "from": {"type": "string", "description": "Начало периода YYYY-MM-DD."},
                "to": {"type": "string", "description": "Конец периода включительно YYYY-MM-DD."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "tg_send_message",
        "description": ("Отправить сообщение ОТ ИМЕНИ пользователя в его личном Telegram. Это необратимо и видно "
                        "адресату, поэтому ТОЛЬКО после явного согласия: сначала покажи пользователю адресата и точный "
                        "текст, дождись «да/отправляй», и лишь потом вызови с confirmed:true. Без confirmed:true ничего "
                        "не отправится."),
        "input_schema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "@username / телефон / id / название чата."},
                "text": {"type": "string", "description": "Текст сообщения ровно как увидит адресат."},
                "reply_to": {"type": "integer", "description": "id сообщения, на которое отвечаем (необязательно)."},
                "confirmed": {"type": "boolean", "description": "true только после явного согласия пользователя."},
            },
            "required": ["peer", "text"],
        },
    },
]
TG_TOOL_NAMES = {t["name"] for t in TG_TOOLS}


def execute_tg_tool(name: str, inp: dict) -> dict:
    inp = inp or {}
    try:
        if name == "tg_send_message":
            if not inp.get("confirmed"):
                return {"needs_confirmation": True,
                        "message": (f"Нужно подтверждение пользователя, чтобы отправить сообщение "
                                    f"«{inp.get('text', '')}» контакту {inp.get('peer', '')} от его имени.")}
            return send_message(inp.get("peer"), inp.get("text"), inp.get("reply_to"))
        if name == "tg_list_dialogs":
            return list_dialogs(inp.get("query"), inp.get("limit") or 30, inp.get("type") or "")
        if name == "tg_read_messages":
            return read_history(inp.get("peer"), inp.get("limit") or 30, inp.get("from"), inp.get("to"), inp.get("offset_id") or 0)
        if name == "tg_search_messages":
            return search_messages(inp.get("query"), inp.get("peer") or "", inp.get("limit") or 20, inp.get("from"), inp.get("to"))
    except TgError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Telegram: {e}"}
    return {"error": "unknown_tool"}
