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

import file_tools

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
        self.openai_key = ""    # ключ для генерации картинок (задаёт вызывающий код)


_tls = threading.local()


def set_ctx(ctx):
    _tls.ctx = ctx


def get_ctx():
    return getattr(_tls, "ctx", None)


_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_KIND_METHOD = {"document": ("sendDocument", "document"), "photo": ("sendPhoto", "photo"),
                "video": ("sendVideo", "video"), "audio": ("sendAudio", "audio"),
                "voice": ("sendVoice", "voice"), "animation": ("sendAnimation", "animation")}


def file_send(ctx, filename=None, content=None, content_base64=None, url=None, kind=None, caption=None, sheets=None):
    filename = re.sub(r"[\\/\x00]", "_", (filename or "").strip()) or "file.txt"
    if not url and not content_base64 and (content or sheets) and file_tools.ext_of(filename) in file_tools.BUILDABLE_EXT:
        try:   # docx / xlsx / pptx / pdf собираем из markdown-разметки или таблиц
            built, filename = file_tools.build(filename, content, sheets)
        except file_tools.FileToolError as e:
            return {"error": f"не удалось собрать {filename}: {e}"}
        content, content_base64 = None, base64.b64encode(built).decode()
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
        if not data.startswith(b"\xef\xbb\xbf") and Path(filename).suffix.lower() in (".csv", ".tsv"):
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


class ProgressMessage:
    """Сообщение «идёт работа» с анимацией (бегущая луна, полоска прогресса, секундомер,
    смена этапов): редактируется раз в пару секунд, пока выполняется долгая операция,
    и удаляется при выходе из with — и при успехе, и при ошибке."""
    MOON = "🌑🌒🌓🌔🌕🌖🌗🌘"

    def __init__(self, token, chat_id, title, detail="", stages=(), interval=2.0, expected=45.0):
        self.token, self.chat_id = token, chat_id
        self.title, self.detail = title, detail
        self.stages = stages                  # [(с какой секунды, текст)]
        self.interval, self.expected = interval, expected
        self.message_id = None
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.time()

    def _render(self, tick: int) -> str:
        elapsed = time.time() - self._t0
        # Точное время неизвестно: полоска плавно приближается к 90% и ждёт.
        pct = 0.9 * (1 - 2.718281828 ** (-elapsed / (self.expected / 2.5)))
        filled = max(1, round(pct * 10))
        bar = "▰" * filled + "▱" * (10 - filled)
        stage = ""
        for since, text in self.stages:
            if elapsed >= since:
                stage = text
        dots = "." * (tick % 3 + 1)
        lines = [f"🎨 <b>{html.escape(self.title)}</b>"]
        if self.detail:
            lines += ["", f"<blockquote>{html.escape(self.detail)}</blockquote>"]
        lines += ["", f"<code>{bar}</code> {int(pct * 100)}%",
                  f"{self.MOON[tick % len(self.MOON)]} <i>{html.escape(stage)}{dots}</i>",
                  f"⏱ {int(elapsed)} сек"]
        return "\n".join(lines)

    def _run(self):
        tick = 1
        while not self._stop.wait(self.interval):
            try:
                api(self.token, "editMessageText", {"chat_id": self.chat_id, "message_id": self.message_id,
                                                    "text": self._render(tick), "parse_mode": "HTML"})
            except TgApiError:
                pass    # «not modified», лимиты и т.п. — анимация не критична
            tick += 1

    def __enter__(self):
        try:
            self.message_id = api(self.token, "sendMessage", {
                "chat_id": self.chat_id, "text": self._render(0), "parse_mode": "HTML",
                "disable_notification": True})["message_id"]
        except TgApiError:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.message_id:
            try:
                api(self.token, "deleteMessage", {"chat_id": self.chat_id, "message_id": self.message_id})
            except TgApiError:
                pass
        return False


IMAGE_STAGES = [(0, "Придумываю композицию"), (8, "Рисую контуры"), (20, "Добавляю цвета и детали"),
                (38, "Дорабатываю мелочи"), (60, "Почти готово, ещё чуть-чуть")]
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
_IMAGE_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}


def image_generate(ctx, prompt, size="square", caption=None):
    """Генерирует картинку через OpenAI Images API и отправляет в чат фото."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "prompt пуст"}
    if not ctx.openai_key:
        return {"error": "Нет ключа OpenAI для картинок: добавьте набор ключей OpenAI в Настройки → Ассистент "
                         "или переменную OPENAI_API_KEY."}
    with ProgressMessage(ctx.token, ctx.chat_id, "Рисую картинку", prompt[:140] + ("…" if len(prompt) > 140 else ""),
                         IMAGE_STAGES):
        try:
            r = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {ctx.openai_key}", "Content-Type": "application/json"},
                json={"model": IMAGE_MODEL, "prompt": prompt[:4000], "n": 1,
                      "size": _IMAGE_SIZES.get(size, "1024x1024")},
                timeout=180,
            )
        except Exception as e:
            return {"error": f"Ошибка связи с OpenAI: {e}"}
        if not r.ok:
            try:
                err = r.json().get("error", {}).get("message")
            except Exception:
                err = None
            return {"error": f"OpenAI не сгенерировал картинку: {err or r.status_code}"}
        try:
            item = r.json()["data"][0]
            if item.get("b64_json"):
                data = base64.b64decode(item["b64_json"])
            else:
                data = requests.get(item["url"], timeout=60).content
        except Exception as e:
            return {"error": f"Не удалось получить картинку: {e}"}
        payload = {"chat_id": ctx.chat_id}
        if caption:
            payload.update(caption=to_tg_html(caption)[:1024], parse_mode="HTML")
        try:
            try:
                res = api(ctx.token, "sendPhoto", payload, files={"photo": ("image.png", data, "image/png")})
            except TgApiError as e:
                if "parse entities" in str(e).lower() and payload.get("caption"):
                    payload.update(caption=strip_tags(payload["caption"]))
                    payload.pop("parse_mode")
                    res = api(ctx.token, "sendPhoto", payload, files={"photo": ("image.png", data, "image/png")})
                else:   # напр. слишком большое для фото — шлём файлом
                    res = api(ctx.token, "sendDocument", payload, files={"document": ("image.png", data, "image/png")})
        except TgApiError as e:
            return {"error": str(e)}
    ctx.last_message_id = res.get("message_id")
    ctx.sent.append(f"сгенерированная картинка по запросу «{prompt[:100]}»")
    return {"ok": True, "message_id": res.get("message_id")}


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
       "Отправить файл в чат. Формат определяется расширением filename. Документы собираются из content в "
       "markdown-разметке: DOCX, PDF (с кириллицей), PPTX (новый слайд — на «# »/«## » заголовке или «---»); "
       "поддерживаются заголовки #, списки - и 1., таблицы |a|b|, **жирный**, *курсив*, `код`, ```блоки```, "
       "> цитаты, --- разделитель. XLSX — sheets [{name, rows:[[…]]}] (первая строка — шапка; строка с «=» — формула) "
       "или content с таблицей/CSV. Текстовые форматы (txt, md, csv, json, html, py…) — content как есть. "
       "Готовый бинарный файл — content_base64, файл/картинка по ссылке — url. kind: document (по умолчанию), "
       "photo, video, audio, voice, animation. caption — подпись (допустим HTML). Длинные тексты, таблицы, код, "
       "выгрузки — файлом, а не простынёй.",
       {"filename": _STR, "content": _STR, "content_base64": _STR, "url": _STR,
        "sheets": {"type": "array", "items": {"type": "object", "properties": {
            "name": _STR, "rows": {"type": "array", "items": {"type": "array", "items": {}}}}}},
        "kind": {"type": "string", "enum": list(_KIND_METHOD)}, "caption": _STR}, ["filename"]),
    _t("tg_generate_image",
       "Сгенерировать картинку по описанию и отправить в чат (OpenAI). prompt — подробное описание сцены/стиля "
       "(на английском работает точнее, но можно и на русском); size: square, landscape (горизонтальная), "
       "portrait (вертикальная). Занимает до минуты. Просьбы «нарисуй», «сгенерируй картинку/аватар/логотип» — сюда.",
       {"prompt": _STR, "size": {"type": "string", "enum": list(_IMAGE_SIZES)}, "caption": _STR}, ["prompt"]),
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
                                           i.get("url"), i.get("kind"), i.get("caption"), i.get("sheets")),
    "tg_generate_image": lambda c, i: image_generate(c, i.get("prompt"), i.get("size") or "square", i.get("caption")),
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
    "ФОРМАТИРОВАНИЕ — ОБЯЗАТЕЛЬНО В КАЖДОМ ОТВЕТЕ. Ты пишешь в Telegram, ответ уходит как HTML. Доступные теги: "
    "<b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, <s>зачёркнутый</s>, <code>моно (числа, коды, команды — "
    "копируются тапом)</code>, <pre>блок кода/таблица</pre>, <a href=\"URL\">ссылка</a>, <blockquote>цитата</blockquote>, "
    "<blockquote expandable>сворачиваемый длинный блок</blockquote>, <tg-spoiler>скрытый текст</tg-spoiler>. "
    "Других тегов и markdown (** # ``` [](); дефисы-списки) нет — они не сработают.\n"
    "Оформляй КАЖДЫЙ ответ красиво и читаемо:\n"
    "• Ключевое (сумма, дата, название, вывод) — <b>жирным</b>; пояснения и второстепенное — <i>курсивом</i>.\n"
    "• Ответ из нескольких частей — заголовки <b>жирным</b> с эмодзи по смыслу (💰 финансы, 📅 даты, ✅ готово, ⚠️ важно, "
    "🛒 покупки, 🍽 еда, 🚗 авто, ☀️ погода, 📚 учёба), пустая строка между блоками.\n"
    "• Перечисления — строками с «• » или «1. », одна мысль на строку; не сплошной абзац.\n"
    "• Числа, суммы, коды, команды, пути — в <code>…</code>. Таблицы и выровненные данные — в <pre>…</pre>.\n"
    "• Длинное пояснение или справку прячь в <blockquote expandable>, главное оставляй сверху.\n"
    "• Эмодзи — умеренно, как маркеры, не в каждой строке. Короткий ответ в одну фразу — просто текст, без украшений.\n"
    "• Символы < > & в обычном тексте пиши как есть — они экранируются сами. Не оборачивай весь ответ в один тег.\n\n"
    "ВОЗМОЖНОСТИ TELEGRAM — ты умеешь всё из этого списка и пользуешься этим сам, без напоминаний. Инструменты tg_* "
    "отправляют в чат сразу, до твоего финального ответа:\n"
    "• Список покупок / дел / сборов / шагов — ВСЕГДА через tg_send_checklist (кнопки-галочки), а не текстом. "
    "Дополнить или отметить пункты в уже отправленном списке — tg_update_checklist (id из tg_get_checklists, если не помнишь); "
    "«что ещё не куплено» — tg_get_checklists. После отправки чеклиста не пересказывай его пункты в финальном ответе.\n"
    "• Файл, таблица, выгрузка, код, длинный текст/документ (>~25 строк) — tg_send_file с понятным именем и расширением. "
    "Умеешь создавать DOCX, XLSX, PPTX, PDF, CSV, TXT, MD, JSON, HTML. Просят «Word / Excel / презентацию / PDF» — делай "
    "именно этот формат (docx / xlsx / pptx / pdf), а не txt. Документ пиши полностью и качественно: заголовки, списки, "
    "таблицы. Картинка или файл по ссылке — tg_send_file с url.\n"
    "• Читать умеешь: DOCX, XLSX/XLS, PPTX, PDF (с текстом, не сканы), RTF, ODT/ODS/ODP, ZIP, CSV, TXT, MD, JSON, HTML, "
    "код и изображения. Присланные файлы приходят тебе текстом «[Файл «имя»]…». Старые .doc/.ppt не читаются — "
    "попроси пересохранить в docx/pptx.\n"
    "• Картинки ты РИСУЕШЬ САМ: просьбы «нарисуй / сгенерируй картинку, аватар, логотип, открытку» — tg_generate_image "
    "(не предлагай пользователю промпты для сторонних сервисов, а сразу генерируй). Подпись к картинке — caption.\n"
    "• Выбор из вариантов — tg_send_buttons (reply = что придёт тебе при нажатии) или tg_send_poll для голосования. "
    "Геопозиция — tg_send_location, контакт — tg_send_contact, жребий — tg_send_dice, закрепить важное — tg_pin_message, "
    "быстрое подтверждение без слов — tg_react.\n"
    "• Пользователь может присылать фото, текстовые файлы, голосовые, кружки и видео — всё приходит тебе текстом: "
    "голосовые автоматически расшифрованы (пометка «[Голосовое сообщение — расшифровка]»), пересланные помечены "
    "«[Переслано от …]». Просто работай с этим текстом (перескажи, ответь, выдели задачи). Если расшифровка не "
    "получилась — в сообщении будет пометка почему, перескажи её пользователю.\n"
    "• Ответ любой длины показывается целиком (сам режется на несколько сообщений) — не сокращай ради лимита Telegram.\n"
    "• Контекст диалога сохраняется, пока переписка идёт без пауз дольше 10 минут; после — начинается с чистого листа "
    "(пользователь может сбросить вручную командой /new).\n"
    "После инструментов финальный ответ — коротко, без дублирования отправленного.\n\n"
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

MAX_DOWNLOAD = 20 * 1024 * 1024    # потолок getFile у ботов
MAX_TEXT_FILE_CHARS = 60000


_recent_files = {}          # chat_id -> [(ts, имя, bytes)] — присланное, чтобы потом положить в задачу
RECENT_FILES_TTL = 30 * 60
RECENT_FILES_KEEP = 5


def remember_file(chat_id, name: str, data: bytes):
    now = time.time()
    with _convo_guard:
        lst = [f for f in _recent_files.get(chat_id, []) if now - f[0] < RECENT_FILES_TTL]
        lst.append((now, name, data))
        _recent_files[chat_id] = lst[-RECENT_FILES_KEEP:]


def recent_file(chat_id, name_hint: str = ""):
    """Присланный файл по части имени; без подсказки — самый свежий. → (имя, bytes) | None."""
    now = time.time()
    with _convo_guard:
        lst = [f for f in _recent_files.get(chat_id, []) if now - f[0] < RECENT_FILES_TTL]
    if not lst:
        return None
    hint = (name_hint or "").strip().lower()
    for _, name, data in reversed(lst):
        if not hint or hint in name.lower() or name.lower() in hint:
            return name, data
    return None


def download_file(token: str, file_id: str, size_hint: int = 0):
    if size_hint and size_hint > MAX_DOWNLOAD:
        return None
    info = api(token, "getFile", {"file_id": file_id})
    if info.get("file_size", 0) > MAX_DOWNLOAD:
        return None
    r = requests.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=60)
    return r.content if r.ok else None


TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
MAX_AUDIO = 20 * 1024 * 1024    # лимит getFile для ботов; у OpenAI — 25 МБ


def transcribe_audio(data: bytes, filename: str, openai_key: str) -> str:
    """Расшифровка речи через OpenAI. Бросает TgApiError с понятным текстом."""
    last = None
    for model in dict.fromkeys([TRANSCRIBE_MODEL, "whisper-1"]):   # если модель недоступна — whisper-1
        try:
            r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {openai_key}"},
                              data={"model": model}, files={"file": (filename, data)}, timeout=180)
        except Exception as e:
            raise TgApiError(f"ошибка связи с OpenAI: {e}")
        if r.ok:
            return (r.json().get("text") or "").strip()
        try:
            last = r.json().get("error", {}).get("message")
        except Exception:
            last = None
        last = last or f"HTTP {r.status_code}"
        if r.status_code not in (400, 403, 404):
            break
    raise TgApiError(last)


def _forward_note(msg: dict) -> str:
    o = msg.get("forward_origin") or {}
    t = o.get("type")
    if t == "user":
        u = o.get("sender_user") or {}
        who = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    elif t == "hidden_user":
        who = o.get("sender_user_name", "")
    else:
        who = (o.get("chat") or {}).get("title", "") if o else ""
    return f"[Переслано от {who}]" if who else ("[Пересланное сообщение]" if o else "")


def extract_attachments(token: str, msg: dict, openai_key: str = ""):
    """Из сообщения → (текст пользователя, [изображения {media_type, b64}], заметка о неподдержанном).
    Текстовые файлы вставляются прямо в текст, голосовые/кружки/видео — расшифровкой."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    images, unsupported = [], None
    chat_id = (msg.get("chat") or {}).get("id")
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        data = download_file(token, best["file_id"], best.get("file_size", 0))
        if data:
            images.append({"media_type": "image/jpeg", "b64": base64.b64encode(data).decode()})
            remember_file(chat_id, f"photo_{msg.get('message_id', '')}.jpg", data)
        else:
            unsupported = "фото слишком большое"
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or "file"
        mime = doc.get("mime_type") or ""
        data = download_file(token, doc["file_id"], doc.get("file_size", 0))
        if data is None:
            unsupported = f"файл «{name}» не удалось скачать (лимит бота — 20 МБ)"
        else:
            remember_file(chat_id, name, data)   # чтобы можно было положить в задачу «Учёбы»
            if mime in ("image/jpeg", "image/png", "image/webp", "image/gif") and not images:
                images.append({"media_type": mime, "b64": base64.b64encode(data).decode()})
            else:
                try:
                    body = file_tools.read_file(data, name, MAX_TEXT_FILE_CHARS)["text"]
                    note = f"[Файл «{name}»]\n{body or '(файл пуст)'}"
                except file_tools.FileToolError as e:
                    note = f"[Файл «{name}» получен ({len(data) // 1024} КБ), но не прочитан: {e}]"
                text = (text + "\n\n" if text else "") + note
    media = [(k, msg[k]) for k in ("voice", "audio", "video_note", "video") if msg.get(k)]
    if media:
        kind, m = media[0]
        label = {"voice": "Голосовое сообщение", "audio": "Аудио", "video_note": "Видеокружок", "video": "Видео"}[kind]
        if not openai_key:
            unsupported = "для расшифровки голосовых нужен ключ OpenAI (Настройки → Ассистент или OPENAI_API_KEY)"
        else:
            data = download_file(token, m["file_id"], m.get("file_size", 0))
            if not data or len(data) > MAX_AUDIO:
                unsupported = f"{label.lower()} слишком большое для расшифровки (до 20 МБ)"
            else:
                ext = Path(m.get("file_name") or "").suffix or {"voice": ".ogg", "audio": ".mp3"}.get(kind, ".mp4")
                try:
                    said = transcribe_audio(data, f"{kind}{ext}", openai_key)
                except TgApiError as e:
                    unsupported = f"не удалось расшифровать {label.lower()}: {e}"
                else:
                    dur = m.get("duration")
                    head = f"[{label}{f', {int(dur) // 60}:{int(dur) % 60:02d}' if dur else ''} — расшифровка]"
                    text = (text + "\n\n" if text else "") + f"{head}\n{said or '(речь не распознана)'}"
    fwd = _forward_note(msg)
    if fwd and (text or images):
        text = f"{fwd}\n{text}" if text else fwd
    return text, images, unsupported
