"""Резидентские парковочные разрешения своих автомобилей.

Карточка авто (название, номер, марка, модель) живёт в общих данных приложения,
раздел «Автомобили». Разрешения, напоминания и сканы — здесь:

  DATA_DIR/jarvis_cars.json   — разрешения
  DATA_DIR/jarvis_cars/       — фото и PDF

Напоминание: либо дата, либо за сколько дней до окончания, плюс время (МСК)
и текст. Минутный цикл забирает просроченные в окне 6 часов и шлёт текст
владельцу в Telegram. Повторно то же напоминание не уходит.
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
        if not number or not _NUMBER_RE.match(number):
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
