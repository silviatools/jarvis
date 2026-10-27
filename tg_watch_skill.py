"""
tg_watch_skill.py — навык «Временные пуши Telegram».

Пользователь просит следить за диалогом, группой или каналом личного аккаунта
(Telethon, не бот) ограниченное время. Новые входящие от других людей уходят
Web Push в приложение Джарвис. Уведомления самого Telegram не читаются и не меняются.
Свои исходящие не дублируются. Нет подписки на пуш — слежка не включается,
ассистент говорит, что сообщение некуда доставить.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import webpush

SKILL_ID = "tg_watch"
STORE_NAME = "jarvis_tg_watch.json"
DEFAULT_MINUTES = 30
MAX_MINUTES = 10080
# Три попытки. Между ними пауза, а не новый пуш каждые несколько секунд.
MAX_PUSH_TRIES = 3
RETRY_DELAYS = (30, 120)
_lock = threading.Lock()
_logs = []

SKILLS = [
    {"id": SKILL_ID, "title": "Временные пуши Telegram", "risk": "read", "default": True,
     "desc": (
         "По просьбе включает слежку за названным диалогом, группой или каналом личного Telegram на срок, "
         "который вы сказали (если не сказали — 30 минут). Пока окно открыто, новые входящие от других людей "
         "приходят пушем в веб-приложение Джарвис: заголовок — имя диалога, текст — целиком. "
         "Пуш только в Джарвис, уведомления самого Telegram не включаются и не меняются. "
         "На iPhone сайт должен быть на экране «Домой» (iOS 16.4+) и уведомления разрешены в Настройки → Общее. "
         "Свои исходящие сообщения не дублируются. Несколько диалогов можно держать сразу. "
         "«Выключи» останавливает слежку по этому диалогу сразу, по истечении срока она кончается сама. "
         "Если пуш не разрешён или подписок нет, ассистент прямо говорит, что сообщение некуда доставить. "
         "Если имя неточное («менеджер», «Света»), никого не нашёл или подходит несколько — не включает и коротко "
         "переспрашивает: список из имени чата и типа (личный, группа, канал). После вашего выбора или фразы "
         "«это Светлана» запоминает псевдоним и в следующий раз открывает этот диалог по id, а не по имени. "
         "Псевдонимы можно посмотреть, переименовать и удалить."
     ),
     "tools": [
         "tg_watch_start", "tg_watch_stop", "tg_watch_list",
         "tg_watch_alias_list", "tg_watch_alias_save", "tg_watch_alias_delete", "tg_watch_alias_rename",
     ]},
]

_STR = {"type": "string"}
TOOLS = [
    {"name": "tg_watch_start",
     "description": (
         "Включить временную слежку за диалогом, группой или каналом личного Telegram. "
         "name — имя так, как сказал пользователь. minutes — срок в минутах; не передавай, если срок не назван (будет 30). "
         "Несколько диалогов — несколько вызовов. Если ответ с ask:true (имя размыто, никого нет или несколько) — "
         "не включай и не выбирай сам: коротко переспроси и перечисли matches как «имя — личный/группа/канал». "
         "Когда пользователь подтвердил вариант, вызови снова с тем же name и peer_id выбранного диалога: "
         "так запоминается псевдоним, следующий раз это имя включит диалог по id. "
         "Не меняет уведомления Telegram. Если deliverable:false или сказано, что некуда доставить — так и скажи пользователю "
         "и не говори, что слежка включена. Свои исходящие в пуш не попадают."
     ),
     "input_schema": {"type": "object", "properties": {
         "name": {**_STR, "description": "Имя или псевдоним диалога, группы или канала — как сказал пользователь"},
         "minutes": {"type": "number", "description": "Срок в минутах. Пусто — 30."},
         "peer_id": {"type": "number", "description": "Id диалога, который пользователь выбрал из списка. Пусто, пока он не подтвердил."},
     }, "required": ["name"]}},
    {"name": "tg_watch_stop",
     "description": (
         "Сразу остановить слежку. name — диалог, который назвал пользователь. "
         "Без name, если активен один диалог, останавливает его. Если активных несколько и имя не названо — не останавливай все, перечисли."
     ),
     "input_schema": {"type": "object", "properties": {
         "name": {**_STR, "description": "Имя диалога. Можно не передавать, если слежка одна."},
     }, "required": []}},
    {"name": "tg_watch_list",
     "description": "Список диалогов, за которыми слежка ещё идёт, и сколько минут осталось.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "tg_watch_alias_list",
     "description": "Показать запомненные псевдонимы: как пользователь называет диалог и какой это чат (имя и тип).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "tg_watch_alias_save",
     "description": (
         "Запомнить псевдоним диалога, не включая слежку. Когда пользователь прямо говорит «это Светлана» или «запомни как». "
         "alias — его слова. peer_id — если он уже выбрал диалог из списка. "
         "Без peer_id ищи по alias; если совпадений нет или несколько — не сохраняй, переспроси списком имя и тип."
     ),
     "input_schema": {"type": "object", "properties": {
         "alias": {**_STR, "description": "Как пользователь называет человека или чат"},
         "peer_id": {"type": "number", "description": "Id диалога, если пользователь его уже указал"},
         "name": {**_STR, "description": "Имя диалога в Telegram, если оно отличается от псевдонима"},
     }, "required": ["alias"]}},
    {"name": "tg_watch_alias_delete",
     "description": "Удалить псевдоним. Слежку за диалогом не останавливает.",
     "input_schema": {"type": "object", "properties": {
         "alias": {**_STR, "description": "Псевдоним, который забыть"},
     }, "required": ["alias"]}},
    {"name": "tg_watch_alias_rename",
     "description": "Переименовать псевдоним. Диалог по id остаётся тем же.",
     "input_schema": {"type": "object", "properties": {
         "alias": {**_STR, "description": "Текущий псевдоним"},
         "new_alias": {**_STR, "description": "Новое имя, как теперь говорить"},
     }, "required": ["alias", "new_alias"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
if _missing:
    raise RuntimeError("tg watch catalog tools missing: " + ", ".join(_missing))

PROMPT = (
    "ВРЕМЕННЫЕ ПУШИ ИЗ ЛИЧНОГО TELEGRAM. Инструменты: tg_watch_start(name, minutes?, peer_id?), "
    "tg_watch_stop(name?), tg_watch_list(), tg_watch_alias_list(), tg_watch_alias_save(alias, peer_id?), "
    "tg_watch_alias_delete(alias), tg_watch_alias_rename(alias, new_alias). "
    "Просьба включить уведомления по диалогу, группе или каналу на время («включи на полчаса уведомления в диалоге со Светланой») — "
    "это слежка через личный аккаунт, а не настройки уведомлений Telegram: их не включай и не меняй. "
    "Пока окно открыто, новые входящие от других людей уходят пушем в приложение Джарвис. Свои исходящие не дублируются. "
    "Срок передай в minutes, как сказал пользователь; не назвал срок — не передавай minutes (будет 30 минут). "
    "Несколько диалогов можно включить одновременно, каждый своим вызовом. "
    "Если имя размыто («менеджер», «Света»), никого не нашли или подходит несколько — инструмент не включает слежку. "
    "Коротко переспроси и дай варианты списком: имя чата и тип (личный, группа, канал). Сам не выбирай. "
    "Пользователь подтвердил вариант — снова tg_watch_start с тем же name, как он назвал диалог, и peer_id выбранного: "
    "псевдоним запоминается, в следующий раз это имя включит тот же диалог по id. "
    "«Это Светлана» или «запомни как» без просьбы включить — tg_watch_alias_save, не start. "
    "Посмотреть, удалить и переименовать псевдоним — отдельные инструменты. "
    "«Выключи» / «останови» по диалогу — сразу tg_watch_stop с этим именем. "
    "Если инструмент сказал, что сообщение некуда доставить — так и скажи: пуш Джарвиса не разрешён или подписки нет, "
    "нужно открыть сайт с экрана «Домой» и разрешить уведомления в Настройки → Общее. Не говори, что слежка включена. "
    "Если пуш не дошёл (BadJwtToken или ошибка отправки) — так и скажи, не говори «готово»: на телефоне сообщения нет. "
    "Тогда на iPhone открыть Джарвис с иконки и нажать «Переподписаться».\n\n"
)

_TYPE_LABEL = {"user": "личный", "bot": "бот", "group": "группа", "channel": "канал"}

_MEDIA = {
    "voice": "Голосовое",
    "photo": "Фото",
    "file": "Файл",
    "video": "Видео",
    "video_note": "Видеосообщение",
    "audio": "Аудио",
    "sticker": "Стикер",
    "gif": "GIF",
    "poll": "Опрос",
    "location": "Геолокация",
    "contact": "Контакт",
}
NO_PUSH = (
    "Сообщение некуда доставить: уведомления Джарвиса не разрешены или подписок нет. "
    "На iPhone добавьте сайт на экран «Домой» и в Настройки → Общее нажмите «Разрешить уведомления». "
    "Слежку не включил."
)


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": prompt_section(state)}


def _log(msg: str) -> None:
    line = "tg-watch: " + webpush.redact(msg)[:400]
    _logs.append(line)
    print(line, flush=True)


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def _read() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    watches = data.get("watches") if isinstance(data.get("watches"), dict) else {}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    return {"watches": watches, "aliases": aliases}


def _write(doc: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "watches": doc.get("watches") or {},
        "aliases": doc.get("aliases") or {},
    }, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _update(watches=None, aliases=None) -> None:
    doc = _read()
    if watches is not None:
        doc["watches"] = watches
    if aliases is not None:
        doc["aliases"] = aliases
    _write(doc)


def _load() -> dict:
    return _read()["watches"]


def _save(watches: dict) -> None:
    _update(watches=watches)


def _aliases() -> dict:
    return _read()["aliases"]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _key(value: str) -> str:
    return re.sub(r"[^\w@]+", " ", _norm(value), flags=re.UNICODE).strip()


def _type_label(kind: str) -> str:
    return _TYPE_LABEL.get(kind or "", "чат")


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minutes(value):
    if value is None or value == "":
        return DEFAULT_MINUTES, ""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None, "Срок нужен в минутах."
    if n < 1 or n > MAX_MINUTES:
        return None, f"Срок нужен в минутах, от 1 до {MAX_MINUTES}."
    return n, ""


def _tokens(value: str) -> list:
    return [t for t in re.split(r"[^\w@]+", _norm(value)) if t]


def _related(query: str, dialog: dict) -> bool:
    """Подстрока, общий префикс слова или уменьшительное («Света» → «Светлана»). Само по себе слежку не включает."""
    q = _norm(query).lstrip("@")
    if not q:
        return False
    title = _norm(dialog.get("title"))
    username = _norm(dialog.get("username")).lstrip("@")
    if q in title or (username and q in username) or (title and title in q):
        return True
    q_tokens = [t for t in _tokens(q) if len(t) >= 3]
    t_tokens = [t for t in _tokens(title) + ([username] if username else []) if len(t) >= 3]
    for qt in q_tokens:
        for tt in t_tokens:
            if tt.startswith(qt) or qt.startswith(tt):
                return True
            if len(qt) >= 4 and tt.startswith(qt[:-1]):
                return True
            if len(tt) >= 4 and qt.startswith(tt[:-1]):
                return True
    return False


def pick_dialog(query: str, dialogs: list, allow_partial: bool = False) -> dict:
    """Точное имя включает. Неточное — нет, пока allow_partial (это остановка уже открытой слежки)."""
    q = _key(query).lstrip("@")
    if not q:
        return {"ok": False, "message": "Не указано имя диалога."}
    exact, partial = [], []
    for d in dialogs or []:
        title = _key(d.get("title"))
        username = _key(d.get("username")).lstrip("@")
        if title == q or (username and username == q):
            exact.append(d)
        elif _related(query, d):
            partial.append(d)
    if len(exact) == 1:
        return {"ok": True, "dialog": exact[0]}
    if len(exact) > 1:
        return {"ok": False, "ambiguous": True, "matches": exact}
    if allow_partial and len(partial) == 1:
        return {"ok": True, "dialog": partial[0]}
    if partial:
        return {"ok": False, "ambiguous": True, "matches": partial}
    return {"ok": False, "message": f"Диалог «{query.strip()}» не найден в личном Telegram."}


def _match_row(d: dict) -> dict:
    kind = d.get("type") or ""
    return {
        "id": d.get("id") if d.get("id") is not None else d.get("peer_id"),
        "title": d.get("title") or "",
        "username": d.get("username") or "",
        "type": kind,
        "type_label": _type_label(kind),
    }


def _option_line(row: dict) -> str:
    name = row.get("title") or row.get("username") or str(row.get("id") or "")
    return f"— {name} — {row.get('type_label') or 'чат'}"


def _ask(query: str, matches, too_many: bool = False) -> dict:
    rows = [_match_row(d) for d in (matches or [])[:8]]
    shown = (query or "").strip()
    if not rows:
        return {
            "ok": False, "ask": True, "matches": [],
            "message": (
                f"Не нашёл «{shown}». Уточните, кого включить: имя чата и это личный диалог, группа или канал."
            ),
        }
    lines = "\n".join(_option_line(r) for r in rows)
    if too_many:
        head = f"Не включаю: по «{shown}» слишком много похожих. Уточните имя."
    elif len(rows) == 1:
        head = f"Не включаю: имя «{shown}» неточное. Это этот диалог?"
    else:
        head = f"Не включаю: по «{shown}» несколько диалогов. Кого включить?"
    return {
        "ok": False, "ask": True, "ambiguous": True, "matches": rows,
        "message": head + "\n" + lines,
    }


def _ambiguous(matches, prefix: str) -> dict:
    rows = [_match_row(d) for d in (matches or [])[:8]]
    names = [r["title"] or r["username"] or str(r["id"]) for r in rows]
    return {
        "ok": False, "ambiguous": True, "matches": rows,
        "message": prefix + " " + "; ".join(names),
    }


def format_push(title: str, msg: dict, chat_type: str = "") -> dict | None:
    """Заголовок — имя диалога. Своё сообщение — None. Текст целиком; медиа без текста — подпись типа."""
    if not msg or msg.get("out"):
        return None
    text = (msg.get("text") or "").strip()
    media = (msg.get("media") or "").strip()
    label = _MEDIA.get(media, "Вложение" if media else "")
    file_name = (msg.get("file_name") or "").strip()
    if media:
        if text:
            body = f"{label} — {text}" if label else text
        elif media == "file" and file_name:
            body = f"Файл: {file_name}"
        else:
            body = label
    else:
        body = text
    if not body:
        return None
    sender = (msg.get("sender") or "").strip()
    if chat_type in ("group", "channel") and sender and sender.casefold() not in ("я", "me"):
        body = f"{sender}: {body}"
    head = (title or "Telegram").strip() or "Telegram"
    return {"title": head[:180], "body": body}


def _public_watch(w: dict, now: float) -> dict:
    left = max(0, int(round((float(w.get("until") or 0) - now) / 60)))
    return {
        "title": w.get("title") or "",
        "type": w.get("type") or "",
        "minutes_left": left,
        "peer_id": w.get("peer_id"),
    }


def _alive(now: float) -> dict:
    return {k: w for k, w in _load().items() if float(w.get("until") or 0) > now}


def list_watches(now=None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        alive = _alive(now)
        _save(alive)
    rows = [_public_watch(w, now) for w in alive.values()]
    rows.sort(key=lambda r: r["title"])
    return {"ok": True, "watches": rows}


def live_search(query: str) -> dict:
    import tg_tools
    res = tg_tools.list_dialogs(query=query, limit=100, include_muted=True) or {}
    dialogs = res.get("dialogs") or []
    matched = (res.get("summary") or {}).get("matched_chats", len(dialogs))
    return {"dialogs": dialogs, "matched": matched}


def live_peer(peer_id: int) -> dict | None:
    """Диалог по id, когда имя в выдаче поиска не совпало буквально («Света» не содержится в «Светлана»)."""
    tg = _tg_user()

    async def _go():
        import tg_tools
        c = await tg._get_client()
        async for d in c.iter_dialogs():
            if int(d.id) != int(peer_id):
                continue
            ent = d.entity
            return {
                "id": int(d.id),
                "title": d.name or "",
                "username": getattr(ent, "username", "") or "",
                "type": tg_tools._kind(ent),
            }
        return None

    return tg._run(_go(), 40)


def live_latest_id(peer_id: int) -> int:
    """Id последнего сообщения, чтобы не прислать историю. Прочитанным чат не помечается."""
    tg = _tg_user()

    async def _go():
        c = await tg._get_client()
        msgs = await c.get_messages(int(peer_id), limit=1)
        if not msgs:
            return 0
        return int(msgs[0].id)

    return int(tg._run(_go(), 30) or 0)


def _tg_user():
    import tg_user
    if tg_user is None or getattr(tg_user, "TelegramClient", None) is None:
        raise RuntimeError("Личный Telegram не подключён: Настройки → Телеграм.")
    return tg_user


def _media_of(m) -> tuple:
    file_name = ""
    if getattr(m, "photo", None):
        return "photo", file_name
    if getattr(m, "voice", None):
        return "voice", file_name
    if getattr(m, "video_note", None):
        return "video_note", file_name
    if getattr(m, "video", None):
        return "video", file_name
    if getattr(m, "sticker", None):
        return "sticker", file_name
    if getattr(m, "gif", None):
        return "gif", file_name
    if getattr(m, "audio", None):
        return "audio", file_name
    if getattr(m, "poll", None):
        return "poll", file_name
    if getattr(m, "document", None):
        return "file", getattr(getattr(m, "file", None), "name", None) or ""
    if getattr(m, "geo", None):
        return "location", file_name
    if getattr(m, "contact", None):
        return "contact", file_name
    return "", file_name


def live_fetch_new(peer_id: int, after_id: int) -> list:
    """Новые сообщения с id больше after_id. Прочитанным не помечает."""
    tg = _tg_user()

    async def _go():
        c = await tg._get_client()
        me = await c.get_me()
        me_id = getattr(me, "id", None)
        out = []
        async for m in c.iter_messages(int(peer_id), min_id=int(after_id or 0), limit=40, reverse=True):
            media, file_name = _media_of(m)
            sender = ""
            try:
                from telethon import utils
                who = getattr(m, "sender", None)
                sender = utils.get_display_name(who) if who else ""
            except Exception:
                sender = ""
            out.append({
                "id": int(m.id),
                "out": bool(getattr(m, "out", False) or (me_id and getattr(m, "sender_id", None) == me_id)),
                "text": m.message or "",
                "media": media,
                "file_name": file_name or "",
                "sender": sender,
            })
        return out

    return tg._run(_go(), 40) or []


def _dialog_from_alias(rec: dict) -> dict:
    return {
        "id": rec.get("peer_id"),
        "title": rec.get("title") or rec.get("alias") or "",
        "username": rec.get("username") or "",
        "type": rec.get("type") or "",
    }


def _alias_record(phrase: str, dialog: dict) -> dict | None:
    shown = (phrase or "").strip()
    if not _key(shown):
        return None
    peer = dialog.get("id")
    if peer is None:
        peer = dialog.get("peer_id")
    if peer is None:
        return None
    return {
        "alias": shown,
        "peer_id": int(peer),
        "title": dialog.get("title") or "",
        "username": dialog.get("username") or "",
        "type": dialog.get("type") or "",
    }


def _arm(dialog: dict, span: int, now: float, alias_phrase: str = "") -> dict:
    peer = dialog.get("id")
    if peer is None:
        peer = dialog.get("peer_id")
    if peer is None:
        return {"ok": False, "message": "Диалог без id."}
    try:
        latest = int(live_latest_id(peer) or 0)
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    title = dialog.get("title") or (alias_phrase or "").strip() or str(peer)
    remembered = ""
    with _lock:
        watches = _alive(now)
        prev = watches.get(str(peer)) or {}
        last_id = max(latest, int(prev.get("last_id") or 0))
        watches[str(peer)] = {
            "peer_id": peer,
            "id": peer,
            "title": title,
            "username": dialog.get("username") or "",
            "type": dialog.get("type") or "",
            "until": now + span * 60,
            "minutes": span,
            "last_id": last_id,
            "started": now,
        }
        aliases = None
        rec = _alias_record(alias_phrase, dialog)
        if rec:
            aliases = dict(_aliases())
            aliases[_key(alias_phrase)] = rec
            remembered = rec["alias"]
        _update(watches=watches, aliases=aliases)
        rows = [_public_watch(w, now) for w in watches.values()]
    message = (
        f"Слежу за «{title}» {span} мин. Новые входящие от других придут пушем в Джарвис. "
        "Свои сообщения не дублирую. Уведомления Telegram не менял."
    )
    if remembered:
        message += f" Запомнил «{remembered}» за этим диалогом."
    return {
        "ok": True,
        "deliverable": True,
        "title": title,
        "type": dialog.get("type") or "",
        "peer_id": peer,
        "minutes": span,
        "alias": remembered,
        "message": message,
        "watches": rows,
    }


def start(query: str, minutes=None, now=None, peer_id=None) -> dict:
    now = time.time() if now is None else now
    span, err = _minutes(minutes)
    if err:
        return {"ok": False, "message": err}
    if webpush.subscription_count() < 1:
        return {"ok": False, "deliverable": False, "message": NO_PUSH}
    phrase = (query or "").strip()
    chosen = _as_int(peer_id)
    if chosen is None and phrase:
        rec = _aliases().get(_key(phrase))
        if isinstance(rec, dict) and rec.get("peer_id") is not None:
            return _arm(_dialog_from_alias(rec), span, now)
    try:
        found = live_search(phrase)
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    dialogs = found.get("dialogs") or []
    matched = int(found.get("matched") or len(dialogs))
    if chosen is not None:
        resolved = _dialog_by_id(phrase, chosen, dialogs)
        if not resolved.get("ok"):
            return resolved
        return _arm(resolved["dialog"], span, now, alias_phrase=phrase)
    if matched > len(dialogs):
        return _ask(phrase, dialogs, too_many=True)
    picked = pick_dialog(phrase, dialogs, allow_partial=False)
    if not picked.get("ok"):
        if picked.get("ambiguous"):
            return _ask(phrase, picked.get("matches"))
        return _ask(phrase, [])
    return _arm(picked["dialog"], span, now)


def _dialog_by_id(phrase: str, peer_id: int, dialogs=None) -> dict:
    """Сначала уже найденные строки, потом пустой поиск, потом обход диалогов по id."""
    pool = list(dialogs or [])
    dialog = next((d for d in pool if _as_int(d.get("id")) == peer_id), None)
    if dialog:
        return {"ok": True, "dialog": dialog}
    try:
        if dialogs is not None:
            broad = live_search("")
            pool = (broad or {}).get("dialogs") or []
            dialog = next((d for d in pool if _as_int(d.get("id")) == peer_id), None)
            if dialog:
                return {"ok": True, "dialog": dialog}
        dialog = live_peer(peer_id)
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    if not dialog:
        return {"ok": False, "ask": True, "message": "Не вижу выбранный диалог. Уточните имя."}
    return {"ok": True, "dialog": dialog}


def _find_dialog(phrase: str, peer_id=None) -> dict:
    chosen = _as_int(peer_id)
    try:
        found = live_search((phrase or "").strip())
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    dialogs = found.get("dialogs") or []
    if chosen is not None:
        return _dialog_by_id(phrase, chosen, dialogs)
    picked = pick_dialog(phrase, dialogs, allow_partial=False)
    if not picked.get("ok"):
        if picked.get("ambiguous"):
            return _ask(phrase, picked.get("matches"))
        return _ask(phrase, [])
    return {"ok": True, "dialog": picked["dialog"]}


def alias_list() -> dict:
    with _lock:
        rows = [dict(r) for r in _aliases().values() if isinstance(r, dict)]
    for row in rows:
        row["type_label"] = _type_label(row.get("type") or "")
    rows.sort(key=lambda r: (r.get("alias") or "").casefold())
    return {"ok": True, "aliases": rows}


def alias_save(alias: str, peer_id=None, dialog_name: str = "") -> dict:
    phrase = (alias or "").strip()
    if not _key(phrase):
        return {"ok": False, "message": "Не сказано, как называть диалог."}
    found = _find_dialog((dialog_name or phrase).strip(), peer_id)
    if not found.get("ok"):
        return found
    dialog = found["dialog"]
    rec = _alias_record(phrase, dialog)
    if not rec:
        return {"ok": False, "message": "Диалог без id."}
    with _lock:
        aliases = dict(_aliases())
        aliases[_key(phrase)] = rec
        _update(aliases=aliases)
    label = _type_label(rec.get("type") or "")
    title = rec.get("title") or str(rec.get("peer_id"))
    return {
        "ok": True,
        "alias": rec,
        "message": f"Запомнил: «{phrase}» — это «{title}», {label}. Слежку не включал.",
    }


def alias_delete(alias: str) -> dict:
    key = _key(alias)
    if not key:
        return {"ok": False, "message": "Не сказано, какой псевдоним удалить."}
    with _lock:
        aliases = dict(_aliases())
        gone = aliases.pop(key, None)
        _update(aliases=aliases)
    if not gone:
        return {"ok": False, "message": f"Псевдонима «{(alias or '').strip()}» нет."}
    return {"ok": True, "message": f"Псевдоним «{gone.get('alias') or alias.strip()}» удалён.", "alias": gone}


def alias_rename(alias: str, new_alias: str) -> dict:
    old = _key(alias)
    phrase = (new_alias or "").strip()
    new = _key(phrase)
    if not old or not new:
        return {"ok": False, "message": "Нужны старое и новое имя псевдонима."}
    with _lock:
        aliases = dict(_aliases())
        rec = aliases.get(old)
        if not rec:
            return {"ok": False, "message": f"Псевдонима «{(alias or '').strip()}» нет."}
        if new != old and new in aliases:
            other = aliases[new]
            busy = other.get("title") or other.get("peer_id")
            return {"ok": False, "message": f"Псевдоним «{phrase}» уже занят диалогом «{busy}»."}
        if new != old:
            aliases.pop(old, None)
        rec = dict(rec)
        rec["alias"] = phrase
        aliases[new] = rec
        _update(aliases=aliases)
    return {"ok": True, "message": f"Теперь это «{phrase}».", "alias": rec}


def stop(query: str = "", now=None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        watches = _alive(now)
        rows = list(watches.values())
        if not rows:
            _save({})
            return {"ok": False, "message": "Активной слежки нет."}
        if not (query or "").strip():
            if len(rows) == 1:
                gone = rows[0]
                _save({})
                return {"ok": True, "message": f"Слежку за «{gone.get('title')}» остановил.", "watches": []}
            _save(watches)
            return _ambiguous(rows, "Слежек несколько, скажите, какую остановить:")
        picked = pick_dialog(query, rows, allow_partial=True)
        if not picked.get("ok"):
            _save(watches)
            if picked.get("ambiguous"):
                return _ambiguous(picked.get("matches"), "Подходят несколько слежек, не останавливаю, пока не уточните:")
            return {"ok": False, "message": picked.get("message") or "Такой слежки нет."}
        peer = str(picked["dialog"].get("peer_id", picked["dialog"].get("id")))
        gone = watches.pop(peer, None)
        _save(watches)
        left = [_public_watch(w, now) for w in watches.values()]
    title = (gone or picked["dialog"]).get("title") or query.strip()
    return {"ok": True, "message": f"Слежку за «{title}» остановил.", "watches": left}


def tell_owner(text: str) -> None:
    """Одно сообщение в чат бота с владельцем: пуш не дошёл. Без токена — молча."""
    try:
        import jarvis
        import tg_bot
        token = jarvis.get_token()
        if not token:
            _log("в чат не написал: нет токена бота")
            return
        subs = jarvis.load_subscribers()
        chats = list(subs.get("chat_ids") or [])
        owners = []
        try:
            import tg_user
            owners = [c for c in chats if tg_user.is_owner_chat(c)]
        except Exception:
            owners = []
        targets = owners or chats
        if not targets:
            _log("в чат не написал: нет чата")
            return
        for cid in targets:
            tg_bot.send_text(token, cid, text)
    except Exception as e:
        _log("в чат не написал: " + webpush.redact(str(e)))


def _fail_notice(title: str, body: str, reason: str) -> str:
    snippet = " ".join((body or "").split())
    if len(snippet) > 140:
        snippet = snippet[:140] + "…"
    why = " Apple отклонил подпись пуша." if reason == "bad_jwt" else ""
    return (
        f"Пуш не дошёл в приложение Джарвис.{why}\n"
        f"Диалог «{title or 'Telegram'}»: {snippet or 'сообщение без текста'}\n"
        "В Telegram оно осталось, на телефон не пришло. "
        "Если после обновления пуши всё ещё молчат — откройте Джарвис с иконки "
        "и нажмите «Переподписаться»."
    )


def _apply_messages(w: dict, msgs: list, send, now: float) -> tuple:
    """Двигает last_id. Свои и пустые пропускает. Сбой — несколько попыток с паузой, потом сообщение в чат."""
    pushes = []
    logged_nowhere = False
    retry = dict(w.get("retry") or {})
    for m in sorted(msgs or [], key=lambda item: int(item.get("id") or 0)):
        mid = int(m.get("id") or 0)
        if m.get("out"):
            if mid > int(w.get("last_id") or 0):
                w["last_id"] = mid
            continue
        payload = format_push(w.get("title") or "", m, w.get("type") or "")
        if not payload:
            if mid > int(w.get("last_id") or 0):
                w["last_id"] = mid
            continue
        if int(retry.get("id") or 0) == mid and float(retry.get("next_at") or 0) > now:
            break
        result = send(payload["title"], payload["body"], tag=f"tg-{w.get('peer_id')}-{mid}") or {}
        if result.get("reason") == "no_subscriptions":
            if mid > int(w.get("last_id") or 0):
                w["last_id"] = mid
            w.pop("retry", None)
            if not logged_nowhere:
                _log("некуда доставить: подписок нет")
                logged_nowhere = True
            pushes.append({"title": payload["title"], "body": payload["body"], "result": result})
            continue
        if not result.get("ok"):
            tries = int(retry.get("tries") or 0) + 1 if int(retry.get("id") or 0) == mid else 1
            reason = result.get("reason") or "send_failed"
            if tries >= MAX_PUSH_TRIES:
                if mid > int(w.get("last_id") or 0):
                    w["last_id"] = mid
                w.pop("retry", None)
                tell_owner(_fail_notice(payload["title"], payload["body"], reason))
                pushes.append({"title": payload["title"], "body": payload["body"], "result": result, "gave_up": True})
                continue
            delay = RETRY_DELAYS[min(tries - 1, len(RETRY_DELAYS) - 1)]
            w["retry"] = {"id": mid, "tries": tries, "next_at": now + delay, "reason": reason}
            pushes.append({"title": payload["title"], "body": payload["body"], "result": result, "retry": True})
            break
        if mid > int(w.get("last_id") or 0):
            w["last_id"] = mid
        w.pop("retry", None)
        pushes.append({"title": payload["title"], "body": payload["body"], "result": result})
    return pushes


def poll(now=None, enabled: bool = True, fetch_new=None, send=None) -> dict:
    """Снимает истёкшие окна. Включённый навык забирает новые входящие и шлёт пуш."""
    now = time.time() if now is None else now
    fetch_new = fetch_new or live_fetch_new
    send = send or webpush.send_text
    with _lock:
        prev = _load()
        expired = [w.get("title") or k for k, w in prev.items() if float(w.get("until") or 0) <= now]
        active = {k: dict(w) for k, w in prev.items() if float(w.get("until") or 0) > now}
        if not enabled:
            _save(active)
            return {"expired": expired, "pushes": [], "active": len(active)}
        snapshot = {k: dict(w) for k, w in active.items()}
    pushes = []
    advanced = {}
    for key, w in snapshot.items():
        try:
            msgs = fetch_new(int(w["peer_id"]), int(w.get("last_id") or 0)) or []
        except Exception as e:
            _log("чтение: " + webpush.redact(str(e)))
            continue
        pushes.extend(_apply_messages(w, msgs, send, now))
        advanced[key] = {"last_id": int(w.get("last_id") or 0), "retry": w.get("retry")}
    with _lock:
        cur = _alive(now)
        for key, upd in advanced.items():
            if key not in cur:
                continue
            cur[key]["last_id"] = max(int(cur[key].get("last_id") or 0), int(upd.get("last_id") or 0))
            if upd.get("retry"):
                cur[key]["retry"] = upd["retry"]
            else:
                cur[key].pop("retry", None)
        _save(cur)
        left = len(cur)
    return {"expired": expired, "pushes": pushes, "active": left}


def execute(name: str, inp: dict, state) -> dict:
    if not skill_on(state):
        return {"error": "Навык выключен в Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    try:
        if name == "tg_watch_start":
            return start(inp.get("name") or inp.get("query") or "", inp.get("minutes"), peer_id=inp.get("peer_id"))
        if name == "tg_watch_stop":
            return stop(inp.get("name") or inp.get("query") or "")
        if name == "tg_watch_list":
            return list_watches()
        if name == "tg_watch_alias_list":
            return alias_list()
        if name == "tg_watch_alias_save":
            alias = (inp.get("alias") or "").strip()
            dialog_name = (inp.get("dialog") or "").strip()
            if not alias:
                alias = (inp.get("name") or "").strip()
            elif inp.get("name"):
                dialog_name = dialog_name or str(inp.get("name"))
            return alias_save(alias, inp.get("peer_id"), dialog_name)
        if name == "tg_watch_alias_delete":
            return alias_delete(inp.get("alias") or inp.get("name") or "")
        if name == "tg_watch_alias_rename":
            return alias_rename(inp.get("alias") or "", inp.get("new_alias") or "")
    except Exception as e:
        return {"error": webpush.redact(str(e))}
    return {"error": "unknown_tool"}
