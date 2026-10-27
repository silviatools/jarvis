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
         "Если пуш не разрешён или подписок нет, ассистент прямо говорит, что сообщение некуда доставить."
     ),
     "tools": ["tg_watch_start", "tg_watch_stop", "tg_watch_list"]},
]

_STR = {"type": "string"}
TOOLS = [
    {"name": "tg_watch_start",
     "description": (
         "Включить временную слежку за диалогом, группой или каналом личного Telegram. "
         "name — имя так, как сказал пользователь. minutes — срок в минутах; не передавай, если срок не назван (будет 30). "
         "Несколько диалогов — несколько вызовов. Если в ответе несколько совпадений, не выбирай сам: перечисли их и спроси. "
         "Не меняет уведомления Telegram. Если deliverable:false или сказано, что некуда доставить — так и скажи пользователю "
         "и не говори, что слежка включена. Свои исходящие в пуш не попадают."
     ),
     "input_schema": {"type": "object", "properties": {
         "name": {**_STR, "description": "Имя диалога, группы или канала"},
         "minutes": {"type": "number", "description": "Срок в минутах. Пусто — 30."},
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
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
if _missing:
    raise RuntimeError("tg watch catalog tools missing: " + ", ".join(_missing))

PROMPT = (
    "ВРЕМЕННЫЕ ПУШИ ИЗ ЛИЧНОГО TELEGRAM. Инструменты: tg_watch_start(name, minutes?), tg_watch_stop(name?), tg_watch_list(). "
    "Просьба включить уведомления по диалогу, группе или каналу на время («включи на полчаса уведомления в диалоге со Светланой») — "
    "это слежка через личный аккаунт, а не настройки уведомлений Telegram: их не включай и не меняй. "
    "Пока окно открыто, новые входящие от других людей уходят пушем в приложение Джарвис. Свои исходящие не дублируются. "
    "Срок передай в minutes, как сказал пользователь; не назвал срок — не передавай minutes (будет 30 минут). "
    "Несколько диалогов можно включить одновременно, каждый своим вызовом. "
    "Если инструмент вернул несколько совпадений — не выбирай сам, перечисли названия и спроси, какой включить. "
    "«Выключи» / «останови» по диалогу — сразу tg_watch_stop с этим именем. "
    "Если инструмент сказал, что сообщение некуда доставить — так и скажи: пуш Джарвиса не разрешён или подписки нет, "
    "нужно открыть сайт с экрана «Домой» и разрешить уведомления в Настройки → Общее. Не говори, что слежка включена.\n\n"
)

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


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    watches = data.get("watches") if isinstance(data, dict) else None
    if not isinstance(watches, dict):
        return {}
    return watches


def _save(watches: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"watches": watches}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


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


def pick_dialog(query: str, dialogs: list) -> dict:
    """Один диалог или отказ, если совпадений нет либо их несколько."""
    q = _norm(query).lstrip("@")
    if not q:
        return {"ok": False, "message": "Не указано имя диалога."}
    exact, partial = [], []
    for d in dialogs or []:
        title = _norm(d.get("title"))
        username = _norm(d.get("username")).lstrip("@")
        if title == q or (username and username == q):
            exact.append(d)
        elif q in title or (username and q in username):
            partial.append(d)
    if len(exact) == 1:
        return {"ok": True, "dialog": exact[0]}
    if len(exact) > 1:
        return {"ok": False, "ambiguous": True, "matches": exact}
    if len(partial) == 1:
        return {"ok": True, "dialog": partial[0]}
    if len(partial) > 1:
        return {"ok": False, "ambiguous": True, "matches": partial}
    return {"ok": False, "message": f"Диалог «{query.strip()}» не найден в личном Telegram."}


def _match_row(d: dict) -> dict:
    return {
        "id": d.get("id"),
        "title": d.get("title") or "",
        "username": d.get("username") or "",
        "type": d.get("type") or "",
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


def start(query: str, minutes=None, now=None) -> dict:
    now = time.time() if now is None else now
    span, err = _minutes(minutes)
    if err:
        return {"ok": False, "message": err}
    if webpush.subscription_count() < 1:
        return {"ok": False, "deliverable": False, "message": NO_PUSH}
    try:
        found = live_search(query)
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    dialogs = found.get("dialogs") or []
    matched = int(found.get("matched") or len(dialogs))
    if matched > len(dialogs):
        return _ambiguous(dialogs, "Слишком много похожих диалогов, уточните имя.")
    picked = pick_dialog(query, dialogs)
    if not picked.get("ok"):
        if picked.get("ambiguous"):
            return _ambiguous(picked.get("matches"), "Подходят несколько диалогов, не включаю, пока не уточните:")
        return {"ok": False, "message": picked.get("message") or "Диалог не найден."}
    dialog = picked["dialog"]
    peer = dialog.get("id")
    if peer is None:
        return {"ok": False, "message": "Диалог без id."}
    try:
        latest = int(live_latest_id(peer) or 0)
    except Exception as e:
        return {"ok": False, "error": webpush.redact(str(e))}
    title = dialog.get("title") or query.strip()
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
        _save(watches)
        rows = [_public_watch(w, now) for w in watches.values()]
    return {
        "ok": True,
        "deliverable": True,
        "title": title,
        "type": dialog.get("type") or "",
        "minutes": span,
        "message": (
            f"Слежу за «{title}» {span} мин. Новые входящие от других придут пушем в Джарвис. "
            "Свои сообщения не дублирую. Уведомления Telegram не менял."
        ),
        "watches": rows,
    }


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
        picked = pick_dialog(query, rows)
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


def _apply_messages(w: dict, msgs: list, send) -> tuple:
    """Двигает last_id. Свои и пустые пропускает. Сбой доставки — стоп, чтобы повторить это сообщение."""
    pushes = []
    logged_nowhere = False
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
        result = send(payload["title"], payload["body"], tag=f"tg-{w.get('peer_id')}-{mid}") or {}
        if result.get("reason") == "no_subscriptions":
            if mid > int(w.get("last_id") or 0):
                w["last_id"] = mid
            if not logged_nowhere:
                _log("некуда доставить: подписок нет")
                logged_nowhere = True
            pushes.append({"title": payload["title"], "body": payload["body"], "result": result})
            continue
        if not result.get("ok"):
            break
        if mid > int(w.get("last_id") or 0):
            w["last_id"] = mid
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
        pushes.extend(_apply_messages(w, msgs, send))
        advanced[key] = int(w.get("last_id") or 0)
    with _lock:
        cur = _alive(now)
        for key, last_id in advanced.items():
            if key in cur:
                cur[key]["last_id"] = max(int(cur[key].get("last_id") or 0), last_id)
        _save(cur)
        left = len(cur)
    return {"expired": expired, "pushes": pushes, "active": left}


def execute(name: str, inp: dict, state) -> dict:
    if not skill_on(state):
        return {"error": "Навык выключен в Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    try:
        if name == "tg_watch_start":
            return start(inp.get("name") or inp.get("query") or "", inp.get("minutes"))
        if name == "tg_watch_stop":
            return stop(inp.get("name") or inp.get("query") or "")
        if name == "tg_watch_list":
            return list_watches()
    except Exception as e:
        return {"error": webpush.redact(str(e))}
    return {"error": "unknown_tool"}
