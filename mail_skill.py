"""
mail_skill.py — навык «Почта Mail.ru»: ящики по IMAP, только чтение.

Что здесь:
  • SKILLS — две записи каталога «Навыки ассистента»: чтение и поиск писем (инструменты mail_*)
    и уведомления о новых письмах; переключатели — data.telegramSkills[<id>];
  • TOOLS — инструменты для ИИ (бот, голос и чат на сайте — одни и те же схемы);
  • ящики: адрес и подпись — DATA_DIR/jarvis_mail.json (в бэкапе), пароли приложений —
    DATA_DIR/jarvis_mail/secrets.json с правами 600 (не в бэкапе, не в git, наружу не отдаются);
  • индекс заголовков всех папок — DATA_DIR/jarvis_mail/index-<id>.json. У Mail.ru IMAP SEARCH не
    ищет по тексту, отправителю и теме (отвечает «Unsupported search criterion»), поэтому поиск
    идёт по локальному индексу; тексты писем докачиваются в индекс по требованию;
  • фоновая проверка (IDLE у Mail.ru нет): раз в POLL_SECONDS STATUS по папкам, новые письма —
    в индекс и в уведомления. Первая синхронизация ящика только строит индекс и ничего не шлёт.

Письма читаются через BODY.PEEK и EXAMINE (папка открыта только на чтение) — отметка
«непрочитано» на сервере не меняется. Отправки, удаления и перемещения нет.
"""

import base64
import email
import html
import imaplib
import json
import os
import re
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import file_tools

IMAP_HOST = "imap.mail.ru"
IMAP_PORT = 993
POLL_SECONDS = 90
FULL_REFRESH_SECONDS = 600       # раз в 10 минут: какие письма прочитаны и какие удалены
FETCH_BATCH = 200
BODY_CACHE_CHARS = 6000          # сколько текста письма держим в индексе для поиска
DEEP_SCAN_LIMIT = 120            # писем без текста в индексе, которые докачиваем за один поиск
MAX_RAW_BYTES = 30 * 1024 * 1024
MAX_TEXT = 12000
MAX_IMAGE_BYTES = 4_500_000
MAX_NOTIFY_PER_POLL = 8
POLL_STALE_SECONDS = 300         # дольше без проверки — поток считается мёртвым или зависшим
SYNC_DEADLINE = 240              # одна проверка ящика дольше — рвём соединение и идём дальше
LOCK_WAIT = 60                   # ящик занят поиском или «Проверить» — пропускаем до следующего круга
NOTIFY_WAIT = 60
ABORT_WAIT = 15                  # после сброса соединения ждём, пока проверка выйдет с ошибкой
MSK = timezone(timedelta(hours=3))

READ_ID = "mail"
NOTIFY_ID = "mail_notify"

SKILLS = [
    {"id": READ_ID, "title": "Почта Mail.ru", "risk": "read", "default": True,
     "desc": ("Ассистент читает ваши ящики Mail.ru (Настройки → Почта): все папки, поиск и фильтры по ящику, папке, "
              "отправителю, получателю, теме, тексту, датам, непрочитанным и вложениям, сортировка по дате, отправителю "
              "и размеру. Открывает письмо (HTML превращает в текст), показывает картинки, читает вложения docx, xlsx, "
              "pdf и др. и присылает файл вложения в чат. Только чтение: письма не отправляет, не удаляет и не меняет "
              "отметку «прочитано». Поиск по тексту идёт по индексу на сервере Джарвиса — сразу после добавления ящика "
              "индекс строится несколько минут. В боте ящик доступен владельцу и тем, кому включены уведомления этого ящика."),
     "tools": ["mail_accounts", "mail_search", "mail_read", "mail_attachment"]},
    {"id": NOTIFY_ID, "title": "Уведомления о новых письмах", "risk": "read", "default": True,
     "desc": ("Сервер проверяет ящики Mail.ru раз в 1–2 минуты (мгновенного push по IMAP у Mail.ru нет) и присылает "
              "в бот новое письмо: ящик, отправитель, тема и начало текста; если у ящика включён пуш — ещё и уведомление "
              "приложения Джарвис на iPhone (Настройки → Общее → «Уведомления на этот телефон»). Кому и с каких ящиков — "
              "Настройки → Общее → «Маршрутизация уведомлений» или карточка ящика во вкладке «Почта»; папки — во вкладке «Почта». "
              "Ответьте на уведомление, чтобы ассистент открыл письмо. Старые письма при подключении не присылаются."),
     "tools": []},
]

_STR = {"type": "string"}
_ACC = {"type": "string", "description": "Ящик: адрес (имя@mail.ru) или id из mail_accounts. Пусто — все ящики."}
_FOLDER = {"type": "string", "description": "Папка как в mail_accounts (например «Входящие», «Отправленные»). Пусто — все папки."}

TOOLS = [
    {"name": "mail_accounts",
     "description": "Подключённые ящики Mail.ru и их папки: сколько писем и непрочитанных, как давно обновлялся индекс.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "mail_search",
     "description": ("Список и поиск писем по всем ящикам и папкам (только чтение). Без фильтров — последние письма. "
                     "Фильтры складываются (И). Текст ищется по теме, адресам и тексту письма. "
                     "Ответ: items (account, folder, uid, date, from, to, subject, size, unread, has_attachments, snippet), "
                     "total, next_offset — для следующей страницы передай offset=next_offset."),
     "input_schema": {"type": "object", "properties": {
         "account": _ACC, "folder": _FOLDER,
         "from": {"type": "string", "description": "Отправитель: часть имени или адреса."},
         "to": {"type": "string", "description": "Получатель (To или Cc): часть имени или адреса."},
         "subject": {"type": "string", "description": "Часть темы."},
         "text": {"type": "string", "description": "Слова в теме, адресах или тексте письма."},
         "date_from": {"type": "string", "description": "С даты YYYY-MM-DD включительно (МСК)."},
         "date_to": {"type": "string", "description": "По дату YYYY-MM-DD включительно (МСК)."},
         "unread": {"type": "boolean", "description": "true — только непрочитанные, false — только прочитанные."},
         "has_attachments": {"type": "boolean", "description": "true — только с вложениями."},
         "sort": {"type": "string", "enum": ["date", "from", "size"], "description": "По умолчанию date."},
         "order": {"type": "string", "enum": ["desc", "asc"], "description": "По умолчанию desc (новые/большие сверху), для from — asc."},
         "limit": {"type": "integer", "description": "Сколько писем на странице, до 50 (по умолчанию 20)."},
         "offset": {"type": "integer", "description": "Сколько пропустить (пагинация)."},
     }}},
    {"name": "mail_read",
     "description": ("Открыть письмо: заголовки, текст (HTML переведён в обычный текст), список вложений и картинок "
                     "(index, filename, mime, size, inline). Отметку «прочитано» не меняет. Длинный текст — частями через offset."),
     "input_schema": {"type": "object", "properties": {
         "account": {"type": "string", "description": "Ящик из результата mail_search."},
         "folder": {"type": "string", "description": "Папка из результата mail_search."},
         "uid": {"type": "integer", "description": "uid письма из mail_search."},
         "offset": {"type": "integer", "description": "С какого символа текста продолжить."},
     }, "required": ["account", "folder", "uid"]}},
    {"name": "mail_attachment",
     "description": ("Вложение или картинка письма по index из mail_read. action=read — прочитать содержимое "
                     "(docx, xlsx, pptx, pdf, txt, csv…; картинку — посмотреть самому); action=send — прислать файл "
                     "в текущий чат Telegram (только в боте)."),
     "input_schema": {"type": "object", "properties": {
         "account": _STR, "folder": _STR, "uid": {"type": "integer"},
         "index": {"type": "integer", "description": "Номер вложения из mail_read.attachments."},
         "action": {"type": "string", "enum": ["read", "send"]},
         "offset": {"type": "integer", "description": "С какого символа продолжить чтение длинного файла."},
     }, "required": ["account", "folder", "uid", "index", "action"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
if _missing:
    raise RuntimeError("mail skills catalog tools missing: " + ", ".join(_missing))

PROMPT = (
    "ПОЧТА MAIL.RU — ящики пользователя, только чтение: mail_accounts (ящики и папки), mail_search (список, поиск, "
    "фильтры, сортировка, страницы через offset), mail_read (открыть письмо), mail_attachment (прочитать вложение "
    "или прислать файл в чат). Читать можно без спроса. Отправлять, удалять, перемещать письма и менять отметку "
    "«прочитано» ты не можешь — так и скажи, если попросят. Вопросы «что пришло», «найди письмо от …», «что в "
    "письме» — сначала mail_search, потом mail_read по account/folder/uid из результата. Если поиск вернул "
    "next_offset, а пользователю нужно больше — следующая страница. Если ящик не подключён или вход не удался — "
    "скажи и направь в Настройки → Почта. Не выдумывай письма, которых нет в результате.\n\n"
)


def skill_on(state, sid: str = READ_ID) -> bool:
    default = next((s["default"] for s in SKILLS if s["id"] == sid), True)
    v = (state or {}).get(sid)
    return default if v is None else bool(v)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    """Те же инструменты и тот же текст, что у циклов ассистента. Пусто, если навык выключен."""
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": prompt_section(state)}


class MailError(Exception):
    pass


# ── хранилище ────────────────────────────────────────────────────────────────

def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def config_path() -> Path:
    return data_dir() / "jarvis_mail.json"


def private_dir() -> Path:
    return data_dir() / "jarvis_mail"


def secrets_path() -> Path:
    return private_dir() / "secrets.json"


def index_path(acc_id: str) -> Path:
    return private_dir() / f"index-{acc_id}.json"


_cfg_lock = threading.RLock()
_acc_locks = {}
_indexes = {}
_conns = {}
_notifiers = []
_poller = None


def _write_json(path: Path, data, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config() -> dict:
    with _cfg_lock:
        cfg = _read_json(config_path(), {})
        if not isinstance(cfg.get("accounts"), list):
            cfg["accounts"] = []
        return cfg


def _save_config(cfg: dict):
    with _cfg_lock:
        _write_json(config_path(), cfg)


def _secrets() -> dict:
    return _read_json(secrets_path(), {})


def _password(acc_id: str) -> str:
    return (_secrets().get(acc_id) or {}).get("password") or ""


def _set_password(acc_id: str, password):
    with _cfg_lock:
        s = _secrets()
        if password is None:
            s.pop(acc_id, None)
        else:
            s[acc_id] = {"password": password}
        _write_json(secrets_path(), s, private=True)


def _update_account(acc_id: str, **patch):
    with _cfg_lock:
        cfg = load_config()
        for a in cfg["accounts"]:
            if a["id"] == acc_id:
                a.update(patch)
                _save_config(cfg)
                return a
    return None


def _acc_lock(acc_id: str) -> threading.RLock:
    with _cfg_lock:
        return _acc_locks.setdefault(acc_id, threading.RLock())


def _index(acc_id: str) -> dict:
    with _cfg_lock:
        if acc_id not in _indexes:
            idx = _read_json(index_path(acc_id), {})
            idx.setdefault("folders", {})
            _indexes[acc_id] = idx
        return _indexes[acc_id]


def _save_index(acc_id: str):
    idx = _indexes.get(acc_id)
    if idx is not None:
        _write_json(index_path(acc_id), idx, private=True)


def redact(text, extra=()) -> str:
    s = str(text)
    for v in list(extra) + [x.get("password") for x in _secrets().values() if isinstance(x, dict)]:
        if v and len(v) >= 4:
            s = s.replace(v, "•••")
    return s[:400]


def _log(msg):
    try:
        print("mail: " + redact(msg), flush=True)
    except Exception:
        pass


def mask_email(addr: str) -> str:
    local, _, domain = str(addr or "").partition("@")
    return (local[:1] + "***@" + domain) if domain else "***"


# ── IMAP ─────────────────────────────────────────────────────────────────────

def decode_mutf7(name: str) -> str:
    """Имя папки IMAP (modified UTF-7, RFC 3501) → обычная строка."""
    out, i = [], 0
    while i < len(name):
        c = name[i]
        if c == "&":
            j = name.find("-", i)
            if j < 0:
                out.append(name[i:])
                break
            chunk = name[i + 1:j]
            if not chunk:
                out.append("&")
            else:
                b = chunk.replace(",", "/")
                b += "=" * (-len(b) % 4)
                try:
                    out.append(base64.b64decode(b).decode("utf-16-be"))
                except Exception:
                    out.append(name[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


_FOLDER_RU = {"INBOX": "Входящие"}
_SKIP_NOTIFY = re.compile(r"спам|spam|junk|корзин|trash|удал[её]нн|отправлен|sent|черновик|draft", re.I)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def classify_login_error(e) -> str:
    """Ошибка входа → понятный текст. Пароль в текст не попадает."""
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"Сеть: {IMAP_HOST} не ответил вовремя. Проверьте позже; если повторяется — сервер Джарвиса не достаёт до Mail.ru."
    if isinstance(e, socket.gaierror):
        return f"Сеть: не удалось найти {IMAP_HOST} (DNS). Проверьте интернет на сервере."
    if isinstance(e, ssl.SSLError):
        return f"Сеть: ошибка защищённого соединения с {IMAP_HOST}: {e.__class__.__name__}."
    if isinstance(e, (ConnectionError, OSError)) and not isinstance(e, imaplib.IMAP4.error):
        return f"Сеть: нет соединения с {IMAP_HOST}:{IMAP_PORT} ({e.__class__.__name__})."
    raw = e.args[0] if getattr(e, "args", None) else e
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    low = text.lower()
    if re.search(r"imap.*(disabled|not allowed|запрещ|отключ|выключ)|(disabled|запрещ|отключ).*imap", low):
        return ("Доступ по IMAP выключен в ящике. Включите: почта Mail.ru → Настройки → Все настройки → Безопасность → "
                "Внешние сервисы → «Доступ к почте по IMAP, POP и SMTP».")
    if "parol prilozheniya" in low or "application password" in low:
        return ("Mail.ru не принял пароль: нужен пароль для внешнего приложения (обычный пароль от почты не подходит). "
                "Создайте: Настройки → Все настройки → Безопасность → Пароли для внешних приложений → тип «Полный доступ "
                "к Почте». Если пароль приложения верный — проверьте адрес и что включён «Доступ к почте по IMAP, POP и SMTP».")
    if "authenticationfailed" in low or "authentication failed" in low or "invalid" in low or "login" in low:
        return ("Неверный адрес или пароль приложения. Если всё верно — проверьте, что в Безопасности включён "
                "«Доступ к почте по IMAP, POP и SMTP».")
    if "limit" in low or "too many" in low:
        return "Mail.ru временно ограничил вход (слишком много попыток или соединений). Попробуйте через несколько минут."
    return "Mail.ru отказал во входе: " + text[:200]


def _connect(email_addr: str, password: str):
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    except Exception as e:
        raise MailError(classify_login_error(e))
    try:
        m.login(email_addr, password)
    except Exception as e:
        try:
            m.logout()
        except Exception:
            pass
        raise MailError(classify_login_error(e))
    return m


def _conn(acc: dict):
    """Живое соединение ящика (держим между вызовами, проверяем NOOP)."""
    m = _conns.get(acc["id"])
    if m is not None:
        try:
            if m.noop()[0] == "OK":
                return m
        except Exception:
            pass
        _drop_conn(acc["id"])
    pw = _password(acc["id"])
    if not pw:
        raise MailError("Для ящика не сохранён пароль приложения — добавьте его заново в Настройки → Почта.")
    m = _connect(acc["email"], pw)
    _conns[acc["id"]] = m
    return m


def _drop_conn(acc_id: str):
    m = _conns.pop(acc_id, None)
    if m is not None:
        try:
            m.logout()
        except Exception:
            pass


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def _list_folders(m) -> list:
    typ, data = m.list()
    if typ != "OK":
        raise MailError("Сервер не отдал список папок.")
    out, pending = [], None
    for item in data or []:
        if isinstance(item, tuple):          # имя пришло литералом
            head, lit = item
            mm = _LIST_RE.match(head.strip().rsplit(b"{", 1)[0] + b'""')
            if mm:
                out.append((mm.group("flags").decode(), lit.decode("utf-8", "replace")))
            continue
        if not item:
            continue
        mm = _LIST_RE.match(item.strip())
        if not mm:
            continue
        name = mm.group("name").strip()
        if name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
        out.append((mm.group("flags").decode(), name.decode("utf-8", "replace")))
    folders = []
    for flags, raw in out:
        if "\\noselect" in flags.lower():
            continue
        folders.append({"raw": raw, "display": _FOLDER_RU.get(raw.upper(), decode_mutf7(raw))})
    return folders


def _status(m, raw: str) -> dict:
    typ, data = m.status(_quote(raw), "(MESSAGES UIDNEXT UIDVALIDITY UNSEEN)")
    if typ != "OK" or not data or not data[0]:
        raise MailError(f"STATUS {raw} не прошёл")
    line = data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
    return {k.lower(): int(v) for k, v in re.findall(r"(MESSAGES|UIDNEXT|UIDVALIDITY|UNSEEN)\s+(\d+)", line)}


def _examine(m, raw: str):
    typ, data = m.select(_quote(raw), readonly=True)
    if typ != "OK":
        raise MailError(f"Папка «{decode_mutf7(raw)}» не открылась.")


def _uid_search(m, query: str) -> list:
    typ, data = m.uid("SEARCH", None, query)
    if typ != "OK":
        raise MailError("SEARCH не прошёл")
    return [int(x) for x in b" ".join(d for d in (data or []) if d).split()]


_HEADER_FIELDS = "FROM TO CC SUBJECT DATE CONTENT-TYPE"


def _fetch_groups(data) -> list:
    """Ответ FETCH imaplib → [(метаданные bytes, литерал bytes|None)] по одному на письмо."""
    groups = []
    for item in data or []:
        if isinstance(item, tuple):
            head, lit = item
            if re.match(rb"^\d+ \(", head) or not groups:
                groups.append([head, lit])
            else:
                groups[-1][0] += b" " + head
                groups[-1][1] = lit if groups[-1][1] is None else groups[-1][1]
        elif isinstance(item, bytes):
            if re.match(rb"^\d+ \(", item):
                groups.append([item, None])
            elif groups:
                groups[-1][0] += b" " + item
    return [(g[0], g[1]) for g in groups]


def _unescape_8bit(v: str) -> str:
    """Заголовок с «голой» кириллицей без =?…?= — email отдаёт его с суррогатами."""
    if not re.search(r"[\udc80-\udcff]", v):
        return v
    b = v.encode("utf-8", "surrogateescape")
    for enc in ("utf-8", "cp1251", "koi8-r"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _hget(msg, name):
    """Сырое значение заголовка: msg.get() превращает «голые» 8-битные байты в «�»."""
    for k, v in getattr(msg, "_headers", []):
        if k.lower() == name.lower():
            return v
    return None


def _dec_header(v) -> str:
    if not v:
        return ""
    v = _unescape_8bit(str(v))
    try:
        parts = []
        for chunk, cs in decode_header(str(v)):
            if isinstance(chunk, bytes):
                for enc in (cs, "utf-8", "cp1251"):
                    if not enc:
                        continue
                    try:
                        chunk = chunk.decode(enc)
                        break
                    except (LookupError, UnicodeDecodeError):
                        continue
                else:
                    chunk = chunk.decode("utf-8", "replace")
            parts.append(chunk)
        return re.sub(r"\s+", " ", "".join(parts)).strip()
    except Exception:
        return re.sub(r"\s+", " ", str(v)).strip()


def _addr_list(v) -> str:
    v = _dec_header(v)
    pairs = getaddresses([v]) if v else []
    out = []
    for name, addr in pairs:
        name = name.strip().strip('"')
        out.append(f"{name} <{addr}>" if name and addr else (addr or name))
    return ", ".join(x for x in out if x)


def _date_ts(date_hdr, internal) -> int:
    for src in (date_hdr, internal):
        if not src:
            continue
        try:
            dt = parsedate_to_datetime(src) if src is date_hdr else datetime.strptime(src, "%d-%b-%Y %H:%M:%S %z")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MSK)
            return int(dt.timestamp())
        except Exception:
            continue
    return 0


def _meta_from(head: bytes, lit) -> dict:
    meta_s = head.decode("utf-8", "replace")
    uid = re.search(r"UID (\d+)", meta_s)
    if not uid:
        return None
    flags = re.search(r"FLAGS \(([^)]*)\)", meta_s)
    size = re.search(r"RFC822\.SIZE (\d+)", meta_s)
    internal = re.search(r'INTERNALDATE "([^"]+)"', meta_s)
    msg = email.message_from_bytes(lit or b"")
    ctype = (_hget(msg, "Content-Type") or "").lower()
    return {
        "uid": int(uid.group(1)),
        "from": _addr_list(_hget(msg, "From")),
        "to": _addr_list(_hget(msg, "To")),
        "cc": _addr_list(_hget(msg, "Cc")),
        "subject": _dec_header(_hget(msg, "Subject")),
        "ts": _date_ts(_hget(msg, "Date"), internal.group(1) if internal else None),
        "size": int(size.group(1)) if size else 0,
        "seen": "\\seen" in (flags.group(1).lower() if flags else ""),
        "att": "multipart/mixed" in ctype,
    }


def _fetch_header_set(m, uid_set: str) -> list:
    typ, data = m.uid("FETCH", uid_set, f"(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
    if typ != "OK":
        raise MailError("FETCH заголовков не прошёл")
    return [meta for meta in (_meta_from(h, lit) for h, lit in _fetch_groups(data)) if meta]


def _fetch_headers(m, uids: list) -> list:
    out = []
    for i in range(0, len(uids), FETCH_BATCH):
        out += _fetch_header_set(m, ",".join(str(u) for u in uids[i:i + FETCH_BATCH]))
    return out


def _fetch_raw(m, uid: int) -> bytes:
    typ, data = m.uid("FETCH", str(uid), "(UID RFC822.SIZE BODY.PEEK[])")
    if typ != "OK":
        raise MailError("Письмо не скачалось.")
    for head, lit in _fetch_groups(data):
        if lit is not None:
            return lit
    raise MailError("Письмо не найдено — возможно, его удалили или переместили. Повтори mail_search.")


# ── разбор письма ────────────────────────────────────────────────────────────

class _HtmlText(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.links, self.images = [], 0, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")
        if tag == "a" and (a.get("href") or "").startswith("http"):
            self.links.append(a["href"])
        if tag == "img" and a.get("src"):
            self.images.append({"src": a["src"], "alt": a.get("alt") or ""})

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str):
    p = _HtmlText()
    try:
        p.feed(src or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", src or ""), [], []
    text = "".join(p.out).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, p.links, p.images


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    for enc in (part.get_content_charset(), "utf-8", "cp1251"):
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    plain, htmls, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        fname = _dec_header(part.get_filename()) if part.get_filename() else ""
        is_att = "attachment" in disp or bool(fname) or not ctype.startswith("text/")
        if ctype == "message/rfc822":
            is_att = True
        if not is_att and ctype == "text/plain":
            plain.append(_part_text(part))
        elif not is_att and ctype == "text/html":
            htmls.append(_part_text(part))
        else:
            data = part.get_payload(decode=True) or b""
            if not fname:
                ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "message/rfc822": "eml"}.get(ctype, "bin")
                fname = f"part{len(atts) + 1}.{ext}"
            atts.append({"index": len(atts) + 1, "filename": re.sub(r"[\\/\x00]", "_", fname), "mime": ctype,
                         "size": len(data), "inline": "inline" in disp or bool(part.get("Content-ID")),
                         "_data": data})
    links, images = [], []
    text = "\n\n".join(t.strip() for t in plain if t.strip())
    if htmls:
        h_text, links, images = html_to_text("\n".join(htmls))
        if not text:
            text = h_text
    return {
        "from": _addr_list(_hget(msg, "From")), "to": _addr_list(_hget(msg, "To")), "cc": _addr_list(_hget(msg, "Cc")),
        "subject": _dec_header(_hget(msg, "Subject")), "ts": _date_ts(_hget(msg, "Date"), None),
        "text": text, "links": links[:20],
        "remote_images": [i for i in images if not i["src"].startswith("cid:")][:10],
        "attachments": atts,
    }


# ── индекс и синхронизация ───────────────────────────────────────────────────

def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d %H:%M") if ts else ""


def _norm(s) -> str:
    return (s or "").casefold().replace("ё", "е")


def _snippet(text: str, n=200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def _sync_folder(acc: dict, m, f: dict, st: dict, now: float) -> list:
    """Обновить одну папку в индексе. → новые письма, о которых можно уведомить."""
    idx = _index(acc["id"])
    rec = idx["folders"].setdefault(f["raw"], {"msgs": {}})
    rec["display"] = f["display"]
    rec["messages"], rec["unseen"] = st.get("messages", 0), st.get("unseen", 0)
    fresh = []
    reset = rec.get("uidvalidity") != st.get("uidvalidity")
    if reset or not rec.get("synced"):
        _examine(m, f["raw"])
        uids = _uid_search(m, "ALL")
        rec["msgs"] = {str(x["uid"]): x for x in _fetch_headers(m, uids)}
        rec.update(uidvalidity=st.get("uidvalidity"), uidnext=st.get("uidnext", 0), synced=True, full_at=now)
        return fresh
    if st.get("uidnext", 0) > rec.get("uidnext", 0):
        _examine(m, f["raw"])
        start = rec.get("uidnext") or 1
        # «n:*» при n больше последнего uid по RFC 3501 всё равно отдаёт последнее письмо — отсекаем.
        news = [x for x in _fetch_header_set(m, f"{start}:*") if x["uid"] >= start and str(x["uid"]) not in rec["msgs"]]
        for meta in news:
            if meta["size"] <= 3 * 1024 * 1024:
                try:
                    parsed = parse_message(_fetch_raw(m, meta["uid"]))
                    meta["body"] = parsed["text"][:BODY_CACHE_CHARS]
                    meta["att"] = meta["att"] or any(not a["inline"] for a in parsed["attachments"])
                except Exception:
                    pass
            rec["msgs"][str(meta["uid"])] = meta
            if not meta["seen"]:
                fresh.append(meta)
        rec["uidnext"] = st.get("uidnext", 0)
    if now - rec.get("full_at", 0) >= FULL_REFRESH_SECONDS or len(rec["msgs"]) > st.get("messages", 0):
        _examine(m, f["raw"])
        alive = set(_uid_search(m, "ALL"))
        unseen = set(_uid_search(m, "UNSEEN"))
        rec["msgs"] = {k: v for k, v in rec["msgs"].items() if int(k) in alive}
        for k, v in rec["msgs"].items():
            v["seen"] = int(k) not in unseen
        missing = sorted(alive - {int(k) for k in rec["msgs"]})
        for meta in _fetch_headers(m, missing):
            rec["msgs"][str(meta["uid"])] = meta
        rec["full_at"] = now
    return fresh


def _sync_locked(acc: dict):
    now = time.time()
    idx = _index(acc["id"])
    first = not idx.get("synced_at")
    try:
        m = _conn(acc)
        folders = _list_folders(m)
        alive = {f["raw"] for f in folders}
        for raw in list(idx["folders"]):
            if raw not in alive:
                idx["folders"].pop(raw)
        fresh = []
        for f in folders:
            st = _status(m, f["raw"])
            got = _sync_folder(acc, m, f, st, now)
            if f["raw"] in (acc.get("notifyFolders") or ["INBOX"]):
                fresh += [dict(x, folder=f["display"], folder_raw=f["raw"]) for x in got]
        idx["synced_at"] = now
        _save_index(acc["id"])
        _update_account(acc["id"], status="ok", lastError="", lastSyncAt=int(now * 1000))
    except MailError as e:
        _drop_conn(acc["id"])
        _update_account(acc["id"], status="error", lastError=str(e), lastSyncAt=int(now * 1000))
        return {"ok": False, "error": str(e)}, []
    except (imaplib.IMAP4.abort, OSError) as e:
        _drop_conn(acc["id"])
        err = classify_login_error(e) if isinstance(e, OSError) else "Соединение с Mail.ru оборвалось, повторю на следующей проверке."
        _update_account(acc["id"], status="error", lastError=err, lastSyncAt=int(now * 1000))
        return {"ok": False, "error": err}, []
    except Exception as e:
        _drop_conn(acc["id"])
        _update_account(acc["id"], status="error", lastError=redact(f"{e.__class__.__name__}: {e}"), lastSyncAt=int(now * 1000))
        return {"ok": False, "error": e.__class__.__name__}, []
    return {"ok": True, "new": len(fresh), "first": first}, fresh


def _count(v) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        return sum(_count(x) for x in v.values())
    return 0


def _run_notifiers(acc: dict, fresh: list) -> int:
    """Уведомители — в своём потоке: зависший Telegram или пуш не останавливает проверку почты."""
    sent = []

    def run():
        for fn in list(_notifiers):
            try:
                sent.append(_count(fn(acc, fresh)))
            except Exception as e:
                _log(f"notifier {mask_email(acc.get('email'))}: {e.__class__.__name__}")

    t = threading.Thread(target=run, daemon=True, name="mail-notify")
    t.start()
    t.join(NOTIFY_WAIT)
    if t.is_alive():
        _log(f"notifier {mask_email(acc.get('email'))}: не уложился в {NOTIFY_WAIT} с, дошлёт в фоне")
    return sum(sent)


def sync_account(acc: dict, notify: bool = True, wait=None) -> dict:
    """Один проход по ящику: папки, STATUS, новые письма. wait — сколько ждать, если ящик занят."""
    lock = _acc_lock(acc["id"])
    if not lock.acquire(timeout=-1 if wait is None else wait):
        return {"ok": False, "busy": True, "error": "ящик занят другой операцией"}
    try:
        res, fresh = _sync_locked(acc)
    finally:
        lock.release()
    if res.get("ok"):
        fresh.sort(key=lambda x: x.get("ts", 0))
        res["notified"] = _run_notifiers(acc, fresh) if notify and not res["first"] and fresh else 0
    return res


def add_notifier(fn):
    """fn(account, [письма]) вызывается после проверки, если пришли новые непрочитанные письма.
    Может вернуть число отправленных уведомлений (или словарь чисел) — оно попадёт в лог проверки."""
    if fn not in _notifiers:
        _notifiers.append(fn)


_poller_state = {"gen": 0, "beat": 0.0, "startedAt": 0.0, "lastRoundAt": 0.0, "rounds": 0, "interval": POLL_SECONDS}


def _abort_conn(acc_id: str):
    m = _conns.get(acc_id)
    sock = getattr(m, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


def _sync_with_deadline(acc: dict, notify: bool) -> dict:
    box = {}

    def run():
        try:
            box["res"] = sync_account(acc, notify, wait=LOCK_WAIT)
        except BaseException as e:
            box["res"] = {"ok": False, "error": e.__class__.__name__}

    t = threading.Thread(target=run, daemon=True, name="mail-sync")
    t.start()
    t.join(SYNC_DEADLINE)
    if t.is_alive():
        _abort_conn(acc["id"])
        t.join(ABORT_WAIT)
    return box.get("res") or {"ok": False, "error": f"проверка дольше {SYNC_DEADLINE} с — соединение сброшено"}


def _poll_line(acc: dict, res: dict, secs: float) -> str:
    box = mask_email(acc.get("email"))
    if res.get("busy"):
        return f"{box}: пропуск — ящик занят"
    if not res.get("ok"):
        return f"{box}: ошибка — {str(res.get('error') or '')[:120]} ({secs:.1f} с)"
    first = ", первая синхронизация" if res.get("first") else ""
    return f"{box}: новых {res.get('new', 0)}, уведомлений {res.get('notified', 0)}{first} ({secs:.1f} с)"


def poll_once(notify=True) -> list:
    """Круг фоновой проверки: каждый ящик отдельно — ошибка или зависание одного не трогает остальные."""
    out = []
    try:
        accounts = load_config()["accounts"]
    except Exception as e:
        _log(f"poll: конфиг не прочитан: {e.__class__.__name__}")
        return out
    for acc in accounts:
        _poller_state["beat"] = time.time()
        t0 = time.time()
        try:
            res = _sync_with_deadline(acc, notify)
        except Exception as e:
            res = {"ok": False, "error": e.__class__.__name__}
        try:
            _update_account(acc["id"], lastPollAt=int(time.time() * 1000))
        except Exception as e:
            _log(f"poll: lastPollAt не записан: {e.__class__.__name__}")
        _log(_poll_line(acc, res, time.time() - t0))
        out.append(dict(res, id=acc["id"]))
    _poller_state["beat"] = time.time()
    return out


def _poll_loop(gen: int, interval: int):
    while _poller_state["gen"] == gen:
        _poller_state["beat"] = time.time()
        try:
            poll_once()
            _poller_state["rounds"] += 1
            _poller_state["lastRoundAt"] = time.time()
        except BaseException as e:   # поток проверки не должен умирать ни от чего
            _log(f"poll: {e.__class__.__name__}")
        _poller_state["beat"] = time.time()
        time.sleep(interval)


def poller_alive() -> bool:
    return (_poller is not None and _poller.is_alive()
            and time.time() - _poller_state["beat"] < POLL_STALE_SECONDS)


def start_poller(interval: int = POLL_SECONDS) -> bool:
    """Запустить фоновую проверку; если поток умер или завис — запустить новый. True — если запустили."""
    global _poller
    with _cfg_lock:
        if poller_alive():
            return False
        why = "" if _poller is None else (" (прежний поток завис)" if _poller.is_alive() else " (прежний поток умер)")
        _poller_state["gen"] += 1
        _poller_state.update(beat=time.time(), startedAt=time.time(), interval=interval)
        _poller = threading.Thread(target=_poll_loop, args=(_poller_state["gen"], interval), daemon=True,
                                   name=f"mail-poller-{_poller_state['gen']}")
        _poller.start()
    try:
        n = len(load_config()["accounts"])
    except Exception:
        n = "?"
    _log(f"poller {'restarted' if why else 'started'}{why}, {n} ящиков, раз в {interval} с")
    return True


def ensure_poller() -> bool:
    """Сторож: вызывается из минутного цикла сервера и при открытии вкладки «Почта»."""
    if not _poller_state["gen"]:
        return False
    return start_poller(_poller_state["interval"])


# ── ящики: добавить, проверить, удалить ──────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _public(acc: dict) -> dict:
    idx = _index(acc["id"])
    pw = _password(acc["id"])
    folders = [{"id": raw, "name": r.get("display") or decode_mutf7(raw), "messages": r.get("messages", len(r.get("msgs", {}))),
                "unseen": r.get("unseen", 0), "indexed": len(r.get("msgs", {}))} for raw, r in idx["folders"].items()]
    folders.sort(key=lambda x: (x["id"].upper() != "INBOX", x["name"].casefold()))
    return {"id": acc["id"], "email": acc["email"], "title": acc.get("title") or "",
            "password_mask": ("•" * 8 + f" ({len(pw)} симв.)") if pw else "не задан",
            "status": acc.get("status") or "new", "lastError": acc.get("lastError") or "",
            "lastCheckAt": acc.get("lastCheckAt") or 0, "lastSyncAt": acc.get("lastSyncAt") or 0,
            "lastPollAt": acc.get("lastPollAt") or 0, "addedAt": acc.get("addedAt") or 0,
            "notifyFolders": acc.get("notifyFolders") or ["INBOX"], "push": bool(acc.get("push")), "folders": folders,
            "indexed": sum(f["indexed"] for f in folders), "synced": bool(idx.get("synced_at"))}


def public_status() -> dict:
    try:
        ensure_poller()
    except Exception as e:
        _log(f"watchdog: {e.__class__.__name__}")
    ms = lambda t: int(t * 1000) if t else 0
    return {"ok": True, "host": IMAP_HOST, "poll_seconds": POLL_SECONDS, "now": int(time.time() * 1000),
            "stale_seconds": POLL_STALE_SECONDS,
            "poller": {"alive": poller_alive(), "startedAt": ms(_poller_state["startedAt"]),
                       "lastRoundAt": ms(_poller_state["lastRoundAt"]), "beatAt": ms(_poller_state["beat"])},
            "accounts": [_public(a) for a in load_config()["accounts"]]}


def _background_sync(acc):
    threading.Thread(target=lambda: sync_account(acc, notify=False), daemon=True, name="mail-first-sync").start()


def add_account(email_addr: str, password: str, title: str = "") -> dict:
    email_addr = (email_addr or "").strip().lower()
    password = re.sub(r"\s+", "", password or "")
    if not _EMAIL_RE.match(email_addr):
        return {"ok": False, "error": "Укажите адрес целиком, например name@mail.ru."}
    if not password:
        return {"ok": False, "error": "Укажите пароль для внешнего приложения."}
    try:
        _connect(email_addr, password).logout()
    except MailError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": classify_login_error(e)}
    with _cfg_lock:
        cfg = load_config()
        acc = next((a for a in cfg["accounts"] if a["email"] == email_addr), None)
        if acc is None:
            acc = {"id": uuid.uuid4().hex[:10], "email": email_addr, "addedAt": int(time.time() * 1000),
                   "notifyFolders": ["INBOX"]}
            cfg["accounts"].append(acc)
        acc.update(title=(title or "").strip()[:60], status="ok", lastError="", lastCheckAt=int(time.time() * 1000))
        _save_config(cfg)
        _set_password(acc["id"], password)
    _drop_conn(acc["id"])
    _background_sync(acc)
    return {"ok": True, "account": _public(acc), "message": "Вход выполнен. Индекс писем строится в фоне."}


def _find_account(key) -> dict:
    key = _norm(str(key or "").strip())
    for a in load_config()["accounts"]:
        if key in (_norm(a["id"]), _norm(a["email"]), _norm(a.get("title"))):
            return a
    return None


def check_account(acc_id: str) -> dict:
    acc = _find_account(acc_id)
    if not acc:
        return {"ok": False, "error": "Ящик не найден."}
    pw = _password(acc["id"])
    now = int(time.time() * 1000)
    if not pw:
        return {"ok": False, "error": "Пароль приложения не сохранён — удалите ящик и добавьте заново."}
    try:
        with _acc_lock(acc["id"]):
            _drop_conn(acc["id"])
            m = _connect(acc["email"], pw)
            n = len(_list_folders(m))
            m.logout()
    except MailError as e:
        _update_account(acc["id"], status="error", lastError=str(e), lastCheckAt=now)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        err = classify_login_error(e)
        _update_account(acc["id"], status="error", lastError=err, lastCheckAt=now)
        return {"ok": False, "error": err}
    _update_account(acc["id"], status="ok", lastError="", lastCheckAt=now)
    if not _index(acc["id"]).get("synced_at"):
        _background_sync(acc)
    return {"ok": True, "message": f"Вход выполнен, папок: {n}."}


def update_account(acc_id: str, patch: dict) -> dict:
    acc = _find_account(acc_id)
    if not acc:
        return {"ok": False, "error": "Ящик не найден."}
    upd = {}
    if "title" in patch:
        upd["title"] = str(patch.get("title") or "").strip()[:60]
    if "push" in patch:
        upd["push"] = bool(patch.get("push"))
    if isinstance(patch.get("notifyFolders"), list):
        upd["notifyFolders"] = [str(x) for x in patch["notifyFolders"]][:50]
    if patch.get("password"):
        pw = re.sub(r"\s+", "", patch["password"])
        try:
            _connect(acc["email"], pw).logout()
        except MailError as e:
            return {"ok": False, "error": str(e)}
        _set_password(acc["id"], pw)
        _drop_conn(acc["id"])
        upd.update(status="ok", lastError="", lastCheckAt=int(time.time() * 1000))
    acc = _update_account(acc["id"], **upd)
    return {"ok": True, "account": _public(acc)}


def delete_account(acc_id: str) -> dict:
    acc = _find_account(acc_id)
    if not acc:
        return {"ok": False, "error": "Ящик не найден."}
    with _acc_lock(acc["id"]):
        _drop_conn(acc["id"])
        with _cfg_lock:
            cfg = load_config()
            cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != acc["id"]]
            _save_config(cfg)
            _set_password(acc["id"], None)
            _indexes.pop(acc["id"], None)
            try:
                index_path(acc["id"]).unlink()
            except FileNotFoundError:
                pass
    return {"ok": True, "id": acc["id"]}


# ── инструменты ──────────────────────────────────────────────────────────────

def _accounts_for(key, allowed) -> list:
    accs = [a for a in load_config()["accounts"] if allowed is None or a["id"] in allowed]
    if key:
        k = _norm(str(key).strip())
        accs = [a for a in accs if k in (_norm(a["id"]), _norm(a["email"]), _norm(a.get("title")))]
        if not accs:
            raise MailError(f"Ящик «{key}» не найден среди подключённых.")
    if not accs:
        raise MailError("Нет подключённых ящиков Mail.ru (или они недоступны в этом чате). Подключить: Настройки → Почта.")
    return accs


def _folder_raw(acc: dict, folder) -> str:
    idx = _index(acc["id"])
    k = _norm(str(folder or "").strip())
    for raw, r in idx["folders"].items():
        if k in (_norm(raw), _norm(r.get("display"))):
            return raw
    if k in ("inbox", "входящие"):
        return "INBOX"
    raise MailError(f"Папка «{folder}» не найдена в {acc['email']}. Папки — в mail_accounts.")


def _date_bound(s, end=False):
    if not s:
        return None
    try:
        d = datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=MSK)
    except ValueError:
        raise MailError("Дата — в формате YYYY-MM-DD.")
    return int((d + timedelta(days=1)).timestamp()) - 1 if end else int(d.timestamp())


def _item(acc, raw, rec, meta) -> dict:
    return {"account": acc["email"], "folder": rec.get("display") or decode_mutf7(raw), "uid": meta["uid"],
            "date": _fmt_ts(meta.get("ts")), "from": meta.get("from", ""),
            "to": ", ".join(x for x in (meta.get("to"), meta.get("cc")) if x),
            "subject": meta.get("subject") or "(без темы)", "size": meta.get("size", 0),
            "unread": not meta.get("seen"), "has_attachments": bool(meta.get("att")),
            "snippet": _snippet(meta.get("body"), 160)}


def tool_accounts(allowed=None) -> dict:
    accs = [a for a in load_config()["accounts"] if allowed is None or a["id"] in allowed]
    if not accs:
        return {"accounts": [], "note": "Ящиков нет. Подключить: Настройки → Почта."}
    out = []
    for a in accs:
        p = _public(a)
        out.append({"account": p["email"], "id": p["id"], "title": p["title"], "status": p["status"],
                    "error": p["lastError"] or None, "indexed": p["indexed"],
                    "index_updated": _fmt_ts(int(p["lastSyncAt"] / 1000)) if p["lastSyncAt"] else "ещё не построен",
                    "folders": [{"folder": f["name"], "messages": f["messages"], "unread": f["unseen"]} for f in p["folders"]]})
    return {"accounts": out}


def _refresh_unseen(acc, raws):
    """Перед фильтром по «непрочитанным» — свежие отметки с сервера (UID SEARCH UNSEEN работает у Mail.ru)."""
    try:
        with _acc_lock(acc["id"]):
            m = _conn(acc)
            idx = _index(acc["id"])
            for raw in raws:
                _examine(m, raw)
                unseen = set(_uid_search(m, "UNSEEN"))
                for k, v in idx["folders"].get(raw, {}).get("msgs", {}).items():
                    v["seen"] = int(k) not in unseen
    except Exception as e:
        _log(f"unseen refresh: {e.__class__.__name__}")


def _deep_scan(acc, raw, metas):
    """Докачать текст писем без текста в индексе (поиск по тексту)."""
    done = 0
    with _acc_lock(acc["id"]):
        m = _conn(acc)
        _examine(m, raw)
        for meta in metas:
            try:
                if meta.get("size", 0) > 5 * 1024 * 1024:
                    meta["body"] = ""
                    continue
                meta["body"] = parse_message(_fetch_raw(m, meta["uid"]))["text"][:BODY_CACHE_CHARS]
                done += 1
            except MailError:
                meta["body"] = ""
    return done


def tool_search(inp: dict, allowed=None) -> dict:
    accs = _accounts_for(inp.get("account"), allowed)
    f_from, f_to = _norm(inp.get("from")), _norm(inp.get("to"))
    f_subj, f_text = _norm(inp.get("subject")), _norm(inp.get("text"))
    d_from, d_to = _date_bound(inp.get("date_from")), _date_bound(inp.get("date_to"), end=True)
    unread, has_att = inp.get("unread"), inp.get("has_attachments")
    rows, pending_text, warnings = [], [], []
    for acc in accs:
        idx = _index(acc["id"])
        if not idx.get("synced_at"):
            warnings.append(f"{acc['email']}: индекс ещё строится — результаты неполные.")
        raws = [_folder_raw(acc, inp["folder"])] if inp.get("folder") else list(idx["folders"])
        if unread is not None:
            _refresh_unseen(acc, raws)
        for raw in raws:
            rec = idx["folders"].get(raw) or {}
            for meta in rec.get("msgs", {}).values():
                ts = meta.get("ts", 0)
                if d_from and ts < d_from or d_to and ts > d_to:
                    continue
                if unread is not None and bool(unread) == bool(meta.get("seen")):
                    continue
                if has_att is not None and bool(has_att) != bool(meta.get("att")):
                    continue
                if f_from and f_from not in _norm(meta.get("from")):
                    continue
                if f_to and f_to not in _norm((meta.get("to") or "") + " " + (meta.get("cc") or "")):
                    continue
                if f_subj and f_subj not in _norm(meta.get("subject")):
                    continue
                if f_text:
                    hay = _norm(" ".join(meta.get(k) or "" for k in ("subject", "from", "to", "cc", "body")))
                    if f_text not in hay:
                        if "body" not in meta:
                            pending_text.append((acc, raw, rec, meta))
                        continue
                rows.append((acc, raw, rec, meta))
    scanned = 0
    if pending_text:
        pending_text.sort(key=lambda r: -r[3].get("ts", 0))
        batch, rest = pending_text[:DEEP_SCAN_LIMIT], pending_text[DEEP_SCAN_LIMIT:]
        by_folder = {}
        for r in batch:
            by_folder.setdefault((r[0]["id"], r[1]), []).append(r)
        for (_, raw), group in by_folder.items():
            try:
                scanned += _deep_scan(group[0][0], raw, [g[3] for g in group])
            except MailError as e:
                warnings.append(str(e))
        for acc, raw, rec, meta in batch:
            if f_text in _norm(meta.get("body")):
                rows.append((acc, raw, rec, meta))
        for acc_id in {r[0]["id"] for r in batch}:
            _save_index(acc_id)
        if rest:
            warnings.append(f"Текст ещё не проверен у {len(rest)} старых писем — сузь даты или папку и повтори.")
    sort = inp.get("sort") or "date"
    order = inp.get("order") or ("asc" if sort == "from" else "desc")
    key = {"date": lambda r: r[3].get("ts", 0), "size": lambda r: r[3].get("size", 0),
           "from": lambda r: _norm(r[3].get("from"))}.get(sort, lambda r: r[3].get("ts", 0))
    rows.sort(key=key, reverse=(order == "desc"))
    limit = max(1, min(int(inp.get("limit") or 20), 50))
    offset = max(0, int(inp.get("offset") or 0))
    page = rows[offset:offset + limit]
    out = {"total": len(rows), "offset": offset, "items": [_item(*r) for r in page],
           "next_offset": offset + limit if offset + limit < len(rows) else None}
    if scanned:
        out["text_scanned"] = scanned
    if warnings:
        out["warnings"] = warnings
    return out


def _load_message(inp, allowed):
    acc = _accounts_for(inp.get("account"), allowed)[0]
    raw_folder = _folder_raw(acc, inp.get("folder"))
    try:
        uid = int(inp.get("uid"))
    except (TypeError, ValueError):
        raise MailError("uid — число из mail_search.")
    with _acc_lock(acc["id"]):
        m = _conn(acc)
        _examine(m, raw_folder)
        raw = _fetch_raw(m, uid)
    if len(raw) > MAX_RAW_BYTES:
        raise MailError("Письмо больше 30 МБ — открыть не могу.")
    return acc, raw_folder, uid, parse_message(raw)


def tool_read(inp: dict, allowed=None) -> dict:
    acc, raw_folder, uid, p = _load_message(inp, allowed)
    meta = _index(acc["id"])["folders"].get(raw_folder, {}).get("msgs", {}).get(str(uid))
    if meta is not None:
        meta["body"] = p["text"][:BODY_CACHE_CHARS]
        _save_index(acc["id"])
    offset = max(0, int(inp.get("offset") or 0))
    text = p["text"][offset:offset + MAX_TEXT]
    out = {"account": acc["email"], "folder": _index(acc["id"])["folders"].get(raw_folder, {}).get("display") or raw_folder,
           "uid": uid, "date": _fmt_ts(p["ts"]), "from": p["from"], "to": p["to"], "cc": p["cc"] or None,
           "subject": p["subject"] or "(без темы)", "text": text or "(текста нет)",
           "attachments": [{k: v for k, v in a.items() if k != "_data"} for a in p["attachments"]]}
    if offset + MAX_TEXT < len(p["text"]):
        out["next_offset"] = offset + MAX_TEXT
    if p["links"]:
        out["links"] = p["links"]
    if p["remote_images"]:
        out["remote_images"] = p["remote_images"]
    if any(a["mime"].startswith("image/") for a in p["attachments"]):
        out["note"] = "Картинки — в attachments (mime image/*): mail_attachment action=read покажет картинку, send пришлёт в чат."
    return out


_IMG_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def tool_attachment(inp: dict, allowed=None, ctx=None, tg_bot=None) -> dict:
    acc, raw_folder, uid, p = _load_message(inp, allowed)
    try:
        n = int(inp.get("index"))
    except (TypeError, ValueError):
        raise MailError("index — номер вложения из mail_read.")
    att = next((a for a in p["attachments"] if a["index"] == n), None)
    if not att:
        raise MailError(f"Вложения №{n} нет. Список — mail_read.attachments.")
    data, name = att["_data"], att["filename"]
    info = {"account": acc["email"], "uid": uid, "index": n, "filename": name, "mime": att["mime"], "size": len(data)}
    if not data:
        raise MailError("Вложение пустое.")
    action = inp.get("action") or "read"
    if action == "send":
        if not (ctx and tg_bot):
            return dict(info, error="Прислать файл можно только в чате Telegram с ботом.")
        kind = "photo" if att["mime"] in ("image/jpeg", "image/png") and len(data) < 10_000_000 else "document"
        res = tg_bot.file_send(ctx, filename=name, content_base64=base64.b64encode(data).decode(), kind=kind,
                               caption=f"Из письма «{(p['subject'] or '')[:80]}»")
        if res.get("ok"):
            tg_bot.remember_file(ctx.chat_id, name, data)
        return dict(info, **res)
    if ctx and tg_bot:
        tg_bot.remember_file(ctx.chat_id, name, data)
    if att["mime"] in _IMG_MIME:
        if len(data) > MAX_IMAGE_BYTES:
            return dict(info, note="Картинка слишком большая, чтобы посмотреть, — могу прислать её файлом (action=send).")
        return dict(info, note="Картинка приложена к этому ответу — посмотри её.",
                    _image={"media_type": att["mime"], "b64": base64.b64encode(data).decode()})
    try:
        offset = max(0, int(inp.get("offset") or 0))
        r = file_tools.read_file(data, name, offset + MAX_TEXT)
    except file_tools.FileToolError as e:
        return dict(info, error=f"Не прочитал: {e}. Могу прислать файл в чат (action=send).")
    text = r["text"][offset:]
    out = dict(info, format=r["format"], text=text)
    if r["truncated"]:
        out["next_offset"] = offset + MAX_TEXT
    return out


def execute(name: str, inp: dict, state=None, allowed=None, ctx=None, tg_bot=None) -> dict:
    """allowed — множество id ящиков, доступных в этом чате; None — все (сайт, владелец)."""
    if not skill_on(state):
        return {"error": "Навык «Почта» выключен: Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    try:
        if name == "mail_accounts":
            return tool_accounts(allowed)
        if name == "mail_search":
            return tool_search(inp, allowed)
        if name == "mail_read":
            return tool_read(inp, allowed)
        if name == "mail_attachment":
            return tool_attachment(inp, allowed, ctx, tg_bot)
    except MailError as e:
        return {"error": str(e)}
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError) as e:
        acc_key = inp.get("account")
        acc = _find_account(acc_key) if acc_key else None
        if acc:
            _drop_conn(acc["id"])
        return {"error": "Mail.ru не ответил: " + (classify_login_error(e) if isinstance(e, OSError) else redact(e))}
    return {"error": f"неизвестный инструмент {name}"}


def notification_text(acc: dict, meta: dict) -> str:
    box = html.escape(acc.get("title") or acc["email"])
    lines = [f"📬 <b>{box}</b> · {html.escape(meta.get('folder') or 'Входящие')}",
             f"<b>{html.escape(meta.get('from') or '—')}</b>",
             html.escape(meta.get("subject") or "(без темы)")]
    snip = _snippet(meta.get("body"), 220)
    if snip:
        lines.append(f"<i>{html.escape(snip)}</i>")
    if meta.get("att"):
        lines.append("📎 есть вложения")
    return "\n".join(lines)


def push_body(meta: dict) -> str:
    parts = [meta.get("from") or "—", meta.get("subject") or "(без темы)", _snippet(meta.get("body"), 160)]
    return "\n".join(p for p in parts if p)


def notification_context(acc: dict, meta: dict) -> str:
    return (f"Пришло новое письмо: ящик {acc['email']}, папка «{meta.get('folder') or 'Входящие'}», uid {meta['uid']}, "
            f"от {meta.get('from') or '—'}, тема «{meta.get('subject') or '(без темы)'}». "
            "Если пользователь спросит о нём — открой mail_read с этими account/folder/uid.")
