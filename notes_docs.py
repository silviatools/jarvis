"""Документы в «Прочее → Заметки».

Список, чтение и правка — на сайте. Текст с оформлением, фото, видео и любые
файлы лежат в DATA_DIR и попадают в бэкап:

  DATA_DIR/jarvis_notes.json  — документы
  DATA_DIR/jarvis_notes/      — вложения

Ассистент умеет искать и читать (навык notes_docs). Текст вложений читается
тем же file_tools.read_file, что вложения заданий «Учёбы». Фото прикладывается
к ответу блоком _image, только если текущая модель его принимает. Видео, аудио
и нечитаемый бинарный файл так и помечаются. Править документы инструментами нельзя.
"""

import base64
import html
import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

try:
    import file_tools
except Exception:
    file_tools = None

STORE_NAME = "jarvis_notes.json"
FILES_DIR_NAME = "jarvis_notes"
SKILL_ID = "notes_docs"

MAX_DOCS = 400
MAX_HTML = 400_000
MAX_TEXT = 200_000
MAX_TITLE = 200
MAX_FILES = 40
MAX_FILE_BYTES = 50 * 1024 * 1024
READ_LIMIT = 12_000
FILE_TEXT = 8_000
FILES_TEXT = 24_000
VISION_MAX = 4_500_000
EXCERPT = 180
SEARCH_LIMIT = 30
_VISION_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_lock = threading.RLock()
_see = threading.local()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_FILE_SRC = re.compile(r"^/api/notes/file/[A-Za-z0-9_-]{8,80}$")

_ALLOWED = {
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "strike", "del",
    "h1", "h2", "h3", "ul", "ol", "li", "blockquote", "a", "pre", "code", "hr", "img",
}
_VOID = {"br", "hr", "img"}
_SKIP = {
    "script", "style", "iframe", "object", "embed", "svg", "math", "form",
    "textarea", "button", "input", "link", "meta", "base", "noscript",
}
_BLOCK = {"p", "div", "h1", "h2", "h3", "li", "blockquote", "br", "pre", "hr"}

SKILLS = [
    {"id": SKILL_ID, "title": "Документы в заметках", "risk": "read", "default": True,
     "desc": ("Ассистент ищет и читает документы из Прочее → Заметки → Документы: "
              "заметки с оформлением, фото, видео и любыми файлами. "
              "Текст документа и текстовых вложений приходит обычными абзацами, без вёрстки. "
              "Файлы txt, pdf, docx и другие читаемые форматы читаются так же, как вложения заданий «Учёбы». "
              "Фото модель смотрит, только если сама принимает изображения; иначе в ответе будет, что фото не посмотреть. "
              "Видео, аудио и нечитаемый файл приходят с пометкой, что содержимое не разобрать. "
              "Править, создавать и удалять документы этими инструментами нельзя: это делается в разделе на сайте."),
     "tools": ["notes_search", "notes_read"]},
]


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


_STR = {"type": "string"}

TOOLS = [
    {"name": "notes_search",
     "description": ("Найти документы в Прочее → Заметки → Документы по словам из названия, текста и имён файлов. "
                     "Пустой query — последние документы. Возвращает id, название, отрывок и список вложений "
                     "(имя, тип, размер), без полного текста."),
     "input_schema": {"type": "object", "properties": {
         "query": _STR,
         "limit": {"type": "integer", "description": "сколько документов вернуть, до 30"},
     }, "required": []}},
    {"name": "notes_read",
     "description": ("Прочитать один документ из Заметок вместе с вложениями. id — из notes_search. "
                     "Либо query — название или фраза: если документ один, вернётся его текст; "
                     "если несколько — список, без полного текста, тогда вызови ещё раз с id. "
                     "Текст документа без оформления. Читаемые файлы (txt, pdf, docx, xlsx и др.) — "
                     "в files[].text, тем же чтением, что вложения заданий «Учёбы». "
                     "Фото: если модель его принимает, одна картинка приложена к ответу (files[].seen). "
                     "Остальные фото — повторный вызов с file (id или часть имени). "
                     "Видео, аудио и нечитаемый файл — files[].unread, содержимого нет."),
     "input_schema": {"type": "object", "properties": {
         "id": _STR, "query": _STR,
         "file": {"type": "string", "description": "id или часть имени одного вложения"},
     }, "required": []}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}

PROMPT = (
    "ДОКУМЕНТЫ В ЗАМЕТКАХ — notes_search, notes_read. "
    "Раздел Прочее → Заметки → Документы хранит заметки пользователя: оформленный текст, фото, видео и любые файлы.\n"
    "а) Вопрос про заметку, конспект или «что в документе» — сначала notes_search, затем notes_read по id. "
    "Не отвечай по памяти и не выдумывай текст.\n"
    "б) Текст документа и files[].text уже без вёрстки: txt, pdf, docx и прочие читаемые форматы читаются так же, "
    "как вложения заданий «Учёбы». Если truncated — скажи, что прочитано начало.\n"
    "в) Фото со seen:true приложено к ответу — смотри его. Если unread говорит, что модель не смотрит фото, "
    "так и скажи: имени и размера достаточно, содержимое не описывай. Несколько фото: смотри приложенное, "
    "для остальных вызови notes_read с file=id.\n"
    "г) Видео, аудио и бинарный файл, который не прочитать, помечены unread. Назови имя, тип и размер и скажи, "
    "что содержимое разобрать нельзя. Не выдумывай, что внутри.\n"
    "д) Создавать, править и удалять документы этими инструментами нельзя. Если просят изменить — скажи, что это в "
    "Прочее → Заметки → Документы.\n"
    "е) needs_clarification — несколько документов или вложений подошли. Перечисли названия и спроси, какое открыть. "
    "Не склеивай их в один текст.\n\n"
)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": PROMPT}


def execute(name: str, inp: dict, state) -> dict:
    if not skill_on(state):
        return {"error": "Навык «Документы в заметках» выключен."}
    inp = inp or {}
    if name == "notes_search":
        return search_result(inp.get("query") or "", inp.get("limit"))
    if name == "notes_read":
        return read_result(inp.get("id") or "", inp.get("query") or "", inp.get("file") or "")
    return {"error": "unknown tool"}


@contextmanager
def vision_scope(enabled: bool):
    """Включает просмотр фото на время чтения. Снаружи цикла модели — выключено."""
    prev = getattr(_see, "on", None)
    _see.on = bool(enabled)
    try:
        yield
    finally:
        if prev is None:
            try:
                del _see.on
            except AttributeError:
                pass
        else:
            _see.on = prev


def _vision_on() -> bool:
    return getattr(_see, "on", False) is True


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def files_dir() -> Path:
    return data_dir() / FILES_DIR_NAME


def _now() -> int:
    return int(time.time() * 1000)


def _load() -> dict:
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    docs = raw.get("docs") if isinstance(raw, dict) else None
    if not isinstance(docs, list):
        docs = []
    return {"docs": [d for d in docs if isinstance(d, dict) and _ok_id(str(d.get("id") or ""))]}


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _ok_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def _clean(value, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text.strip()[:limit]


def _filename(name: str, ext: str = "") -> str:
    base = Path(str(name or "")).name
    base = "".join(ch for ch in base if ord(ch) >= 32 and ch not in '\\/:*?"<>|').strip().strip(".")
    base = base[:180]
    if not base:
        base = "файл"
    if "." not in base and ext:
        suffix = "".join(ch for ch in str(ext).lower() if ch.isalnum())[:8]
        if suffix:
            base = f"{base}.{suffix}"
    return base


def _fold(value: str) -> str:
    return str(value or "").casefold().replace("ё", "е")


def _safe_href(value) -> str:
    text = str(value or "").strip()
    low = text.lower()
    if not (low.startswith("https://") or low.startswith("http://") or low.startswith("mailto:")):
        return ""
    if any(ch in text for ch in ' \t\r\n"<>'):
        return ""
    return text[:2000]


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.texts = []
        self.skip = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if self.skip:
            if tag in _SKIP:
                self.skip += 1
            return
        if tag in _SKIP:
            self.skip = 1
            return
        if tag in _BLOCK:
            self.texts.append("\n")
        if tag not in _ALLOWED:
            return
        rendered = self._attrs(tag, attrs)
        if tag in _VOID:
            self.parts.append(f"<{tag}{rendered}>")
            return
        self.stack.append(tag)
        self.parts.append(f"<{tag}{rendered}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if (tag or "").lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self.skip:
            if tag in _SKIP:
                self.skip = max(0, self.skip - 1)
            return
        if tag in _BLOCK:
            self.texts.append("\n")
        if tag not in _ALLOWED or tag in _VOID:
            return
        if tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self.skip or not data:
            return
        self.parts.append(html.escape(data))
        self.texts.append(data)

    def close(self):
        super().close()
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")

    def _attrs(self, tag, attrs):
        out = []
        for key, value in attrs:
            key = str(key or "").lower()
            if key.startswith("on") or key in ("style", "class", "id", "srcdoc"):
                continue
            if tag == "a" and key == "href":
                href = _safe_href(value)
                if href:
                    out.append(f' href="{html.escape(href, quote=True)}"')
                    out.append(' rel="noopener noreferrer"')
            elif tag == "img" and key == "src":
                src = str(value or "").strip()
                if _FILE_SRC.match(src):
                    out.append(f' src="{html.escape(src, quote=True)}"')
            elif tag == "img" and key == "alt":
                alt = _clean(value, 180).replace("\n", " ")
                if alt:
                    out.append(f' alt="{html.escape(alt, quote=True)}"')
        return "".join(out)


def sanitize(raw: str) -> tuple:
    """Вернуть (безопасный html, плоский текст)."""
    source = str(raw or "")
    if len(source) > MAX_HTML:
        source = source[:MAX_HTML]
    parser = _Clean()
    try:
        parser.feed(source)
        parser.close()
        cleaned = "".join(parser.parts).strip()
        text = _plain("".join(parser.texts))
    except Exception:
        text = _plain(re.sub(r"<[^>]*>", " ", source))
        cleaned = html.escape(text)
    return cleaned, text[:MAX_TEXT]


def _plain(value: str) -> str:
    text = str(value or "").replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _excerpt(text: str) -> str:
    one = re.sub(r"\s+", " ", text or "").strip()
    if len(one) <= EXCERPT:
        return one
    return one[:EXCERPT - 1].rstrip() + "…"


def _file_public(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or "файл",
        "size": int(item.get("size") or 0),
        "mime": item.get("mime") or "application/octet-stream",
        "kind": item.get("kind") or "file",
    }


def _card(doc: dict) -> dict:
    return {
        "id": doc.get("id"),
        "title": doc.get("title") or "",
        "excerpt": _excerpt(doc.get("text") or ""),
        "updatedAt": doc.get("updatedAt") or 0,
        "createdAt": doc.get("createdAt") or 0,
        "files": [_file_public(f) for f in (doc.get("files") or [])],
    }


def _full(doc: dict) -> dict:
    row = _card(doc)
    row["html"] = doc.get("html") or ""
    return row


def _read_view(doc: dict, files: list) -> dict:
    text = doc.get("text") or ""
    return {
        "id": doc.get("id"),
        "title": doc.get("title") or "",
        "text": text[:READ_LIMIT],
        "truncated": len(text) > READ_LIMIT,
        "files": files,
        "updatedAt": doc.get("updatedAt") or 0,
    }


def _pick_files(files: list, raw: str):
    """Одно вложение по id или части имени. Пустой запрос — все. Неоднозначно — список совпадений."""
    sel = str(raw or "").strip()
    if not sel:
        return list(files), None
    low = sel.casefold()
    hit = [f for f in files if f.get("id") == sel or low in str(f.get("name") or "").casefold()]
    if len(hit) == 1:
        return hit, None
    if not hit:
        return [], "missing"
    return hit, "several"


def _readable_ext(name: str) -> str:
    if not file_tools:
        return ""
    return file_tools.ext_of(name or "")


def _expand_file(doc: dict, item: dict, budget: list, image_holder: dict, focus: bool) -> dict:
    """Читает одно вложение. budget — [оставшиеся символы текста]. image_holder['img'] — первое фото."""
    meta = _file_public(item)
    path = _file_path(doc.get("id"), item.get("id"))
    if path is None or not path.is_file():
        meta["unread"] = "файл не найден на диске"
        return meta
    try:
        size = path.stat().st_size
    except OSError:
        meta["unread"] = "файл не найден на диске"
        return meta
    if size > MAX_FILE_BYTES:
        meta["unread"] = "файл больше 50 МБ — не читаю"
        return meta
    kind = item.get("kind") or "file"
    mime = item.get("mime") or ""
    name = item.get("name") or "файл"
    if kind == "image" or str(mime).startswith("image/"):
        return _expand_image(meta, path, mime, item.get("id"), image_holder, focus)
    if kind in ("video", "audio") or str(mime).startswith(("video/", "audio/")):
        meta["unread"] = "видео и аудио не читаются"
        return meta
    ext = _readable_ext(name)
    if not file_tools:
        meta["unread"] = "чтение файлов на сервере недоступно"
        return meta
    if ext and ext not in file_tools.READABLE_EXT:
        meta["unread"] = f"формат .{ext} не читается, содержимое не прочитать"
        return meta
    if not ext:
        try:
            sample = path.read_bytes()[:8000]
        except OSError:
            meta["unread"] = "файл не найден на диске"
            return meta
        if b"\x00" in sample:
            meta["unread"] = "бинарный файл, содержимое не прочитать"
            return meta
    limit = min(FILE_TEXT, budget[0])
    if limit <= 0:
        meta["unread"] = "текст вложения не влез в ответ — вызови notes_read с file=\"" + str(item.get("id") or "") + "\""
        return meta
    try:
        data = path.read_bytes()
        got = file_tools.read_file(data, name, limit)
    except file_tools.FileToolError as e:
        meta["unread"] = str(e)
        return meta
    except OSError:
        meta["unread"] = "файл не найден на диске"
        return meta
    meta["text"] = got.get("text") or ""
    meta["truncated"] = bool(got.get("truncated"))
    meta["format"] = got.get("format") or ext or "txt"
    budget[0] = max(0, budget[0] - len(meta["text"]))
    return meta


def _expand_image(meta: dict, path: Path, mime: str, file_id, image_holder: dict, focus: bool) -> dict:
    if mime not in _VISION_MIME:
        meta["unread"] = "этот формат фото не разбирается"
        return meta
    if not _vision_on():
        meta["unread"] = "текущая модель не смотрит фото"
        return meta
    try:
        size = path.stat().st_size
        data = path.read_bytes() if size <= VISION_MAX else b""
    except OSError:
        meta["unread"] = "файл не найден на диске"
        return meta
    if size > VISION_MAX:
        meta["unread"] = "фото больше 4.5 МБ, к ответу не прикладываю"
        return meta
    if image_holder.get("img") is not None and not focus:
        meta["note"] = "чтобы посмотреть, вызови notes_read с file=\"" + str(file_id or "") + "\""
        return meta
    image_holder["img"] = {"media_type": mime, "b64": base64.b64encode(data).decode()}
    meta["seen"] = True
    meta["note"] = "фото приложено к этому ответу — посмотри его"
    return meta


def _files_for_read(doc: dict, file_ref: str):
    files = list(doc.get("files") or [])
    chosen, why = _pick_files(files, file_ref)
    if why == "missing":
        return None, {"ok": False, "error": "Вложение не найдено", "files": [_file_public(f) for f in files]}
    if why == "several":
        view = _read_view(doc, [_file_public(f) for f in chosen])
        return None, {
            "ok": True,
            "needs_clarification": True,
            "message": "Подошло несколько вложений. Уточни file — какое открыть.",
            "doc": view,
        }
    targets = chosen if str(file_ref or "").strip() else files
    focus_id = chosen[0].get("id") if str(file_ref or "").strip() and chosen else None
    budget = [FILES_TEXT]
    holder = {"img": None}
    expanded = {}
    ordered = sorted(targets, key=lambda item: 0 if item.get("id") == focus_id else 1)
    for item in ordered:
        expanded[item.get("id")] = _expand_file(doc, item, budget, holder, item.get("id") == focus_id)
    if focus_id:
        shown = [_file_public(item) if item.get("id") != focus_id else expanded[item.get("id")] for item in files]
    else:
        shown = [expanded.get(item.get("id")) or _file_public(item) for item in files]
    return holder.get("img"), {"ok": True, "doc": _read_view(doc, shown)}


def _find(data: dict, doc_id: str):
    for doc in data["docs"]:
        if doc.get("id") == doc_id:
            return doc
    return None


def list_docs() -> dict:
    with _lock:
        docs = _load()["docs"]
    docs.sort(key=lambda d: d.get("updatedAt") or 0, reverse=True)
    return {"ok": True, "docs": [_card(d) for d in docs]}


def get_doc(doc_id: str):
    if not _ok_id(doc_id):
        return 400, {"ok": False, "error": "Неверный документ"}
    with _lock:
        doc = _find(_load(), doc_id)
    if not doc:
        return 404, {"ok": False, "error": "Документ не найден"}
    return 200, {"ok": True, "doc": _full(doc)}


def create_doc(payload: dict):
    title = _clean((payload or {}).get("title"), MAX_TITLE).replace("\n", " ")
    html_in = (payload or {}).get("html") or ""
    if not isinstance(html_in, str):
        return 400, {"ok": False, "error": "Текст документа должен быть строкой"}
    if len(html_in) > MAX_HTML:
        return 400, {"ok": False, "error": "Текст слишком большой"}
    cleaned, text = sanitize(html_in)
    now = _now()
    doc = {
        "id": str(uuid.uuid4()),
        "title": title,
        "html": cleaned,
        "text": text,
        "files": [],
        "createdAt": now,
        "updatedAt": now,
    }
    with _lock:
        data = _load()
        if len(data["docs"]) >= MAX_DOCS:
            return 400, {"ok": False, "error": f"Уже {MAX_DOCS} документов — удалите ненужный"}
        data["docs"].append(doc)
        _save(data)
    return 200, {"ok": True, "doc": _full(doc)}


def save_doc(payload: dict):
    doc_id = str((payload or {}).get("id") or "")
    if not _ok_id(doc_id):
        return 400, {"ok": False, "error": "Неверный документ"}
    has_title = "title" in (payload or {})
    has_html = "html" in (payload or {})
    if not has_title and not has_html:
        return 400, {"ok": False, "error": "Нечего сохранять"}
    html_in = (payload or {}).get("html") if has_html else None
    if has_html and not isinstance(html_in, str):
        return 400, {"ok": False, "error": "Текст документа должен быть строкой"}
    if has_html and len(html_in) > MAX_HTML:
        return 400, {"ok": False, "error": "Текст слишком большой"}
    with _lock:
        data = _load()
        doc = _find(data, doc_id)
        if not doc:
            return 404, {"ok": False, "error": "Документ не найден"}
        if has_title:
            doc["title"] = _clean(payload.get("title"), MAX_TITLE).replace("\n", " ")
        if has_html:
            cleaned, text = sanitize(html_in)
            doc["html"] = cleaned
            doc["text"] = text
        doc["updatedAt"] = _now()
        _save(data)
        return 200, {"ok": True, "doc": _full(doc)}


def delete_doc(doc_id: str):
    if not _ok_id(doc_id):
        return 400, {"ok": False, "error": "Неверный документ"}
    with _lock:
        data = _load()
        if not _find(data, doc_id):
            return 404, {"ok": False, "error": "Документ не найден"}
        data["docs"] = [d for d in data["docs"] if d.get("id") != doc_id]
        _save(data)
    folder = _doc_dir(doc_id)
    if folder and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return 200, {"ok": True}


def _doc_dir(doc_id: str):
    if not _ok_id(doc_id):
        return None
    root = files_dir().resolve()
    path = (root / doc_id).resolve()
    if path.parent != root:
        return None
    return path


def _file_path(doc_id: str, file_id: str):
    folder = _doc_dir(doc_id)
    if folder is None or not _ok_id(file_id):
        return None
    path = (folder / file_id).resolve()
    if path.parent != folder:
        return None
    return path


def _sniff(data: bytes):
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if len(data) >= 12 and b"ftyp" in data[:32]:
        head = data[:32]
        if b"heic" in head or b"heif" in head or b"mif1" in head:
            return "image", "image/heic"
        return "video", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio", "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio", "audio/flac"
    if data.startswith(b"%PDF"):
        return "file", "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "file", "application/zip"
    return "file", "application/octet-stream"


def save_file(doc_id: str, name: str, ext: str, blob: bytes):
    if not _ok_id(doc_id):
        return 400, {"ok": False, "error": "Неверный документ"}
    if not blob:
        return 400, {"ok": False, "error": "Пустой файл"}
    if len(blob) > MAX_FILE_BYTES:
        return 413, {"ok": False, "error": "Файл больше 50 МБ"}
    kind, mime = _sniff(blob)
    file_id = str(uuid.uuid4())
    path = _file_path(doc_id, file_id)
    if path is None:
        return 400, {"ok": False, "error": "Неверный документ"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    item = {
        "id": file_id,
        "name": _filename(name, ext),
        "size": len(blob),
        "mime": mime,
        "kind": kind,
    }
    with _lock:
        data = _load()
        doc = _find(data, doc_id)
        if not doc:
            path.unlink(missing_ok=True)
            return 404, {"ok": False, "error": "Документ не найден"}
        files = list(doc.get("files") or [])
        if len(files) >= MAX_FILES:
            path.unlink(missing_ok=True)
            return 400, {"ok": False, "error": f"В документе уже {MAX_FILES} файлов"}
        files.append(item)
        doc["files"] = files
        doc["updatedAt"] = _now()
        _save(data)
        return 200, {"ok": True, "file": _file_public(item), "doc": _full(doc)}


def remove_file(file_id: str):
    if not _ok_id(file_id):
        return 400, {"ok": False, "error": "Неверный файл"}
    removed = None
    with _lock:
        data = _load()
        for doc in data["docs"]:
            files = list(doc.get("files") or [])
            keep = [f for f in files if f.get("id") != file_id]
            if len(keep) != len(files):
                doc["files"] = keep
                doc["updatedAt"] = _now()
                removed = (doc.get("id"), doc)
                break
        if not removed:
            return 404, {"ok": False, "error": "Файл не найден"}
        _save(data)
    path = _file_path(removed[0], file_id)
    if path and path.exists():
        path.unlink()
    return 200, {"ok": True, "doc": _full(removed[1])}


def open_file(file_id: str):
    if not _ok_id(file_id):
        return None
    with _lock:
        for doc in _load()["docs"]:
            for item in doc.get("files") or []:
                if item.get("id") != file_id:
                    continue
                path = _file_path(doc.get("id"), file_id)
                if path and path.is_file():
                    return path, item.get("mime") or "application/octet-stream", item.get("name") or "файл", item.get("kind") or "file"
    return None


def _hay(doc: dict) -> str:
    names = " ".join(str(f.get("name") or "") for f in (doc.get("files") or []))
    return _fold(f"{doc.get('title') or ''}\n{doc.get('text') or ''}\n{names}")


def _matches(doc: dict, words: list) -> bool:
    if not words:
        return True
    hay = _hay(doc)
    return all(word in hay for word in words)


def _ranked(query: str, limit: int) -> list:
    words = [w for w in re.split(r"\s+", _fold(query).strip()) if w]
    folded = _fold(query).strip()
    with _lock:
        docs = list(_load()["docs"])
    scored = []
    for doc in docs:
        if not _matches(doc, words):
            continue
        title = _fold(doc.get("title") or "")
        score = 0
        if folded and folded == title:
            score += 6
        elif folded and folded in title:
            score += 4
        elif words and all(w in title for w in words):
            score += 2
        scored.append((score, doc.get("updatedAt") or 0, doc))
    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [doc for _, _, doc in scored[:limit]]


def _limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 20
    return max(1, min(SEARCH_LIMIT, number))


def search_result(query: str, limit=None) -> dict:
    found = _ranked(str(query or ""), _limit(limit))
    return {"ok": True, "docs": [_card(doc) for doc in found], "count": len(found)}


def _with_image(result: dict, image) -> dict:
    if image:
        result["_image"] = {"media_type": image["media_type"], "b64": image["b64"]}
    return result


def read_result(doc_id: str, query: str, file_ref: str = "") -> dict:
    doc_id = str(doc_id or "").strip()
    query = str(query or "").strip()
    file_ref = str(file_ref or "")
    if doc_id:
        if not _ok_id(doc_id):
            return {"ok": False, "error": "Неверный документ"}
        with _lock:
            doc = _find(_load(), doc_id)
        if not doc:
            return {"ok": False, "error": "Документ не найден"}
        image, result = _files_for_read(doc, file_ref)
        return result if not result.get("ok") or result.get("needs_clarification") else _with_image(result, image)
    if not query:
        found = _ranked("", 20)
        return {"ok": True, "docs": [_card(doc) for doc in found], "count": len(found)}
    found = _ranked(query, 8)
    if not found:
        return {"ok": True, "docs": [], "count": 0}
    folded = _fold(query)
    exact = [doc for doc in found if _fold(doc.get("title") or "") == folded]
    picked = exact[0] if len(exact) == 1 else (found[0] if len(found) == 1 else None)
    if picked is not None:
        image, result = _files_for_read(picked, file_ref)
        return result if not result.get("ok") or result.get("needs_clarification") else _with_image(result, image)
    return {
        "ok": True,
        "needs_clarification": True,
        "message": "Подошло несколько документов. Уточни, какой открыть.",
        "docs": [_card(doc) for doc in found],
    }
