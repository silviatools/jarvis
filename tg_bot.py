"""
tg_bot.py — возможности Telegram-бота для текстового ассистента.

Что здесь:
  • форматированные ответы: whitelist HTML-тегов Telegram + безопасный
    fallback в plain text, нарезка длинных сообщений без разрыва тегов;
  • инструменты ассистента (tg_*): чеклисты с кнопками-галочками, файлы и
    медиа, опросы, геопозиция, контакт, кнопки быстрых ответов, реакции,
    закрепление, кубик;
  • обработка нажатий на эти кнопки (callback_query);
  • память диалога с TTL: пока переписка непрерывна — контекст держится,
    после паузы дольше ASSISTANT_CONTEXT_MINUTES (по умолчанию 10) — сбрасывается.
"""

import base64
import html
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:  # сервер без requests живёт, бот — нет
    requests = None

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
STATE_FILE = DATA_DIR / "jarvis_tg_bot_state.json"

TG_MSG_LIMIT = 4096
CHUNK = 3500           # с запасом: считаем сырой HTML, а лимит — по тексту без тегов
MAX_UPLOAD = 45 * 1024 * 1024
MAX_INLINE_B64 = 20 * 1024 * 1024
MAX_CHECKLIST_ITEMS = 60
MAX_CHECKLISTS = 60
MAX_QUICK_SETS = 200


def context_ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("ASSISTANT_CONTEXT_MINUTES", "10"))) * 60
    except ValueError:
        return 600.0


# ── вызовы Bot API ───────────────────────────────────────────────────────────

class TgApiError(Exception):
    pass


def api(token: str, method: str, payload: dict = None, files: dict = None, timeout: int = 30) -> dict:
    """Возвращает result Bot API; при ошибке — TgApiError с описанием."""
    if not requests:
        raise TgApiError("requests не установлен")
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if files:
            data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                    for k, v in (payload or {}).items() if v is not None}
            r = requests.post(url, data=data, files=files, timeout=max(timeout, 120))
        else:
            r = requests.post(url, json=payload or {}, timeout=timeout)
        j = r.json()
    except Exception as e:
        raise TgApiError(f"{method}: {e}")
    if not j.get("ok"):
        raise TgApiError(j.get("description") or f"{method}: HTTP {r.status_code}")
    return j.get("result")


# ── форматирование (HTML для Telegram) ───────────────────────────────────────

_SIMPLE_TAGS = {
    "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u",
    "s": "s", "strike": "s", "del": "s", "code": "code", "pre": "pre",
    "tg-spoiler": "tg-spoiler",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^<>]*?)?)\s*>")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#\d{1,6}|#x[0-9a-fA-F]{1,6});")
_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _escape_text(s: str) -> str:
    """Экранирует < > и одиночные &, оставляя уже готовые сущности."""
    out, pos = [], 0
    for m in _ENTITY_RE.finditer(s):
        out.append(s[pos:m.start()].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        out.append(m.group(0))
        pos = m.end()
    out.append(s[pos:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def _md_to_html(text: str) -> str:
    """Подстраховка: если модель всё же написала markdown — превращаем самое
    частое в HTML-теги. Код-блоки прячем, чтобы не трогать их содержимое."""
    blocks = []

    def stash(html_str):
        blocks.append(html_str)
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```",
                  lambda m: stash("<pre>" + html.escape(m.group(1).strip("\n"), quote=False) + "</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: stash("<code>" + html.escape(m.group(1), quote=False) + "</code>"), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def to_tg_html(text: str) -> str:
    """Любой текст модели → валидный HTML Telegram. Разрешены только теги из
    белого списка; остальное (в т.ч. содержимое <pre>/<code>) экранируется,
    незакрытые теги закрываются, лишние закрывающие — выбрасываются."""
    text = _md_to_html(text or "")
    out, stack, pos = [], [], 0
    for m in _TAG_RE.finditer(text):
        out.append(_escape_text(text[pos:m.start()]))
        pos = m.end()
        closing, name, attrs = bool(m.group(1)), m.group(2).lower(), m.group(3) or ""
        raw = m.group(0)
        in_code = "code" in stack
        in_pre = "pre" in stack

        if closing:
            canon = {"blockquote": "blockquote", "a": "a", "span": "tg-spoiler"}.get(name) or _SIMPLE_TAGS.get(name)
            if canon and canon in stack:
                # закрываем всё, что открыто внутри, затем сам тег
                while stack:
                    t = stack.pop()
                    out.append(f"</{t}>")
                    if t == canon:
                        break
            elif not canon:
                out.append(_escape_text(raw))
            # лишний закрывающий известный тег — молча выбрасываем
            continue

        if in_code:  # внутри <code> тегов не бывает
            out.append(_escape_text(raw))
            continue
        if name == "a":
            hm = _HREF_RE.search(attrs)
            href = (hm.group(1) or hm.group(2) or "").strip() if hm else ""
            if re.match(r"(?i)^(https?://|tg://|mailto:)", href) and "a" not in stack:
                out.append(f'<a href="{html.escape(href, quote=True)}">')
                stack.append("a")
            else:
                out.append(_escape_text(raw))
        elif name == "blockquote":
            if "blockquote" in stack:
                out.append(_escape_text(raw))
            else:
                out.append("<blockquote expandable>" if "expandable" in attrs.lower() else "<blockquote>")
                stack.append("blockquote")
        elif name == "code" and in_pre:
            lm = re.search(r'class\s*=\s*"(language-[\w+-]+)"', attrs)
            out.append(f'<code class="{lm.group(1)}">' if lm else "<code>")
            stack.append("code")
        elif name == "span" and "tg-spoiler" in attrs:
            out.append("<tg-spoiler>")
            stack.append("tg-spoiler")
        elif name in _SIMPLE_TAGS and not in_pre:
            canon = _SIMPLE_TAGS[name]
            out.append(f"<{canon}>")
            stack.append(canon)
        else:
            out.append(_escape_text(raw))
    out.append(_escape_text(text[pos:]))
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


_SPLIT_TOKEN_RE = re.compile(r"(<[^>]+>)")


def split_html(s: str, limit: int = CHUNK) -> list:
    """Режет уже валидный HTML на части ≤ limit, закрывая открытые теги в конце
    части и переоткрывая их в начале следующей."""
    if len(s) <= limit:
        return [s] if s.strip() else []
    parts, cur, stack = [], "", []   # stack: список (имя, открывающий тег)

    def closers():
        return "".join(f"</{n}>" for n, _ in reversed(stack))

    def flush():
        nonlocal cur
        if cur.strip() and re.sub(r"<[^>]+>", "", cur).strip():
            parts.append(cur + closers())
        cur = "".join(o for _, o in stack)

    for tok in _SPLIT_TOKEN_RE.split(s):
        if not tok:
            continue
        if tok.startswith("<") and _SPLIT_TOKEN_RE.fullmatch(tok):
            name = tok.strip("<>/ ").split()[0].lower()
            if tok.startswith("</"):
                if stack and stack[-1][0] == name:
                    stack.pop()
                cur += tok
            else:
                stack.append((name, tok))
                cur += tok
            continue
        while tok:
            room = limit - len(cur) - len(closers())
            if room <= 200 and cur.strip():
                flush()
                continue
            if len(tok) <= room:
                cur += tok
                tok = ""
            else:
                cut = tok.rfind("\n", 0, room)
                if cut < room // 2:
                    cut = tok.rfind(" ", 0, room)
                if cut <= 0:
                    cut = room
                # не рвём HTML-сущность
                amp = tok.rfind("&", max(0, cut - 8), cut)
                if amp != -1 and ";" not in tok[amp:cut]:
                    cut = amp or cut
                cur += tok[:cut]
                tok = tok[cut:].lstrip("\n")
                flush()
    if cur.strip() and re.sub(r"<[^>]+>", "", cur).strip():
        parts.append(cur)
    return parts


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def send_text(token: str, chat_id, text: str, reply_markup: dict = None, reply_to: int = None,
              silent: bool = False) -> list:
    """Отправляет форматированный текст (с нарезкой). Возвращает message_id
    отправленных сообщений. Если Telegram не принял разметку — шлёт plain."""
    ids = []
    chunks = split_html(to_tg_html(text)) or ["…"]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        if silent:
            payload["disable_notification"] = True
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        if reply_to and i == 0:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            res = api(token, "sendMessage", payload)
        except TgApiError as e:
            if "parse entities" not in str(e).lower():
                raise
            payload.update(text=strip_tags(chunk))
            payload.pop("parse_mode")
            res = api(token, "sendMessage", payload)
        ids.append(res["message_id"])
    return ids


# ── хранилище состояния (чеклисты, кнопки быстрых ответов) ──────────────────

_state_lock = threading.RLock()


def _load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("checklists", {})
    st.setdefault("quick", {})
    return st


def _save_state(st: dict):
    cl = st["checklists"]
    if len(cl) > MAX_CHECKLISTS:
        for k in sorted(cl, key=lambda k: cl[k].get("updated", 0))[:len(cl) - MAX_CHECKLISTS]:
            del cl[k]
    q = st["quick"]
    if len(q) > MAX_QUICK_SETS:
        for k in sorted(q, key=lambda k: q[k].get("created", 0))[:len(q) - MAX_QUICK_SETS]:
            del q[k]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# ── чеклисты ─────────────────────────────────────────────────────────────────

def _checklist_view(cid: str, cl: dict):
    items = cl["items"]
    done = sum(1 for i in items if i["done"])
    head = f"<b>{html.escape(cl['title'])}</b>"
    if items and done == len(items):
        head += "\n✅ Всё отмечено"
    elif items:
        head += f"\nОтмечено {done} из {len(items)}"
    rows = [[{"text": ("✅ " if i["done"] else "⬜ ") + i["t"][:60], "callback_data": f"tcl:{cid}:{i['id']}"}]
            for i in items]
    if done:
        rows.append([{"text": "🗑 Убрать отмеченные", "callback_data": f"tcl:{cid}:clr"}])
    return head, {"inline_keyboard": rows}


def _push_checklist(token: str, cid: str, cl: dict):
    text, markup = _checklist_view(cid, cl)
    try:
        api(token, "editMessageText", {
            "chat_id": cl["chat_id"], "message_id": cl["message_id"], "text": text,
            "parse_mode": "HTML", "reply_markup": markup})
    except TgApiError as e:
        if "not modified" not in str(e).lower():
            raise


def _clean_items(raw) -> list:
    out = []
    for x in raw or []:
        s = str(x).strip()
        if s:
            out.append(s[:200])
    return out


def _find_items(cl: dict, names) -> list:
    """Находит пункты по тексту: точное совпадение без регистра, иначе подстрока."""
    found = []
    for name in _clean_items(names):
        low = name.lower()
        hit = [i for i in cl["items"] if i["t"].lower() == low] or \
              [i for i in cl["items"] if low in i["t"].lower() or i["t"].lower() in low]
        found.extend(i for i in hit[:1] if i not in found)
    return found


def _checklist_summary(cid: str, cl: dict) -> dict:
    return {"id": cid, "title": cl["title"],
            "items": [{"text": i["t"], "done": i["done"]} for i in cl["items"]]}


def checklist_send(ctx, title, items):
    items = _clean_items(items)[:MAX_CHECKLIST_ITEMS]
    if not items:
        return {"error": "items пуст"}
    cid = uuid.uuid4().hex[:8]
    cl = {"chat_id": ctx.chat_id, "message_id": 0, "title": (title or "Список").strip()[:200],
          "items": [{"id": n + 1, "t": t, "done": False} for n, t in enumerate(items)],
          "next_id": len(items) + 1, "created": time.time(), "updated": time.time()}
    text, markup = _checklist_view(cid, cl)
    res = api(ctx.token, "sendMessage", {"chat_id": ctx.chat_id, "text": text, "parse_mode": "HTML",
                                         "reply_markup": markup})
    cl["message_id"] = res["message_id"]
    with _state_lock:
        st = _load_state()
        st["checklists"][cid] = cl
        _save_state(st)
    ctx.last_message_id = res["message_id"]
    ctx.sent.append(f"чеклист «{cl['title']}» (id {cid}): " + ", ".join(items))
    return {"ok": True, "id": cid, "message_id": res["message_id"]}


def checklist_update(ctx, cid, add=None, remove=None, check=None, uncheck=None, title=None):
    with _state_lock:
        st = _load_state()
        cl = st["checklists"].get(cid)
        if not cl or cl["chat_id"] != ctx.chat_id:
            return {"error": "чеклист не найден — возьми id из tg_get_checklists"}
        missing = []

        def pick(names):
            hit = _find_items(cl, names)
            if len(hit) < len(_clean_items(names)):
                missing.extend(n for n in _clean_items(names) if not _find_items(cl, [n]))
            return hit

        for i in pick(remove):
            cl["items"].remove(i)
        for i in pick(check):
            i["done"] = True
        for i in pick(uncheck):
            i["done"] = False
        for t in _clean_items(add):
            if len(cl["items"]) >= MAX_CHECKLIST_ITEMS:
                break
            cl["items"].append({"id": cl["next_id"], "t": t, "done": False})
            cl["next_id"] += 1
        if title:
            cl["title"] = title.strip()[:200]
        cl["updated"] = time.time()
        _push_checklist(ctx.token, cid, cl)
        _save_state(st)
        res = {"ok": True, **_checklist_summary(cid, cl)}
        if missing:
            res["not_found"] = missing
        return res


def checklists_get(ctx):
    with _state_lock:
        st = _load_state()
    mine = [(k, c) for k, c in st["checklists"].items() if c["chat_id"] == ctx.chat_id]
    mine.sort(key=lambda kc: kc[1].get("updated", 0), reverse=True)
    return {"checklists": [_checklist_summary(k, c) for k, c in mine[:10]]}


def handle_checklist_callback(token: str, cq: dict):
    """Нажатие на пункт чеклиста: переключает галочку и перерисовывает сообщение."""
    _, cid, what = (cq.get("data") or "").split(":", 2)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    note = None
    with _state_lock:
        st = _load_state()
        cl = st["checklists"].get(cid)
        if not cl or cl["chat_id"] != chat_id:
            note = "Список устарел"
        else:
            if what == "clr":
                cl["items"] = [i for i in cl["items"] if not i["done"]]
            else:
                for i in cl["items"]:
                    if str(i["id"]) == what:
                        i["done"] = not i["done"]
                        break
            cl["updated"] = time.time()
            try:
                _push_checklist(token, cid, cl)
            except TgApiError as e:
                print(f"  checklist edit: {e}")
            _save_state(st)
    try:
        api(token, "answerCallbackQuery", {"callback_query_id": cq.get("id"), **({"text": note} if note else {})})
    except TgApiError:
        pass


# ── кнопки быстрых ответов ───────────────────────────────────────────────────

def buttons_send(ctx, text, buttons):
    """buttons: [[{text, url?|reply?}]]; reply — текст, который уйдёт ассистенту при нажатии."""
    qid = uuid.uuid4().hex[:8]
    replies, rows = {}, []
    for r, row in enumerate((buttons or [])[:8]):
        out = []
        for c, b in enumerate((row or [])[:4]):
            label = str(b.get("text") or "").strip()[:60]
            if not label:
                continue
            if b.get("url"):
                out.append({"text": label, "url": b["url"]})
            else:
                key = f"{r}.{c}"
                replies[key] = str(b.get("reply") or label)[:500]
                out.append({"text": label, "callback_data": f"tqr:{qid}:{key}"})
        if out:
            rows.append(out)
    if not rows:
        return {"error": "нет кнопок"}
    ids = send_text(ctx.token, ctx.chat_id, text or "Выберите:", reply_markup={"inline_keyboard": rows})
    if replies:
        with _state_lock:
            st = _load_state()
            st["quick"][qid] = {"chat_id": ctx.chat_id, "replies": replies, "created": time.time()}
            _save_state(st)
    ctx.last_message_id = ids[-1]
    ctx.sent.append(f"кнопки: " + ", ".join(b["text"] for r in rows for b in r))
    return {"ok": True, "message_id": ids[-1]}


def handle_quick_callback(token: str, cq: dict, dispatch):
    """Нажатие на кнопку быстрого ответа: убираем клавиатуру и передаём текст ассистенту."""
    _, qid, key = (cq.get("data") or "").split(":", 2)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    with _state_lock:
        st = _load_state()
        q = st["quick"].get(qid)
        reply = q["replies"].get(key) if q and q["chat_id"] == chat_id else None
        if reply:
            del st["quick"][qid]
            _save_state(st)
    try:
        api(token, "answerCallbackQuery", {"callback_query_id": cq.get("id"), **({} if reply else {"text": "Кнопка устарела"})})
        if reply:
            api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg.get("message_id"),
                                                  "reply_markup": {"inline_keyboard": []}})
    except TgApiError:
        pass
    if reply:
        dispatch(token, chat_id, reply)


# ── контекст отправки и остальные инструменты ───────────────────────────────

class Ctx:
    """Контекст одного обращения к ассистенту: куда слать и что уже отправлено."""
    def __init__(self, token, chat_id, user_message_id=None):
        self.token = token
        self.chat_id = chat_id
        self.user_message_id = user_message_id
        self.last_message_id = None
        self.sent = []          # заметки об отправленном — уходят в историю диалога


_tls = threading.local()


def set_ctx(ctx):
    _tls.ctx = ctx


def get_ctx():
    return getattr(_tls, "ctx", None)


_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_KIND_METHOD = {"document": ("sendDocument", "document"), "photo": ("sendPhoto", "photo"),
                "video": ("sendVideo", "video"), "audio": ("sendAudio", "audio"),
                "voice": ("sendVoice", "voice"), "animation": ("sendAnimation", "animation")}


def file_send(ctx, filename=None, content=None, content_base64=None, url=None, kind=None, caption=None):
    filename = re.sub(r"[\\/\x00]", "_", (filename or "").strip()) or "file.txt"
    if not kind:
        ext = Path(filename).suffix.lower()
        kind = "photo" if (url and ext in _PHOTO_EXT) else "document"
    if kind not in _KIND_METHOD:
        return {"error": f"kind: {', '.join(_KIND_METHOD)}"}
    method, field = _KIND_METHOD[kind]
    payload = {"chat_id": ctx.chat_id}
    if caption:
        payload["caption"] = to_tg_html(caption)[:1024]
        payload["parse_mode"] = "HTML"
    files = None
    if url:
        payload[field] = url
    elif content_base64:
        try:
            data = base64.b64decode(content_base64, validate=False)
        except Exception:
            return {"error": "content_base64 не декодируется"}
    elif content is not None:
        data = str(content).encode("utf-8")
        if data.startswith(b"\xef\xbb\xbf") is False and Path(filename).suffix.lower() == ".csv":
            data = b"\xef\xbb\xbf" + data   # BOM — чтобы Excel не ломал кириллицу
    else:
        return {"error": "нужен content, content_base64 или url"}
    if not url:
        if len(data) > MAX_UPLOAD:
            return {"error": "файл больше 45 МБ"}
        if not data:
            return {"error": "пустой файл"}
        files = {field: (filename, data, mimetypes.guess_type(filename)[0] or "application/octet-stream")}
    try:
        api(ctx.token, "sendChatAction", {"chat_id": ctx.chat_id, "action": "upload_document"})
    except TgApiError:
        pass
    try:
        res = api(ctx.token, method, payload, files=files)
    except TgApiError as e:
        if "parse entities" in str(e).lower() and payload.get("caption"):
            payload["caption"] = strip_tags(payload["caption"])
            payload.pop("parse_mode")
            res = api(ctx.token, method, payload, files=files)
        else:
            return {"error": str(e)}
    ctx.last_message_id = res.get("message_id")
    ctx.sent.append(f"{kind} «{filename}»" + (f" ({caption[:80]})" if caption else ""))
    return {"ok": True, "message_id": res.get("message_id")}


def poll_send(ctx, question, options, multiple=False, anonymous=True, quiz_correct_option=None, explanation=None):
    opts = _clean_items(options)[:10]
    if len(opts) < 2:
        return {"error": "нужно 2–10 вариантов"}
    payload = {"chat_id": ctx.chat_id, "question": str(question)[:300],
               "options": [{"text": o[:100]} for o in opts], "is_anonymous": bool(anonymous)}
    if quiz_correct_option is not None:
        payload.update(type="quiz", correct_option_id=int(quiz_correct_option))
        if explanation:
            payload["explanation"] = str(explanation)[:200]
    else:
        payload["allows_multiple_answers"] = bool(multiple)
    res = api(ctx.token, "sendPoll", payload)
    ctx.last_message_id = res["message_id"]
    ctx.sent.append(f"опрос «{question}»: " + ", ".join(opts))
    return {"ok": True, "message_id": res["message_id"]}


def location_send(ctx, latitude, longitude, title=None, address=None):
    lat, lon = float(latitude), float(longitude)
    if title:
        res = api(ctx.token, "sendVenue", {"chat_id": ctx.chat_id, "latitude": lat, "longitude": lon,
                                           "title": title[:200], "address": (address or title)[:200]})
    else:
        res = api(ctx.token, "sendLocation", {"chat_id": ctx.chat_id, "latitude": lat, "longitude": lon})
    ctx.last_message_id = res["message_id"]
    ctx.sent.append(f"геопозиция {title or ''} ({lat}, {lon})")
    return {"ok": True, "message_id": res["message_id"]}


def contact_send(ctx, phone, first_name, last_name=None):
    res = api(ctx.token, "sendContact", {"chat_id": ctx.chat_id, "phone_number": str(phone),
                                         "first_name": first_name, **({"last_name": last_name} if last_name else {})})
    ctx.last_message_id = res["message_id"]
    ctx.sent.append(f"контакт {first_name} {phone}")
    return {"ok": True, "message_id": res["message_id"]}


def dice_send(ctx, emoji="🎲"):
    if emoji not in ("🎲", "🎯", "🏀", "⚽", "🎳", "🎰"):
        emoji = "🎲"
    res = api(ctx.token, "sendDice", {"chat_id": ctx.chat_id, "emoji": emoji})
    ctx.last_message_id = res["message_id"]
    ctx.sent.append(f"кубик {emoji}: выпало {res.get('dice', {}).get('value')}")
    return {"ok": True, "value": res.get("dice", {}).get("value")}


def react(ctx, emoji="👍"):
    if not ctx.user_message_id:
        return {"error": "нет сообщения для реакции"}
    try:
        api(ctx.token, "setMessageReaction", {"chat_id": ctx.chat_id, "message_id": ctx.user_message_id,
                                              "reaction": [{"type": "emoji", "emoji": emoji}]})
    except TgApiError as e:
        return {"error": f"{e} (допустимы только стандартные реакции Telegram: 👍 ❤ 🔥 👏 😁 🤔 🎉 🙏 👌 и т.п.)"}
    return {"ok": True}


def pin(ctx, message_id=None, unpin=False):
    mid = message_id or ctx.last_message_id
    if not mid:
        return {"error": "message_id не задан и в этом ответе ещё ничего не отправлено"}
    try:
        if unpin:
            api(ctx.token, "unpinChatMessage", {"chat_id": ctx.chat_id, "message_id": mid})
        else:
            api(ctx.token, "pinChatMessage", {"chat_id": ctx.chat_id, "message_id": mid, "disable_notification": True})
    except TgApiError as e:
        return {"error": str(e)}
    return {"ok": True}


def _t(name, desc, props, required=()):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": list(required)}}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

BOT_TOOLS = [
    _t("tg_send_checklist",
       "Отправить в чат интерактивный список с галочками (кнопки: нажал — пункт отмечен). Для списка покупок, дел, "
       "сборов, шагов. Пункты в items — коротко, по одному на элемент. Возвращает id для tg_update_checklist.",
       {"title": _STR, "items": _STRS}, ["title", "items"]),
    _t("tg_update_checklist",
       "Изменить уже отправленный чеклист: add — добавить пункты, remove — убрать, check/uncheck — отметить/снять "
       "(по тексту пункта), title — переименовать. id берётся из tg_send_checklist или tg_get_checklists.",
       {"id": _STR, "add": _STRS, "remove": _STRS, "check": _STRS, "uncheck": _STRS, "title": _STR}, ["id"]),
    _t("tg_get_checklists",
       "Текущее состояние чеклистов этого чата (что отмечено, что нет) — вызывай, когда спрашивают «что ещё не "
       "куплено» или просят дополнить существующий список.", {}),
    _t("tg_send_file",
       "Отправить файл в чат. Текстовый файл (txt, md, csv, json, html, py и т.д.) — передай content. Бинарный — "
       "content_base64. Файл или картинка по ссылке — url. kind: document (по умолчанию), photo, video, audio, "
       "voice, animation. caption — подпись (допустим HTML). Длинные таблицы, код, документы, выгрузки — файлом, "
       "а не простынёй текста.",
       {"filename": _STR, "content": _STR, "content_base64": _STR, "url": _STR,
        "kind": {"type": "string", "enum": list(_KIND_METHOD)}, "caption": _STR}, []),
    _t("tg_send_poll",
       "Отправить опрос (2–10 вариантов). Викторина — quiz_correct_option (индекс верного варианта с 0) и explanation.",
       {"question": _STR, "options": _STRS, "multiple": {"type": "boolean"}, "anonymous": {"type": "boolean"},
        "quiz_correct_option": {"type": "integer"}, "explanation": _STR}, ["question", "options"]),
    _t("tg_send_buttons",
       "Отправить сообщение с кнопками под ним. У кнопки либо url (ссылка), либо reply — текст, который придёт тебе "
       "как сообщение пользователя при нажатии (быстрый выбор вариантов). buttons — строки кнопок.",
       {"text": _STR, "buttons": {"type": "array", "items": {"type": "array", "items": {
           "type": "object", "properties": {"text": _STR, "url": _STR, "reply": _STR}, "required": ["text"]}}}},
       ["text", "buttons"]),
    _t("tg_send_location", "Отправить геопозицию (с title — как место с названием и адресом).",
       {"latitude": {"type": "number"}, "longitude": {"type": "number"}, "title": _STR, "address": _STR},
       ["latitude", "longitude"]),
    _t("tg_send_contact", "Отправить карточку контакта.", {"phone": _STR, "first_name": _STR, "last_name": _STR},
       ["phone", "first_name"]),
    _t("tg_send_dice", "Бросить анимированный кубик (🎲 🎯 🏀 ⚽ 🎳 🎰) — жребий, «орёл или решка», развлечение.",
       {"emoji": _STR}),
    _t("tg_react", "Поставить реакцию-эмодзи на сообщение пользователя (👍 ❤ 🔥 👏 😁 🤔 🎉 🙏 👌 и т.п.).",
       {"emoji": _STR}),
    _t("tg_pin_message",
       "Закрепить (или открепить unpin:true) сообщение в чате. Без message_id — последнее отправленное тобой "
       "в этом ответе (чеклист, файл и т.д.).", {"message_id": {"type": "integer"}, "unpin": {"type": "boolean"}}),
]
TOOL_NAMES = {t["name"] for t in BOT_TOOLS}

_DISPATCH = {
    "tg_send_checklist": lambda c, i: checklist_send(c, i.get("title"), i.get("items")),
    "tg_update_checklist": lambda c, i: checklist_update(c, i.get("id"), i.get("add"), i.get("remove"),
                                                         i.get("check"), i.get("uncheck"), i.get("title")),
    "tg_get_checklists": lambda c, i: checklists_get(c),
    "tg_send_file": lambda c, i: file_send(c, i.get("filename"), i.get("content"), i.get("content_base64"),
                                           i.get("url"), i.get("kind"), i.get("caption")),
    "tg_send_poll": lambda c, i: poll_send(c, i.get("question"), i.get("options"), i.get("multiple"),
                                           i.get("anonymous", True), i.get("quiz_correct_option"), i.get("explanation")),
    "tg_send_buttons": lambda c, i: buttons_send(c, i.get("text"), i.get("buttons")),
    "tg_send_location": lambda c, i: location_send(c, i.get("latitude"), i.get("longitude"), i.get("title"), i.get("address")),
    "tg_send_contact": lambda c, i: contact_send(c, i.get("phone"), i.get("first_name"), i.get("last_name")),
    "tg_send_dice": lambda c, i: dice_send(c, i.get("emoji") or "🎲"),
    "tg_react": lambda c, i: react(c, i.get("emoji") or "👍"),
    "tg_pin_message": lambda c, i: pin(c, i.get("message_id"), bool(i.get("unpin"))),
}


def execute_tool(name: str, inp: dict) -> dict:
    ctx = get_ctx()
    if ctx is None:
        return {"error": "Инструмент доступен только в Telegram-чате."}
    try:
        return _DISPATCH[name](ctx, inp or {})
    except TgApiError as e:
        return {"error": f"Telegram отклонил запрос: {e}"}
    except (TypeError, ValueError) as e:
        return {"error": f"неверные аргументы: {e}"}


PROMPT_SECTION = (
    "ФОРМАТИРОВАНИЕ ОТВЕТА. Ответ уходит в Telegram как HTML. Допустимые теги: <b>жирный</b>, <i>курсив</i>, "
    "<u>подчёркнутый</u>, <s>зачёркнутый</s>, <code>моно</code>, <pre>блок кода</pre>, <a href=\"URL\">ссылка</a>, "
    "<blockquote>цитата</blockquote> (<blockquote expandable> — сворачиваемая), <tg-spoiler>скрытый текст</tg-spoiler>. "
    "Никакого markdown (** # ``` и т.п.) и других тегов. Списки — строками с «• » или «1. ». Заголовки — через <b>. "
    "Не перегружай разметкой: короткий ответ — просто текст.\n\n"
    "ВОЗМОЖНОСТИ TELEGRAM — инструменты tg_*, они отправляют в чат сразу, до твоего финального ответа:\n"
    "• Список покупок / дел / сборов / шагов — ВСЕГДА через tg_send_checklist (кнопки-галочки), а не текстом. "
    "Дополнить или отметить пункты в уже отправленном списке — tg_update_checklist (id из tg_get_checklists, если не помнишь); "
    "«что ещё не куплено» — tg_get_checklists. После отправки чеклиста не пересказывай его пункты в финальном ответе.\n"
    "• Файл, таблица, выгрузка, код, длинный текст/документ (>~25 строк) — tg_send_file с понятным именем и расширением "
    "(csv для таблиц, md/txt для текстов, html, json…). Картинка или файл по ссылке — tg_send_file с url.\n"
    "• Выбор из вариантов — tg_send_buttons (reply = что прийдёт тебе при нажатии) или tg_send_poll для голосования. "
    "Геопозиция — tg_send_location, контакт — tg_send_contact, жребий — tg_send_dice, закрепить важное — tg_pin_message, "
    "быстрое подтверждение без слов — tg_react.\n"
    "• Пользователь может присылать фото и текстовые файлы — их содержимое придёт тебе в сообщении.\n"
    "Финальный ответ после инструментов — одна короткая строка, без дублирования отправленного.\n\n"
)


# ── память диалога ───────────────────────────────────────────────────────────

MAX_HISTORY = 30   # чётное число: история всегда начинается с сообщения пользователя

_convo = {}
_convo_meta = {}   # chat_id -> {"ts": последняя активность, "lock": Lock}
_convo_guard = threading.Lock()


def chat_lock(chat_id) -> threading.Lock:
    with _convo_guard:
        return _convo_meta.setdefault(chat_id, {"ts": 0.0, "lock": threading.Lock()})["lock"]


def history_for(chat_id) -> list:
    """История чата. Если пауза с последней активности дольше TTL — контекст
    сбрасывается. Вызывать под chat_lock(chat_id)."""
    with _convo_guard:
        meta = _convo_meta.setdefault(chat_id, {"ts": 0.0, "lock": threading.Lock()})
        hist = _convo.setdefault(chat_id, [])
        if hist and time.time() - meta["ts"] > context_ttl_seconds():
            hist.clear()
        return hist


def touch(chat_id):
    with _convo_guard:
        _convo_meta.setdefault(chat_id, {"ts": 0.0, "lock": threading.Lock()})["ts"] = time.time()


def reset(chat_id):
    with _convo_guard:
        _convo.pop(chat_id, None)


def trim(history: list):
    del history[:-MAX_HISTORY]
    while history and history[0].get("role") != "user":
        history.pop(0)


# ── входящие вложения ────────────────────────────────────────────────────────

MAX_DOWNLOAD = 15 * 1024 * 1024
_TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".log", ".ini",
             ".py", ".js", ".ts", ".sql", ".sh", ".ics", ".srt"}
MAX_TEXT_FILE_CHARS = 60000


def download_file(token: str, file_id: str, size_hint: int = 0):
    if size_hint and size_hint > MAX_DOWNLOAD:
        return None
    info = api(token, "getFile", {"file_id": file_id})
    if info.get("file_size", 0) > MAX_DOWNLOAD:
        return None
    r = requests.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=60)
    return r.content if r.ok else None


def extract_attachments(token: str, msg: dict):
    """Из сообщения → (текст пользователя, [изображения {media_type, b64}], заметка о неподдержанном).
    Текстовые файлы вставляются прямо в текст."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    images, unsupported = [], None
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        data = download_file(token, best["file_id"], best.get("file_size", 0))
        if data:
            images.append({"media_type": "image/jpeg", "b64": base64.b64encode(data).decode()})
        else:
            unsupported = "фото слишком большое"
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or "file"
        ext = Path(name).suffix.lower()
        mime = doc.get("mime_type") or ""
        if mime in ("image/jpeg", "image/png", "image/webp", "image/gif") and not images:
            data = download_file(token, doc["file_id"], doc.get("file_size", 0))
            if data:
                images.append({"media_type": mime, "b64": base64.b64encode(data).decode()})
        elif ext in _TEXT_EXT or mime.startswith("text/"):
            data = download_file(token, doc["file_id"], doc.get("file_size", 0))
            if data:
                body = data.decode("utf-8", errors="replace")
                cut = len(body) > MAX_TEXT_FILE_CHARS
                text = (text + "\n\n" if text else "") + f"[Файл «{name}»]\n{body[:MAX_TEXT_FILE_CHARS]}" + \
                       ("\n[…обрезано]" if cut else "")
            else:
                unsupported = f"файл «{name}» не удалось скачать (лимит бота — 20 МБ)"
        else:
            unsupported = f"файлы такого типа («{name}») пока не читаю — только текстовые и изображения"
    if msg.get("voice") or msg.get("audio") or msg.get("video_note") or msg.get("video"):
        unsupported = "голосовые и видео пока не расшифровываю — напишите текстом"
    return text, images, unsupported
