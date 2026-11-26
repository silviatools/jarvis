"""Резидентские парковочные разрешения своих автомобилей.

Карточка авто (название, номер, марка, модель) живёт в общих данных приложения,
раздел «Автомобили». Разрешения, напоминания и сканы — здесь:

  DATA_DIR/jarvis_cars.json   — разрешения
  DATA_DIR/jarvis_cars/       — фото и PDF

Напоминание: либо дата, либо за сколько дней до окончания, плюс время (МСК)
и текст. Минутный цикл забирает просроченные в окне 6 часов и шлёт текст
владельцу в Telegram. Повторно то же напоминание не уходит.
Отдельные плановые задачи для этих слотов не нужны.

Ассистент пишет в тот же файл инструментами parking_permit_*: навык
«Резидентские разрешения». Заметки бокса для этого не используются.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

STORE_NAME = "jarvis_cars.json"
FILES_DIR_NAME = "jarvis_cars"
MAX_PERMITS = 200
MAX_REMINDERS = 12
MAX_FILES = 12
MAX_FILE_BYTES = 20 * 1024 * 1024
GRACE_HOURS = 6
SOON_DAYS = 30

MSK = timezone(timedelta(hours=3))
_lock = threading.RLock()

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_NUMBER_RE = re.compile(r"^[\w\s./№-]{1,40}$", re.UNICODE)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EXT_MAGIC = {
    "jpg": (lambda b: b.startswith(b"\xff\xd8\xff"), "image/jpeg"),
    "jpeg": (lambda b: b.startswith(b"\xff\xd8\xff"), "image/jpeg"),
    "png": (lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"), "image/png"),
    "gif": (lambda b: b.startswith((b"GIF87a", b"GIF89a")), "image/gif"),
    "webp": (lambda b: len(b) >= 12 and b.startswith(b"RIFF") and b[8:12] == b"WEBP", "image/webp"),
    "pdf": (lambda b: b.startswith(b"%PDF"), "application/pdf"),
    "heic": (lambda b: b"ftyp" in b[:32], "image/heic"),
}

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(MSK)


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def files_dir() -> Path:
    return data_dir() / FILES_DIR_NAME


def _load() -> dict:
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    permits = raw.get("permits") if isinstance(raw, dict) else None
    if not isinstance(permits, list):
        permits = []
    return {"permits": [p for p in permits if isinstance(p, dict) and p.get("id")]}


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _clean(value, limit: int, newlines: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if not newlines:
        text = text.replace("\n", " ")
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return text[:limit]


def _parse_date(value):
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _as_int(value):
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _clock_field(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text if _TIME_RE.match(text) else None


def _money_field(value):
    """Сумма в рублях: None — пусто, False — не разобрать, иначе целое."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return False
        number = int(value)
    else:
        text = str(value).replace("\u00a0", "").replace(" ", "").replace("₽", "")
        text = text.replace("руб.", "").replace("руб", "").replace(",", ".")
        if not re.fullmatch(r"\d+(?:\.0+)?", text or ""):
            return False
        number = int(float(text))
    if number < 0 or number > 100_000_000:
        return False
    return number


def _uin_field(value):
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ""
    if not re.fullmatch(r"\d{1,40}", text):
        return None
    return text


def _payload_has(payload, *keys):
    for key in keys:
        if key in payload:
            return key
    return None


def human_date(iso: str) -> str:
    day = _parse_date(iso)
    if not day:
        return str(iso or "")
    return f"{day.day} {_MONTHS[day.month]} {day.year}"


def _fire_at(permit: dict, reminder: dict):
    clock = str(reminder.get("time") or "10:00")
    if not _TIME_RE.match(clock):
        return None
    hour, minute = (int(x) for x in clock.split(":"))
    mode = reminder.get("mode")
    if mode == "date":
        day = _parse_date(reminder.get("date"))
    elif mode == "days":
        end = _parse_date(permit.get("ends"))
        days = _as_int(reminder.get("daysBefore"))
        if end is None or days is None:
            return None
        day = end - timedelta(days=days)
    else:
        return None
    if day is None:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)


def _slot(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _sent_key(reminder_id: str, slot: str) -> str:
    return f"{reminder_id}|{slot}"


def phase(permit: dict, today=None) -> str:
    if permit.get("revoked"):
        return "revoked"
    end = _parse_date(permit.get("ends"))
    today = today or now_msk().date()
    if end is None:
        return "active"
    if end < today:
        return "expired"
    if (end - today).days <= SOON_DAYS:
        return "soon"
    return "active"


def public_permit(permit: dict, today=None) -> dict:
    today = today or now_msk().date()
    sent = set(permit.get("sent") or [])
    reminders = []
    for reminder in permit.get("reminders") or []:
        fire = _fire_at(permit, reminder)
        slot = _slot(fire) if fire else ""
        reminders.append({
            "id": reminder.get("id"),
            "mode": reminder.get("mode"),
            "daysBefore": reminder.get("daysBefore") if reminder.get("mode") == "days" else None,
            "date": reminder.get("date") if reminder.get("mode") == "date" else "",
            "time": reminder.get("time") or "10:00",
            "text": reminder.get("text") or "",
            "firesAt": slot,
            "sent": bool(slot and _sent_key(reminder.get("id"), slot) in sent),
        })
    files = [{
        "id": f.get("id"),
        "name": f.get("name") or "файл",
        "size": f.get("size") or 0,
        "mime": f.get("mime") or "application/octet-stream",
    } for f in (permit.get("files") or [])]
    return {
        "id": permit.get("id"),
        "carId": permit.get("carId"),
        "number": permit.get("number") or "",
        "starts": permit.get("starts") or "",
        "ends": permit.get("ends") or "",
        "zone": permit.get("zone") or "",
        "address": permit.get("address") or "",
        "spot": permit.get("spot") or "",
        "holder": permit.get("holder") or "",
        "notes": permit.get("notes") or "",
        "freeFrom": permit.get("freeFrom") or "",
        "freeUntil": permit.get("freeUntil") or "",
        "fee": permit.get("fee") if isinstance(permit.get("fee"), int) else None,
        "uin": permit.get("uin") or "",
        "revoked": bool(permit.get("revoked")),
        "phase": phase(permit, today),
        "files": files,
        "reminders": reminders,
        "updatedAt": permit.get("updatedAt") or 0,
    }


def for_assistant() -> list:
    """Снимок для ассистента: без путей на диске."""
    return [public_permit(p) for p in _load()["permits"]]


def view() -> dict:
    return {"ok": True, "permits": for_assistant()}


def _reminders(raw, existing):
    if raw is None and existing is not None:
        return [dict(r) for r in (existing.get("reminders") or [])], None
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, "Напоминания — список"
    if len(raw) > MAX_REMINDERS:
        return None, "Слишком много напоминаний"
    seen = set()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "Напоминание задано неверно"
        mode = item.get("mode")
        if mode not in ("date", "days"):
            return None, "Напоминание: укажите дату или за сколько дней"
        clock = str(item.get("time") or "10:00").strip()
        if not _TIME_RE.match(clock):
            return None, "Время напоминания — ЧЧ:ММ"
        rec = {
            "id": str(item.get("id") or "").strip() or str(uuid.uuid4()),
            "mode": mode,
            "time": clock,
            "text": _clean(item.get("text"), 1000, newlines=True),
        }
        if not _ID_RE.match(rec["id"]):
            rec["id"] = str(uuid.uuid4())
        if rec["id"] in seen:
            rec["id"] = str(uuid.uuid4())
        seen.add(rec["id"])
        if mode == "days":
            days = _as_int(item.get("daysBefore"))
            if days is None or days < 0 or days > 3650:
                return None, "«За сколько дней» — число от 0 до 3650"
            rec["daysBefore"] = days
            rec["date"] = ""
        else:
            day = _parse_date(item.get("date"))
            if not day:
                return None, "Укажите дату напоминания"
            rec["date"] = day.isoformat()
            rec["daysBefore"] = None
        out.append(rec)
    return out, None


def save_permit(payload: dict):
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "Ожидался объект"}
    if "zone" not in payload and "district" in payload:
        payload = {**payload, "zone": payload.get("district")}
    with _lock:
        data = _load()
        permits = data["permits"]
        pid = str(payload.get("id") or "").strip()
        existing = None
        if pid:
            if not _ID_RE.match(pid):
                return 400, {"ok": False, "error": "Некорректный id"}
            existing = next((p for p in permits if p.get("id") == pid), None)
            if existing is None and len(permits) >= MAX_PERMITS:
                return 400, {"ok": False, "error": "Слишком много разрешений"}
        else:
            if len(permits) >= MAX_PERMITS:
                return 400, {"ok": False, "error": "Слишком много разрешений"}
            pid = str(uuid.uuid4())
        base = existing or {}
        car_id = str(payload.get("carId") if "carId" in payload else base.get("carId") or "").strip()
        if not _ID_RE.match(car_id):
            return 400, {"ok": False, "error": "Выберите автомобиль"}
        number = _clean(payload.get("number") if "number" in payload else base.get("number"), 40)
        if number and not _NUMBER_RE.match(number):
            return 400, {"ok": False, "error": "Укажите номер разрешения"}
        starts = _parse_date(payload.get("starts") if "starts" in payload else base.get("starts"))
        ends = _parse_date(payload.get("ends") if "ends" in payload else base.get("ends"))
        if not starts or not ends:
            return 400, {"ok": False, "error": "Укажите даты начала и окончания"}
        if ends < starts:
            return 400, {"ok": False, "error": "Окончание раньше начала"}
        reminders, err = _reminders(payload.get("reminders") if "reminders" in payload else None, existing)
        if err:
            return 400, {"ok": False, "error": err}
        free_key = _payload_has(payload, "freeFrom", "free_from")
        until_key = _payload_has(payload, "freeUntil", "free_until")
        if free_key:
            free_from = _clock_field(payload.get(free_key))
            if free_from is None:
                return 400, {"ok": False, "error": "Начало бесплатной парковки — ЧЧ:ММ"}
        else:
            free_from = str(base.get("freeFrom") or "")
        if until_key:
            free_until = _clock_field(payload.get(until_key))
            if free_until is None:
                return 400, {"ok": False, "error": "Конец бесплатной парковки — ЧЧ:ММ"}
        else:
            free_until = str(base.get("freeUntil") or "")
        if bool(free_from) != bool(free_until):
            return 400, {"ok": False, "error": "Укажите оба времени бесплатной парковки"}
        if _payload_has(payload, "fee"):
            fee = _money_field(payload.get("fee"))
            if fee is False:
                return 400, {"ok": False, "error": "Плата — сумма в рублях"}
        else:
            fee = base.get("fee") if isinstance(base.get("fee"), int) else None
        if _payload_has(payload, "uin"):
            uin = _uin_field(payload.get("uin"))
            if uin is None:
                return 400, {"ok": False, "error": "УИН — только цифры"}
        else:
            uin = str(base.get("uin") or "")
        if not number and not uin:
            return 400, {"ok": False, "error": "Укажите номер разрешения или УИН"}
        if "revoked" in payload:
            revoked = payload.get("revoked")
            revoked = revoked if isinstance(revoked, bool) else str(revoked).lower() in ("1", "true", "yes", "on")
        else:
            revoked = bool(base.get("revoked"))
        rec = {
            "id": pid,
            "carId": car_id,
            "number": number,
            "starts": starts.isoformat(),
            "ends": ends.isoformat(),
            "zone": _clean(payload.get("zone") if "zone" in payload else base.get("zone"), 80),
            "address": _clean(payload.get("address") if "address" in payload else base.get("address"), 200),
            "spot": _clean(payload.get("spot") if "spot" in payload else base.get("spot"), 40),
            "holder": _clean(payload.get("holder") if "holder" in payload else base.get("holder"), 80),
            "notes": _clean(payload.get("notes") if "notes" in payload else base.get("notes"), 1000, newlines=True),
            "freeFrom": free_from,
            "freeUntil": free_until,
            "fee": fee,
            "uin": uin,
            "revoked": revoked,
            "files": list(base.get("files") or []),
            "reminders": reminders,
            "sent": list(base.get("sent") or []),
            "updatedAt": int(time.time() * 1000),
        }
        if existing:
            for i, item in enumerate(permits):
                if item.get("id") == pid:
                    permits[i] = rec
                    break
        else:
            permits.append(rec)
        _save(data)
        return 200, {"ok": True, "permit": public_permit(rec)}


def _unlink(file_rec: dict) -> None:
    ext = str(file_rec.get("ext") or "")
    fid = str(file_rec.get("id") or "")
    if not _ID_RE.match(fid) or ext not in _EXT_MAGIC:
        return
    path = files_dir() / f"{fid}.{ext}"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _drop(permit: dict) -> None:
    for file_rec in permit.get("files") or []:
        _unlink(file_rec)


def delete_permit(permit_id: str):
    if not _ID_RE.match(str(permit_id or "")):
        return 400, {"ok": False, "error": "Некорректный id"}
    with _lock:
        data = _load()
        found = next((p for p in data["permits"] if p.get("id") == permit_id), None)
        if not found:
            return 404, {"ok": False, "error": "Разрешение не найдено"}
        _drop(found)
        data["permits"] = [p for p in data["permits"] if p.get("id") != permit_id]
        _save(data)
        return 200, {"ok": True}


def purge_car(car_id: str) -> dict:
    """Удалить все разрешения автомобиля вместе с файлами."""
    if not _ID_RE.match(str(car_id or "")):
        return {"ok": False, "error": "Некорректный id", "removed": 0}
    with _lock:
        data = _load()
        gone = [p for p in data["permits"] if p.get("carId") == car_id]
        for permit in gone:
            _drop(permit)
        data["permits"] = [p for p in data["permits"] if p.get("carId") != car_id]
        if gone:
            _save(data)
        return {"ok": True, "removed": len(gone)}


def save_file(permit_id: str, name: str, ext: str, body: bytes):
    ext = str(ext or "").lower().strip().lstrip(".")
    if ext not in _EXT_MAGIC:
        return 400, {"ok": False, "error": "Нужно фото или PDF"}
    if not body:
        return 400, {"ok": False, "error": "Пустой файл"}
    if len(body) > MAX_FILE_BYTES:
        return 413, {"ok": False, "error": "Файл больше 20 МБ"}
    check, mime = _EXT_MAGIC[ext]
    if not check(body):
        return 400, {"ok": False, "error": "Файл не похож на фото или PDF"}
    if not _ID_RE.match(str(permit_id or "")):
        return 400, {"ok": False, "error": "Некорректный id"}
    safe_name = Path(str(name or "scan")).name
    safe_name = _clean(safe_name, 120) or "scan"
    with _lock:
        data = _load()
        permit = next((p for p in data["permits"] if p.get("id") == permit_id), None)
        if not permit:
            return 404, {"ok": False, "error": "Разрешение не найдено"}
        files = list(permit.get("files") or [])
        if len(files) >= MAX_FILES:
            return 400, {"ok": False, "error": "К разрешению можно приложить не больше 12 файлов"}
        fid = str(uuid.uuid4())
        dest_dir = files_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"{fid}.{ext}").write_bytes(body)
        files.append({"id": fid, "name": safe_name, "ext": ext, "size": len(body), "mime": mime})
        permit["files"] = files
        permit["updatedAt"] = int(time.time() * 1000)
        _save(data)
        return 200, {"ok": True, "file": {"id": fid, "name": safe_name, "size": len(body), "mime": mime},
                     "permit": public_permit(permit)}


def open_file(file_id: str):
    if not _ID_RE.match(str(file_id or "")):
        return None
    for permit in _load()["permits"]:
        for file_rec in permit.get("files") or []:
            if file_rec.get("id") == file_id:
                ext = str(file_rec.get("ext") or "")
                path = files_dir() / f"{file_id}.{ext}"
                if ext not in _EXT_MAGIC or not path.is_file():
                    return None
                return path, file_rec.get("mime") or "application/octet-stream", file_rec.get("name") or path.name
    return None


def remove_file(file_id: str):
    if not _ID_RE.match(str(file_id or "")):
        return 400, {"ok": False, "error": "Некорректный id"}
    with _lock:
        data = _load()
        for permit in data["permits"]:
            files = list(permit.get("files") or [])
            found = next((f for f in files if f.get("id") == file_id), None)
            if not found:
                continue
            _unlink(found)
            permit["files"] = [f for f in files if f.get("id") != file_id]
            permit["updatedAt"] = int(time.time() * 1000)
            _save(data)
            return 200, {"ok": True, "permit": public_permit(permit)}
        return 404, {"ok": False, "error": "Файл не найден"}


def partition(now: datetime = None):
    """Напоминания к отправке (окно 6 часов) и уже безнадёжно пропущенные."""
    now = now or now_msk()
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    grace = timedelta(hours=GRACE_HOURS)
    due, missed = [], []
    for permit in _load()["permits"]:
        if permit.get("revoked"):
            continue
        sent = set(permit.get("sent") or [])
        for reminder in permit.get("reminders") or []:
            fire = _fire_at(permit, reminder)
            if fire is None:
                continue
            slot = _slot(fire)
            if _sent_key(reminder.get("id"), slot) in sent:
                continue
            item = {
                "permitId": permit.get("id"),
                "reminderId": reminder.get("id"),
                "slot": slot,
                "permit": permit,
                "reminder": reminder,
            }
            if fire <= now < fire + grace:
                due.append(item)
            elif now >= fire + grace:
                missed.append(item)
    return due, missed


def mark_done(permit_id: str, reminder_id: str, slot: str) -> bool:
    key = _sent_key(reminder_id, slot)
    with _lock:
        data = _load()
        for permit in data["permits"]:
            if permit.get("id") != permit_id:
                continue
            sent = list(permit.get("sent") or [])
            if key not in sent:
                sent.append(key)
            permit["sent"] = sent[-80:]
            _save(data)
            return True
        return False


def render(permit: dict, reminder: dict, car: dict = None) -> str:
    import html as html_mod
    car = car or {}
    name = _clean(car.get("name"), 80)
    make = _clean(car.get("make"), 40)
    model = _clean(car.get("model"), 40)
    plate = _clean(car.get("plate"), 20)
    who = name or " ".join(x for x in (make, model) if x) or "автомобиль"
    if plate:
        who = f"{who} · {plate}"
    number = permit.get("number") or "—"
    end = human_date(permit.get("ends") or "")
    custom = _clean(reminder.get("text"), 1000, newlines=True)
    if not custom:
        custom = f"Резидентское разрешение №{number} заканчивается {end}."
    return "\n".join([
        "🅿️ <b>Парковка — напоминание</b>",
        "",
        html_mod.escape(custom),
        "",
        f"<i>{html_mod.escape(who)}</i>",
        f"<i>Разрешение {html_mod.escape(str(number))}, до {html_mod.escape(end)}</i>",
    ])


def recipients(owner_id, chat_ids, routed_ids) -> list:
    """Сначала владелец личного Telegram, затем чаты с категорией «Парковка».
    Если владельца нет и подписчик один — ему."""
    out = []

    def add(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return
        if number not in out:
            out.append(number)

    add(owner_id)
    for cid in routed_ids or []:
        add(cid)
    chats = list(chat_ids or [])
    if not out and len(chats) == 1:
        add(chats[0])
    return out


# ── навык ассистента ─────────────────────────────────────────────────────────

SKILL_ID = "parking_permits"

SKILLS = [
    {"id": SKILL_ID, "title": "Резидентские разрешения", "risk": "write", "default": True,
     "desc": ("Ассистент сам заносит резидентское парковочное разрешение во вкладку «Разрешения» "
              "раздела «Автомобили»: номер, район, срок, часы бесплатной парковки, годовая плата, УИН, "
              "файл из уже присланного вложения и несколько напоминаний. Привязывает к боксу по госномеру "
              "или имени. В заметки автомобиля не пишет и не предлагает сделать это на ПК. "
              "Напоминание — дата или за сколько дней до окончания, московское время, текст владельцу "
              "в Telegram: это те же слоты, что в карточке разрешения. Отдельную плановую задачу на тот же "
              "срок не ставит. Чтение сразу. Запись и правка сразу. Удаление разрешения вместе с файлами — "
              "только после подтверждения."),
     "tools": ["parking_permit_list", "parking_permit_create", "parking_permit_update", "parking_permit_cancel"]},
]


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


_STR = {"type": "string"}
_REMINDER = {"type": "object", "properties": {
    "mode": {"type": "string", "enum": ["date", "days"],
             "description": "date — в конкретную дату; days — за сколько дней до окончания"},
    "date": {"type": "string", "description": "YYYY-MM-DD или ДД.ММ.ГГГГ, если mode=date"},
    "daysBefore": {"type": "integer", "description": "дней до окончания, если mode=days; 0 — в день окончания"},
    "time": {"type": "string", "description": "ЧЧ:ММ, московское, по умолчанию 10:00"},
    "text": {"type": "string", "description": "текст владельцу в Telegram; пусто — Джарвис напишет про окончание"},
}}

_PERMIT_FIELDS = {
    "car": {"type": "string", "description": "госномер или имя бокса, например X758KC977 или Бусик"},
    "car_id": _STR,
    "number": {"type": "string", "description": "номер разрешения на бланке, если он есть отдельно от УИН"},
    "district": {"type": "string", "description": "район, например Таганский"},
    "starts": {"type": "string", "description": "начало срока, YYYY-MM-DD или ДД.ММ.ГГГГ, день включительно"},
    "ends": {"type": "string", "description": "окончание срока, день включительно"},
    "free_from": {"type": "string", "description": "с какого времени бесплатно, ЧЧ:ММ"},
    "free_until": {"type": "string", "description": "до какого времени бесплатно, ЧЧ:ММ; интервал может переходить через полночь"},
    "fee": {"type": "integer", "description": "годовая плата за круглосуточную парковку, рубли, без копеек"},
    "uin": {"type": "string", "description": "УИН, только цифры"},
    "reminders": {"type": "array", "items": _REMINDER,
                  "description": "напоминания самого разрешения. Не дублируй их plan_create"},
    "use_received_file": {"type": "string",
                          "description": "часть имени уже присланного вложения или last. Нет файла — разрешение всё равно запишется"},
    "notes": {"type": "string", "description": "короткая пометка на бланке. Сюда не складывай район, срок, часы, плату и УИН"},
}

TOOLS = [
    {"name": "parking_permit_list",
     "description": ("Прочитать резидентские разрешения из Автомобили → Разрешения. "
                     "car — госномер или имя бокса; без него — все. Не смотри в заметки автомобиля."),
     "input_schema": {"type": "object", "properties": {"car": _STR, "car_id": _STR}, "required": []}},
    {"name": "parking_permit_create",
     "description": ("Создать резидентское разрешение во вкладке «Разрешения» и привязать к боксу по госномеру или имени. "
                     "Вызывай сам, когда пользователь прислал бланк и просит добавить. Не пиши это в notes автомобиля, "
                     "не создавай новый бокс и не спрашивай про ПК. Номер — только если он есть на бланке; УИН клади в uin. "
                     "Срок starts/ends включительно. Бесплатные часы — free_from и free_until. "
                     "fee — годовая плата за круглосуточную стоянку в рублях; оплату не отмечай, такого поля нет. "
                     "Напоминания — reminders этого разрешения (дата или за сколько дней, время МСК, текст в Telegram). "
                     "Эти слоты уже шлёт минутный цикл. plan_create на те же сроки не вызывай. "
                     "Файл, который уже лежит во вложении чата, — use_received_file. "
                     "Повтор с тем же УИН у того же авто обновляет запись, а не плодит вторую."),
     "input_schema": {"type": "object", "properties": _PERMIT_FIELDS, "required": ["car", "starts", "ends"]}},
    {"name": "parking_permit_update",
     "description": ("Поправить разрешение во вкладке «Разрешения». id — из parking_permit_list; "
                     "без id берётся единственное разрешение автомобиля (car — госномер или имя). "
                     "Передавай только то, что меняется. reminders, если переданы, заменяют список целиком. "
                     "Не переноси правку в заметки автомобиля. Удаление — parking_permit_cancel."),
     "input_schema": {"type": "object", "properties": {**_PERMIT_FIELDS, "id": _STR, "revoked": {"type": "boolean"}},
                      "required": ["car"]}},
    {"name": "parking_permit_cancel",
     "description": ("Удалить резидентское разрешение и его файлы. Без confirmed:true ничего не удаляет и просит подтверждение. "
                     "После согласия пользователя повтори вызов с confirmed:true. Напоминания этого разрешения пропадут вместе с ним. "
                     "Плановые задачи не трогает."),
     "input_schema": {"type": "object", "properties": {
         "car": _STR, "car_id": _STR, "id": _STR, "number": _STR, "uin": _STR,
         "confirmed": {"type": "boolean"},
     }, "required": ["car"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
if _missing:
    raise RuntimeError("parking skills tools missing: " + ", ".join(_missing))

PROMPT = (
    "РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ — parking_permit_list, parking_permit_create, parking_permit_update, parking_permit_cancel. "
    "Раздел «Автомобили», вкладка «Разрешения». Резидентское парковочное разрешение клади только сюда.\n"
    "а) Не пиши его в заметки автомобиля (поле notes бокса), не в «Базу данных», не в документы заметок и не новым боксом. "
    "Не предлагай сделать это на ПК и не открывай сайт вместо записи: инструменты пишут в ту же карточку, что форма на сайте.\n"
    "б) Бокс ищи по госномеру или имени («Бусик», «X758KC977»). Латинские и русские буквы номера — одно и то же. "
    "Не нашёл или их несколько — спроси, ничего не записывай.\n"
    "в) С бланка бери только то, что на нём есть: номер разрешения, район, срок (окончание включительно), "
    "часы бесплатной парковки, годовую плату за круглосуточную стоянку, УИН. Не выдумывай остальное. "
    "Отметки «оплачено» в разрешении нет — не ставь её и не пиши про оплату в заметки, если человек сам её не назвал.\n"
    "г) Напоминания — массив reminders у разрешения: mode date или days, time ЧЧ:ММ по Москве, text — текст владельцу в Telegram. "
    "Слоты уже уходят минутным циклом. plan_create на эти сроки не вызывай и уже стоящие плановые задачи сам не отменяй.\n"
    "д) Файл, который уже есть во вложении этого чата, приложи use_received_file (часть имени или last). "
    "Если файла в буфере нет — разрешение всё равно запиши и скажи, что скан не приложен.\n"
    "е) После записи ответь коротко: авто, район, срок, бесплатные часы, плата, УИН, когда уйдут напоминания, приложен ли файл.\n"
    "ж) Правка — parking_permit_update. Удаление разрешения и файлов — parking_permit_cancel: без confirmed вернётся "
    "needs_confirmation, после «да» повтори с confirmed:true. Список — parking_permit_list.\n\n"
)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": PROMPT}


_PLATE_FOLD = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
})


def plate_key(value) -> str:
    text = str(value or "").upper().translate(_PLATE_FOLD)
    return re.sub(r"[^0-9A-Z]", "", text)


def _name_key(value) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def _active_cars(app):
    return [c for c in ((app or {}).get("cars") or [])
            if isinstance(c, dict) and c.get("id") and not c.get("archived")]


def _all_cars(app):
    return [c for c in ((app or {}).get("cars") or []) if isinstance(c, dict) and c.get("id")]


def _car_brief(car) -> dict:
    return {"id": car.get("id"), "name": car.get("name") or "", "plate": car.get("plate") or ""}


def _confirmed(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "да")


def _user_date(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    day = _parse_date(raw)
    if day:
        return day.isoformat()
    match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", raw)
    if not match:
        return ""
    try:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1))).date().isoformat()
    except ValueError:
        return ""


def resolve_car(app, inp):
    inp = inp or {}
    cars = _active_cars(app)
    cid = str(inp.get("car_id") or inp.get("carId") or "").strip()
    if cid:
        hit = next((c for c in cars if str(c.get("id")) == cid), None)
        if hit:
            return hit, None
        archived = next((c for c in _all_cars(app) if str(c.get("id")) == cid and c.get("archived")), None)
        if archived:
            return None, {"error": "Автомобиль в архиве. Разрешение привязываю только к боксу в гараже."}
        return None, {"error": "Автомобиль с таким id не найден. Новый бокс не создаю и в заметки не пишу."}
    query = str(inp.get("car") or inp.get("plate") or "").strip()
    if not query:
        return None, {"error": "Укажите автомобиль: госномер или имя бокса."}
    folded = plate_key(query)
    by_plate = [c for c in cars if folded and plate_key(c.get("plate")) == folded] if any(ch.isdigit() for ch in folded) else []
    exact = [c for c in cars if _name_key(query) and _name_key(c.get("name")) == _name_key(query)]
    if by_plate:
        found = by_plate
    elif exact:
        found = exact
    else:
        found = [c for c in cars if _name_key(query) and _name_key(query) in _name_key(c.get("name"))]
        if not found and any(ch.isdigit() for ch in folded):
            if any(c.get("archived") and plate_key(c.get("plate")) == folded for c in _all_cars(app)):
                return None, {"error": "Этот автомобиль в архиве. Разрешение привязываю только к боксу в гараже."}
    if len(found) == 1:
        return found[0], None
    if len(found) > 1:
        return None, {"needs_clarification": True,
                      "message": "Подходит несколько автомобилей. Уточни, к какому привязать разрешение.",
                      "cars": [_car_brief(c) for c in found]}
    return None, {"error": "Автомобиль не найден среди боксов. Новый не создаю и в заметки автомобиля не пишу."}


def _free_hours(inp):
    keys = ("free_from", "freeFrom", "free_until", "freeUntil", "free_hours")
    if not any(k in inp for k in keys):
        return None
    if "free_hours" in inp and "free_from" not in inp and "freeFrom" not in inp:
        raw = str(inp.get("free_hours") or "").strip()
        if not raw:
            return ("", "")
        parts = re.split(r"\s*[–—-]\s*", raw)
        if len(parts) != 2:
            return False
        start, end = _clock_field(parts[0]), _clock_field(parts[1])
        if not start or not end:
            return False
        return (start, end)
    start = _clock_field(inp.get("free_from") if "free_from" in inp else inp.get("freeFrom"))
    end = _clock_field(inp.get("free_until") if "free_until" in inp else inp.get("freeUntil"))
    if start is None or end is None or bool(start) != bool(end):
        return False
    return (start, end)


def _tool_reminders(raw):
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "Напоминания — список"
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "Напоминание задано неверно"
        mode = item.get("mode")
        days = item.get("daysBefore") if "daysBefore" in item else item.get("days_before")
        if mode not in ("date", "days"):
            if item.get("date"):
                mode = "date"
            elif days is not None:
                mode = "days"
            else:
                return None, "Напоминание: укажите дату или за сколько дней"
        rec = {"mode": mode, "time": item.get("time") or "10:00", "text": item.get("text") or ""}
        if item.get("id"):
            rec["id"] = str(item.get("id"))
        if mode == "days":
            rec["daysBefore"] = days
        else:
            parsed = _user_date(item.get("date"))
            if not parsed:
                return None, "Укажите дату напоминания"
            rec["date"] = parsed
        out.append(rec)
    return out, None


def _build_payload(car, inp, existing):
    payload = {"carId": car.get("id")}
    if existing:
        payload["id"] = existing.get("id")
    if "number" in inp:
        payload["number"] = inp.get("number") or ""
    if "district" in inp or "zone" in inp:
        payload["zone"] = inp.get("district") if "district" in inp else inp.get("zone")
    for src, dst in (("starts", "starts"), ("start", "starts"), ("ends", "ends"), ("end", "ends")):
        if src not in inp or dst in payload:
            continue
        parsed = _user_date(inp.get(src))
        if inp.get(src) and not parsed:
            return None, {"error": "Дата — ГГГГ-ММ-ДД или ДД.ММ.ГГГГ"}
        payload[dst] = parsed
    hours = _free_hours(inp)
    if hours is False:
        return None, {"error": "Часы бесплатной парковки — ЧЧ:ММ, оба конца, например 20:00 и 08:00"}
    if hours is not None:
        payload["freeFrom"], payload["freeUntil"] = hours
    if "fee" in inp:
        fee = _money_field(inp.get("fee"))
        if fee is False:
            return None, {"error": "Плата — сумма в рублях"}
        payload["fee"] = fee
    if "uin" in inp:
        uin = _uin_field(inp.get("uin"))
        if uin is None:
            return None, {"error": "УИН — только цифры"}
        payload["uin"] = uin
    if "reminders" in inp:
        reminders, err = _tool_reminders(inp.get("reminders"))
        if err:
            return None, {"error": err}
        payload["reminders"] = reminders
    for key in ("address", "spot", "holder", "notes"):
        if key in inp:
            payload[key] = inp.get(key)
    if "revoked" in inp:
        payload["revoked"] = inp.get("revoked")
    return payload, None


def _decorate(app, permit):
    cars = {str(c.get("id")): c for c in _all_cars(app)}
    car = cars.get(str(permit.get("carId"))) or {}
    return {**permit, "carName": car.get("name") or "", "plate": car.get("plate") or ""}


def _permits_of(car_id):
    return [p for p in _load()["permits"] if p.get("carId") == car_id]


def _find_permit(car, inp):
    rows = _permits_of(car.get("id"))
    pid = str((inp or {}).get("id") or (inp or {}).get("permit_id") or "").strip()
    if pid:
        hit = next((p for p in rows if p.get("id") == pid), None)
        if hit:
            return hit, None
        if any(p.get("id") == pid for p in _load()["permits"]):
            return None, {"error": "Это разрешение другого автомобиля."}
        return None, {"error": "Разрешение не найдено."}
    number = _clean(inp.get("number"), 40) if inp.get("number") else ""
    uin = _uin_field(inp.get("uin")) if inp.get("uin") else ""
    if uin is None:
        return None, {"error": "УИН — только цифры"}
    pool = rows
    if uin:
        pool = [p for p in pool if p.get("uin") == uin]
    if number:
        pool = [p for p in pool if p.get("number") == number]
    if not uin and not number:
        open_rows = [p for p in rows if not p.get("revoked")]
        pool = open_rows or rows
    if len(pool) == 1:
        return pool[0], None
    if not pool:
        return None, {"error": "Разрешение этого автомобиля не найдено."}
    return None, {"needs_clarification": True,
                  "message": "У автомобиля несколько разрешений. Укажи id, номер или УИН.",
                  "permits": [_decorate({}, public_permit(p)) for p in pool]}


def _same_open(car, payload):
    rows = _permits_of(car.get("id"))
    uin = payload.get("uin") or ""
    number = payload.get("number") or ""
    if uin:
        hit = [p for p in rows if p.get("uin") == uin]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            return "many"
    if number:
        hit = [p for p in rows if p.get("number") == number]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            return "many"
    return None


def _received(hint):
    try:
        import tg_bot
    except Exception:
        return None, "модуль Telegram недоступен"
    ctx = tg_bot.get_ctx()
    if ctx is None or getattr(ctx, "chat_id", None) is None:
        return None, "присланный файл доступен в чате с ботом — разрешение запишу без скана"
    raw = str(hint or "").strip()
    if raw.lower() in ("last", "latest", "true", "1", "последний", "последнее"):
        raw = ""
    got = tg_bot.recent_file(ctx.chat_id, raw)
    if not got:
        return None, "присланного файла не нашёл — разрешение запишу без скана"
    return got, None


def _attach_received(permit_id, inp):
    stored = next((p for p in _load()["permits"] if p.get("id") == permit_id), None)
    if "use_received_file" not in (inp or {}) or (inp or {}).get("use_received_file") in (None, False, ""):
        return {"permit": public_permit(stored) if stored else None}
    got, err = _received(inp.get("use_received_file"))
    if err or not stored:
        return {"permit": public_permit(stored) if stored else None, "file_error": err or "разрешение не найдено"}
    name, data = got
    safe = Path(str(name or "scan")).name
    if any(f.get("name") == safe and f.get("size") == len(data) for f in (stored.get("files") or [])):
        return {"permit": public_permit(stored), "file": {"name": safe, "already": True}}
    ext = Path(safe).suffix.lower().lstrip(".")
    code, body = save_file(permit_id, safe, ext, data)
    if code != 200:
        return {"permit": public_permit(stored), "file_error": body.get("error") or "файл не приложен"}
    return {"permit": body.get("permit"), "file": body.get("file")}


def _finish(app, car, body, inp, updated_existing=False):
    if not body.get("ok"):
        return body
    attached = _attach_received(body["permit"]["id"], inp)
    out = {"ok": True, "permit": attached.get("permit") or body["permit"], "car": _car_brief(car),
           "where": "Автомобили → Разрешения"}
    if updated_existing:
        out["updated_existing"] = True
    if attached.get("file"):
        out["file"] = attached["file"]
    if attached.get("file_error"):
        out["file_error"] = attached["file_error"]
    return out


def _list(app, inp):
    query = str((inp or {}).get("car") or (inp or {}).get("plate") or (inp or {}).get("car_id") or (inp or {}).get("carId") or "").strip()
    rows = for_assistant()
    if not query:
        return {"ok": True, "permits": [_decorate(app, p) for p in rows]}
    car, err = resolve_car(app, inp)
    if err:
        return err
    return {"ok": True, "permits": [_decorate(app, p) for p in rows if p.get("carId") == car.get("id")],
            "car": _car_brief(car)}


def _write(app, inp, create: bool):
    car, err = resolve_car(app, inp)
    if err:
        return err
    existing = None
    if not create:
        existing, err = _find_permit(car, inp)
        if err:
            return err
    payload, err = _build_payload(car, inp, existing)
    if err:
        return err
    updated_existing = False
    if create:
        same = _same_open(car, payload)
        if same == "many":
            return {"needs_clarification": True,
                    "message": "У автомобиля уже несколько разрешений с таким номером или УИН. Укажи id."}
        if same:
            payload["id"] = same.get("id")
            updated_existing = True
    code, body = save_permit(payload)
    if code != 200:
        return body
    return _finish(app, car, body, inp, updated_existing)


def _cancel(app, inp):
    car, err = resolve_car(app, inp)
    if err:
        return err
    permit, err = _find_permit(car, inp)
    if err:
        return err
    if not _confirmed(inp.get("confirmed")):
        who = car.get("name") or car.get("plate") or "автомобиль"
        title = permit.get("number") or permit.get("uin") or "без номера"
        files = len(permit.get("files") or [])
        return {"needs_confirmation": True,
                "message": (f"Удалить резидентское разрешение {title} ({who}) и файлы ({files})? "
                            "Напоминания этого разрешения тоже удалятся. После согласия повтори вызов с confirmed:true.")}
    code, body = delete_permit(permit.get("id"))
    if code != 200:
        return body
    return {"ok": True, "deleted": permit.get("id"), "car": _car_brief(car)}


def execute(name: str, inp: dict, app: dict, state=None) -> dict:
    if not skill_on(state):
        return {"error": "Навык «Резидентские разрешения» выключен."}
    inp = inp or {}
    if name == "parking_permit_list":
        return _list(app, inp)
    if name == "parking_permit_create":
        return _write(app, inp, True)
    if name == "parking_permit_update":
        return _write(app, inp, False)
    if name == "parking_permit_cancel":
        return _cancel(app, inp)
    return {"error": "unknown tool"}
