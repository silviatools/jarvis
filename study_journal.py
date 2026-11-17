"""
study_journal.py — журнал по каждому предмету «Учёбы».

Ассистент сам ведёт три слоя и знает ситуацию по предмету:
  • память — факты из разговора и из проверки учебных чатов (срок, требование, кабинет);
  • баллы — текущие баллы, что нужно для зачёта, сдал или нет (одна актуальная запись на предмет);
  • зачёты — дата, место, форма, дедлайн допуска и заметка.

Запись, правка и удаление — без отдельного подтверждения: это память предмета, её же правит пользователь
в разделе «Учёба». Выключенный навык не попадает в инструменты, промпт и фоновую запись.
Повтор уже известного факта в проверке чатов не уходит как новая новость: один раз коротко напоминаем
и спрашиваем, обратил ли пользователь внимание.

Файл DATA_DIR/jarvis_study_journals.json — в бэкапе.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
STORE_FILE = DATA_DIR / "jarvis_study_journals.json"
STORE_NAME = STORE_FILE.name
_lock = threading.Lock()

SKILL_ID = "study_journal"
KINDS = ("fact", "grade", "exam")
SOURCES = ("assistant", "scan", "user")
MAX_ENTRIES = 500
MAX_TEXT = 2000

SKILLS = [
    {"id": SKILL_ID, "title": "Журналы предметов", "risk": "write", "default": True,
     "desc": ("Ассистент сам ведёт журнал по каждому предмету в «Учёбе» и опирается на него в боте, чате и голосе. "
              "Память: срок, требование, кабинет, как сдавать — добавляет, правит и удаляет, когда это прозвучало в разговоре. "
              "Баллы: текущие баллы, сколько нужно для зачёта и сдал ли. Зачёты: дата, место, форма, дедлайн допуска и заметка. "
              "Проверка учебных чатов пишет туда же найденные факты. Уже известное в следующий раз не присылается как новое: "
              "один раз коротко напомнит и спросит, обратили ли вы внимание. Журнал виден в столбце предмета и в задании, "
              "его можно править руками. Подтверждение на каждую запись не спрашивает — это память предмета, правки сразу в «Учёбе»."),
     "tools": ["study_journal_list", "study_journal_write", "study_journal_delete"]},
]


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


_STR = {"type": "string"}

TOOLS = [
    {"name": "study_journal_list",
     "description": ("Журнал предметов «Учёбы»: память (факты), текущие баллы и расписание зачётов. "
                     "subject — название или id предмета; без него — все предметы. Вызывай, прежде чем отвечать, что известно по предмету."),
     "input_schema": {"type": "object", "properties": {"subject": _STR}, "required": []}},
    {"name": "study_journal_write",
     "description": (
         "Записать или поправить журнал предмета. Вызывай сам, без спроса, как только в разговоре появилось знание по предмету. "
         "kind=fact — text: что запомнить (срок, требование, кабинет, формат, преподаватель). "
         "kind=grade — текущие баллы: points, points_max, need (что нужно для зачёта/сдачи), passed true/false или null если неизвестно. "
         "На предмет один актуальный балл: повторный вызов обновляет его. "
         "kind=exam — зачёт или экзамен: date YYYY-MM-DD, time ЧЧ:ММ, place, format, deadline YYYY-MM-DD (дедлайн допуска/заметки), "
         "note, text (короткое название, например «Зачёт»). Та же дата у предмета — правка этой записи. "
         "id — если правишь конкретную запись. subject — название предмета с доски «Учёбы»."),
     "input_schema": {"type": "object", "properties": {
         "id": _STR, "subject": _STR, "kind": {"type": "string", "enum": list(KINDS)},
         "text": _STR, "points": {"type": "number"}, "points_max": {"type": "number"},
         "need": _STR, "passed": {"type": ["boolean", "null"]},
         "date": _STR, "time": _STR, "place": _STR, "format": _STR, "deadline": _STR, "note": _STR,
     }, "required": ["kind"]}},
    {"name": "study_journal_delete",
     "description": "Удалить запись журнала предмета (факт, балл или зачёт) по id. Сам, если пользователь просит забыть или запись неверна.",
     "input_schema": {"type": "object", "properties": {"id": _STR}, "required": ["id"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}

PROMPT = (
    "13. ЖУРНАЛЫ ПРЕДМЕТОВ — study_journal_list, study_journal_write, study_journal_delete. "
    "По каждому предмету «Учёбы» ты сам ведёшь журнал и отвечаешь из него, а не «как будто первый раз».\n"
    "а) Память (kind=fact): срок, требование, кабинет, формат сдачи, что сказал преподаватель. Как только это прозвучало "
    "в разговоре — сразу study_journal_write, без вопроса «записать?». Мелочь одного сообщения («ок», «спасибо») не пиши.\n"
    "б) Баллы (kind=grade): текущие points, points_max, need — что нужно для зачёта или сдачи, passed — сдал или нет "
    "(null, если неизвестно). Новый балл по тому же предмету обновляет запись, а не плодит вторую.\n"
    "в) Зачёты и экзамены (kind=exam): date, time, place, format, deadline (до какой даты допуск, работа, заявление), note. "
    "Ту же дату правь, а не дублируй.\n"
    "г) Перед ответом «что по предмету» — study_journal_list (или журнал в get_data study). Не выдумывай баллы и даты.\n"
    "д) Удаление — study_journal_delete, если просят забыть или запись ошибочна. Править можно и в «Учёбе», в столбце предмета.\n"
    "е) Уже лежащее в журнале не подавай как новость. Если уместно напомнить — одна короткая фраза и вопрос, обратил ли человек внимание.\n\n"
)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": PROMPT}


# ── нормализация ──────────────────────────────────────────────────────────────

_STOP = {
    "это", "как", "для", "что", "или", "уже", "ещё", "еще", "при", "над", "под", "без", "про",
    "его", "её", "ее", "их", "вам", "вас", "нам", "нас", "тот", "так", "все", "всё", "был", "была",
    "были", "будет", "надо", "нужно", "только", "тоже", "если", "когда", "чтобы", "предмет", "предмета",
}
_MONTHS = (
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма", 5), ("июн", 6),
    ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12),
)


def _norm(value) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value) -> set:
    return {w for w in _norm(value).split() if len(w) > 2 and w not in _STOP}


def similar(a, b) -> bool:
    """Один и тот же факт, даже если фраза чуть другая."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 12 and (na in nb or nb in na):
        return True
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return False
    inter = len(ta & tb)
    if inter >= 2 and inter / min(len(ta), len(tb)) >= 0.8:
        return True
    return inter >= 3 and inter / len(ta | tb) >= 0.72


def _num(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not m:
        return None
    return round(float(m.group(0).replace(",", ".")), 2)


def _boolish(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().casefold()
    if s in ("1", "true", "yes", "да", "сдал", "зачтено"):
        return True
    if s in ("0", "false", "no", "нет", "не сдал"):
        return False
    return None


def _date(value, today: date = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    found = _find_date(raw, today or date.today())
    return found or ""


def _iso(y, m, d) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def _find_date(text: str, today: date) -> str:
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text or "")
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text or "")
    if m:
        dd, mm = int(m.group(1)), int(m.group(2))
        yy = m.group(3)
        year = today.year if not yy else (2000 + int(yy) if len(yy) == 2 else int(yy))
        iso = _iso(year, mm, dd)
        if iso and not yy and date.fromisoformat(iso) < today:
            iso = _iso(year + 1, mm, dd)
        return iso
    low = (text or "").casefold().replace("ё", "е")
    for m in re.finditer(r"\b(\d{1,2})\s+([а-я]{3,})", low):
        dd = int(m.group(1))
        word = m.group(2)
        if word.startswith("мая") or word == "май":
            month = 5
        elif word.startswith("март"):
            month = 3
        else:
            month = next((n for stem, n in _MONTHS if word.startswith(stem) and stem != "ма"), 0)
        if not month:
            continue
        iso = _iso(today.year, month, dd)
        if iso and date.fromisoformat(iso) < today:
            iso = _iso(today.year + 1, month, dd)
        if iso:
            return iso
    return ""


def _time(value) -> str:
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", str(value or ""))
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _clip(value, n) -> str:
    return str(value or "").strip()[:n]


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── хранилище ─────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"entries": []}


def _load() -> dict:
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return _empty()
    return data


def _save(data: dict) -> None:
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict) and e.get("id")]
    if len(entries) > MAX_ENTRIES:
        entries.sort(key=lambda e: (e.get("active", True), e.get("updatedAt") or 0))
        entries = entries[-MAX_ENTRIES:]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STORE_FILE)


def _subjects(app) -> list:
    return [s for s in (app or {}).get("studySubjects") or [] if isinstance(s, dict) and s.get("id") and s.get("name")]


def resolve_subject(app, query: str):
    """(subject | None, error). Пустой query — («», "")."""
    subjects = _subjects(app)
    raw = str(query or "").strip()
    if not raw:
        return None, "Укажи предмет."
    by_id = [s for s in subjects if str(s.get("id")) == raw]
    if len(by_id) == 1:
        return by_id[0], ""
    q = _norm(raw)
    scored = []
    for s in subjects:
        n, r = _norm(s.get("name")), _norm(s.get("reaSubject"))
        score = 0
        if q and (q == n or (r and q == r)):
            score = 100
        elif len(q) >= 3 and (q in n or n in q or (r and (q in r or r in q))):
            score = 70
        else:
            ta = _tokens(q)
            if ta and len(ta & _tokens(n)) / len(ta) >= 0.8:
                score = 60
        if score:
            scored.append((score, s))
    names = ", ".join(s.get("name") for s in subjects) or "предметов на доске нет"
    if not scored:
        return None, f"Предмет «{raw}» не найден. Сейчас на доске: {names}."
    best = max(sc for sc, _s in scored)
    tied = [s for sc, s in scored if sc == best]
    if len(tied) > 1 and best < 100:
        return None, "Несколько предметов подходят: " + ", ".join(s.get("name") for s in tied) + "."
    return tied[0], ""


def _match_item_subject(app, item: dict):
    named = str(item.get("subject") or "").strip()
    if named:
        found, _err = resolve_subject(app, named)
        if found:
            return found
    blob = _norm(" ".join(str(item.get(k) or "") for k in ("summary", "why", "chat", "subject")))
    chat = re.sub(r"\s+max$", "", _norm(item.get("chat"))).strip()
    hits = []
    for s in _subjects(app):
        n = _norm(s.get("name"))
        if chat and chat == n:
            hits.append(s)
        elif len(n) >= 4 and n in blob:
            hits.append(s)
    uniq = []
    seen = set()
    for s in hits:
        if s.get("id") not in seen:
            seen.add(s.get("id"))
            uniq.append(s)
    return uniq[0] if len(uniq) == 1 else None


def _active(entries, subject_id=None, kind=None):
    out = []
    for e in entries:
        if not e.get("active", True):
            continue
        if subject_id and e.get("subjectId") != subject_id:
            continue
        if kind and e.get("kind") != kind:
            continue
        out.append(e)
    return out


def _public(entry: dict) -> dict:
    keep = ("id", "subjectId", "kind", "text", "source", "createdAt", "updatedAt",
            "announced", "reminded", "acked", "points", "pointsMax", "need", "passed",
            "date", "time", "place", "format", "deadline", "note")
    return {k: entry.get(k) for k in keep if k in entry or k in ("id", "kind", "text")}


def list_entries(subject_id=None) -> list:
    with _lock:
        entries = _load()["entries"]
    rows = [_public(e) for e in _active(entries, subject_id)]
    rows.sort(key=lambda e: ({"grade": 0, "exam": 1, "fact": 2}.get(e.get("kind"), 9), e.get("date") or "9999", -(e.get("updatedAt") or 0)))
    return rows


def for_assistant(app) -> list:
    """Срез для get_data study: по предметам, без служебных флагов."""
    if not skill_on((app or {}).get("telegramSkills")):
        return []
    names = {s.get("id"): s.get("name") for s in _subjects(app)}
    grouped = {}
    for e in list_entries():
        sid = e.get("subjectId") or ""
        g = grouped.setdefault(sid, {"subjectId": sid, "subject": names.get(sid) or "", "facts": [], "grade": None, "exams": []})
        if e.get("kind") == "grade" and g["grade"] is None:
            g["grade"] = {k: e.get(k) for k in ("id", "points", "pointsMax", "need", "passed")}
        elif e.get("kind") == "exam":
            g["exams"].append({k: e.get(k) for k in ("id", "text", "date", "time", "place", "format", "deadline", "note")})
        elif e.get("kind") == "fact":
            if len(g["facts"]) < 12:
                g["facts"].append({"id": e.get("id"), "text": e.get("text")})
    return [g for g in grouped.values() if g["subject"] or g["facts"] or g["grade"] or g["exams"]]


def grade_line(entry: dict) -> str:
    bits = []
    if entry.get("points") is not None:
        bits.append(f"{_trim_num(entry['points'])}" + (f" из {_trim_num(entry['pointsMax'])}" if entry.get("pointsMax") is not None else " баллов"))
    if entry.get("need"):
        bits.append("для зачёта " + str(entry["need"]))
    if entry.get("passed") is True:
        bits.append("сдал")
    elif entry.get("passed") is False:
        bits.append("не сдал")
    return ", ".join(bits) or "баллы"


def exam_line(entry: dict) -> str:
    bits = [entry.get("text") or "Зачёт"]
    if entry.get("date"):
        bits.append(entry["date"] + (f" {entry['time']}" if entry.get("time") else ""))
    if entry.get("place"):
        bits.append(entry["place"])
    if entry.get("format"):
        bits.append(entry["format"])
    if entry.get("deadline"):
        bits.append("до " + entry["deadline"])
    if entry.get("note"):
        bits.append(entry["note"])
    return ", ".join(str(b) for b in bits if b)


def _trim_num(n) -> str:
    if n is None:
        return ""
    f = float(n)
    return str(int(f)) if f == int(f) else str(f)


def entry_line(entry: dict) -> str:
    if entry.get("kind") == "grade":
        return grade_line(entry)
    if entry.get("kind") == "exam":
        return exam_line(entry)
    return str(entry.get("text") or "")


# ── запись ────────────────────────────────────────────────────────────────────

def _blank(subject_id, kind, source) -> dict:
    now = _now_ms()
    return {
        "id": uuid.uuid4().hex[:12], "subjectId": subject_id, "kind": kind, "text": "",
        "source": source if source in SOURCES else "assistant", "createdAt": now, "updatedAt": now,
        "active": True, "announced": False, "reminded": False, "acked": False, "refs": [],
        "points": None, "pointsMax": None, "need": "", "passed": None,
        "date": "", "time": "", "place": "", "format": "", "deadline": "", "note": "",
    }


def _apply_fields(entry: dict, fields: dict, today: date = None) -> str:
    kind = entry.get("kind")
    if kind == "fact":
        text = _clip(fields.get("text"), MAX_TEXT)
        if not text:
            return "Для памяти нужен text."
        entry["text"] = text
        return ""
    if kind == "grade":
        if "points" in fields:
            entry["points"] = _num(fields.get("points"))
        if "points_max" in fields or "pointsMax" in fields:
            entry["pointsMax"] = _num(fields.get("points_max", fields.get("pointsMax")))
        if "need" in fields:
            entry["need"] = _clip(fields.get("need"), 300)
        if "passed" in fields:
            entry["passed"] = _boolish(fields.get("passed"))
        if entry.get("points") is None and not entry.get("need") and entry.get("passed") is None:
            return "Для баллов укажи points, need или passed."
        entry["text"] = grade_line(entry)
        return ""
    if kind == "exam":
        today = today or date.today()
        if fields.get("date"):
            parsed = _date(fields.get("date"), today) or _find_date(str(fields.get("date")), today)
            if fields.get("date") and not parsed:
                return "Дата зачёта — в виде ГГГГ-ММ-ДД или «20 декабря»."
            entry["date"] = parsed
        if fields.get("time"):
            entry["time"] = _time(fields.get("time"))
        if "place" in fields:
            entry["place"] = _clip(fields.get("place"), 200)
        if "format" in fields:
            entry["format"] = _clip(fields.get("format"), 120)
        if fields.get("deadline"):
            parsed = _date(fields.get("deadline"), today) or _find_date(str(fields.get("deadline")), today)
            if not parsed:
                return "Дедлайн — в виде ГГГГ-ММ-ДД или «18 декабря»."
            entry["deadline"] = parsed
        if "note" in fields:
            entry["note"] = _clip(fields.get("note"), 1000)
        if "text" in fields or not entry.get("text"):
            entry["text"] = _clip(fields.get("text") or entry.get("text") or "Зачёт", 200)
        if not (entry.get("date") or entry.get("place") or entry.get("deadline") or entry.get("note")):
            return "Для зачёта нужна дата, место, дедлайн или заметка."
        return ""
    return "Неизвестный kind."


def _same_grade(a: dict, b: dict) -> bool:
    return (_num(a.get("points")) == _num(b.get("points")) and _num(a.get("pointsMax")) == _num(b.get("pointsMax"))
            and _norm(a.get("need")) == _norm(b.get("need")) and a.get("passed") == b.get("passed"))


def _grade_changed(entry, fields) -> bool:
    draft = dict(entry)
    if "points" in fields:
        draft["points"] = _num(fields.get("points"))
    if "points_max" in fields or "pointsMax" in fields:
        draft["pointsMax"] = _num(fields.get("points_max", fields.get("pointsMax")))
    if "need" in fields:
        draft["need"] = _clip(fields.get("need"), 300)
    if "passed" in fields:
        draft["passed"] = _boolish(fields.get("passed"))
    return not _same_grade(entry, draft)


def _exam_changed(entry, fields, today: date) -> bool:
    def nxt(key, parser=None):
        if key not in fields and not (key == "points_max"):
            return entry.get(key) or ""
        raw = fields.get(key)
        if parser:
            return parser(raw, today) or (_find_date(str(raw), today) if raw else "")
        return _clip(raw, 1000)
    date_n = nxt("date", _date) if "date" in fields else (entry.get("date") or "")
    deadline = nxt("deadline", _date) if "deadline" in fields else (entry.get("deadline") or "")
    place = _clip(fields["place"], 200) if "place" in fields else (entry.get("place") or "")
    form = _clip(fields["format"], 120) if "format" in fields else (entry.get("format") or "")
    note = _clip(fields["note"], 1000) if "note" in fields else (entry.get("note") or "")
    tm = _time(fields.get("time")) if fields.get("time") else (entry.get("time") or "")
    return (date_n, tm, _norm(place), _norm(form), deadline, _norm(note)) != (
        entry.get("date") or "", entry.get("time") or "", _norm(entry.get("place")),
        _norm(entry.get("format")), entry.get("deadline") or "", _norm(entry.get("note")))


def _find_existing(entries, subject_id, kind, fields, today: date):
    rows = _active(entries, subject_id, kind)
    if fields.get("id"):
        hit = next((e for e in rows if e.get("id") == fields["id"]), None)
        if hit:
            return hit
    if kind == "grade":
        return rows[0] if rows else None
    if kind == "exam":
        want = ""
        if fields.get("date"):
            want = _date(fields.get("date"), today) or _find_date(str(fields.get("date")), today)
            return next((e for e in rows if e.get("date") == want), None)
        blob = " ".join(x for x in (fields.get("note"), fields.get("place"), fields.get("format")) if x)
        if not blob:
            return None
        return next((e for e in rows if similar(blob, " ".join(x for x in (e.get("note"), e.get("place"), e.get("format")) if x))), None)
    text = fields.get("text") or ""
    return next((e for e in rows if text and similar(text, e.get("text") or "")), None)


def _flag(entry: dict, source: str, created: bool, material: bool) -> str:
    """new — сказать как новость; remind — коротко напомнить; silent — промолчать; stored — уже сказано в разговоре."""
    if source != "scan":
        entry["announced"] = True
        entry["acked"] = True
        entry["reminded"] = True
        return "stored"
    if created or material:
        entry["announced"] = False
        entry["reminded"] = False
        entry["acked"] = False
        return "new"
    if entry.get("acked") or entry.get("reminded"):
        return "silent"
    if entry.get("announced"):
        return "remind"
    return "new"


def write_entry(app, fields: dict, source: str = "assistant", ref: str = "", today: date = None) -> dict:
    today = today or date.today()
    fields = fields or {}
    kind = str(fields.get("kind") or "").strip()
    if kind not in KINDS:
        return {"error": "kind: fact, grade или exam."}
    with _lock:
        data = _load()
        entries = data["entries"]
        current = None
        if fields.get("id"):
            current = next((e for e in entries if e.get("id") == fields["id"] and e.get("active", True)), None)
            if not current:
                return {"error": "Запись журнала не найдена."}
            subject_id = current.get("subjectId")
        else:
            subject, err = resolve_subject(app, fields.get("subject") or "")
            if err:
                return {"error": err}
            subject_id = subject["id"]
            current = _find_existing(entries, subject_id, kind, fields, today)
        created = current is None
        if created:
            current = _blank(subject_id, kind, source)
            entries.append(current)
        elif source in SOURCES:
            pass
        material = False
        if not created and source == "scan":
            if kind == "grade":
                material = _grade_changed(current, fields)
            elif kind == "exam":
                material = _exam_changed(current, fields, today)
            elif kind == "fact":
                material = not similar(fields.get("text") or "", current.get("text") or "")
        err = _apply_fields(current, fields, today)
        if err:
            if created:
                entries.pop()
            return {"error": err}
        if created:
            current["source"] = source if source in SOURCES else "assistant"
        current["updatedAt"] = _now_ms()
        if ref:
            refs = [str(x) for x in (current.get("refs") or []) if x]
            if ref not in refs:
                refs.append(ref)
            current["refs"] = refs[-20:]
        state = _flag(current, source, created, material and source == "scan")
        _save(data)
        return {"ok": True, "state": state, "entry": _public(current), "created": created}


def delete_entry(entry_id: str, source: str = "assistant") -> dict:
    with _lock:
        data = _load()
        current = next((e for e in data["entries"] if e.get("id") == entry_id and e.get("active", True)), None)
        if not current:
            return {"error": "Запись журнала не найдена."}
        current["active"] = False
        current["updatedAt"] = _now_ms()
        _save(data)
    return {"ok": True, "id": entry_id, "deleted": True}


def ack_entry(entry_id: str) -> dict:
    with _lock:
        data = _load()
        current = next((e for e in data["entries"] if e.get("id") == entry_id and e.get("active", True)), None)
        if not current:
            return {"error": "Запись журнала не найдена."}
        current["acked"] = True
        current["reminded"] = True
        current["updatedAt"] = _now_ms()
        _save(data)
    return {"ok": True, "entry": _public(current)}


def mark_sent(announce_ids, remind_ids) -> None:
    announce, remind = set(announce_ids or []), set(remind_ids or [])
    if not announce and not remind:
        return
    with _lock:
        data = _load()
        changed = False
        for e in data["entries"]:
            if e.get("id") in announce:
                e["announced"] = True
                e["reminded"] = False
                changed = True
            elif e.get("id") in remind:
                e["reminded"] = True
                e["announced"] = True
                changed = True
        if changed:
            _save(data)


def user_apply(app, payload: dict) -> dict:
    """Правка из раздела «Учёба»."""
    action = str((payload or {}).get("action") or "").strip()
    if action == "add":
        fields = dict(payload or {})
        sid = str(fields.get("subjectId") or "").strip()
        subject = next((s for s in _subjects(app) if str(s.get("id")) == sid), None)
        if not subject:
            return {"error": "Предмет не найден."}
        fields["subject"] = subject.get("name")
        fields.pop("id", None)
        return write_entry(app, fields, source="user")
    if action == "update":
        return write_entry(app, payload or {}, source="user")
    if action == "delete":
        return delete_entry(str((payload or {}).get("id") or ""))
    if action == "ack":
        return ack_entry(str((payload or {}).get("id") or ""))
    return {"error": "Неизвестное действие."}


def execute(name: str, inp: dict, app: dict) -> dict:
    if not skill_on((app or {}).get("telegramSkills")):
        return {"error": "Навык «Журналы предметов» выключен в Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    if name == "study_journal_list":
        if inp.get("subject"):
            subject, err = resolve_subject(app, inp.get("subject"))
            if err:
                return {"error": err}
            rows = for_assistant(app)
            one = [g for g in rows if g.get("subjectId") == subject.get("id")]
            return {"subject": subject.get("name"), "journal": one[0] if one else {"subject": subject.get("name"), "facts": [], "grade": None, "exams": []}}
        return {"journals": for_assistant(app)}
    if name == "study_journal_write":
        res = write_entry(app, inp, source="assistant")
        if res.get("ok"):
            res["message"] = "Записал в журнал предмета."
        return res
    if name == "study_journal_delete":
        return delete_entry(str(inp.get("id") or ""))
    return {"error": "unknown_tool"}


# ── разбор находок проверки чатов ─────────────────────────────────────────────

def _guess_grade(text: str) -> dict:
    low = (text or "").casefold().replace("ё", "е")
    out = {}
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:из|/)\s*(\d+(?:[.,]\d+)?)", low)
    if m and re.search(r"балл|оцен|зачет|рейтинг", low):
        out["points"], out["points_max"] = _num(m.group(1)), _num(m.group(2))
    if "points" not in out:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*балл", low)
        if m:
            out["points"] = _num(m.group(1))
    m = re.search(r"(?:для зачет[а-я]*|чтобы сдать|допуск[а-я]*|нужно)\s*[:—-]?\s*(\d+(?:[.,]\d+)?)", low)
    if m:
        out["need"] = m.group(1)
    elif re.search(r"для зачет", low):
        tail = re.search(r"для зачет[а-я]*\s+([^.]{0,80})", text or "", re.I)
        if tail:
            out["need"] = tail.group(1).strip(" .,")
    if re.search(r"не\s+сдал|незачет|не\s+зачтено", low):
        out["passed"] = False
    elif re.search(r"\bсдал\b|зачтено|зачет\s+получен", low):
        out["passed"] = True
    return out


def _guess_exam(text: str, today: date) -> dict:
    low = (text or "").casefold().replace("ё", "е")
    if not re.search(r"зачет|экзамен|пересдач", low):
        return {}
    found = _find_date(text, today)
    deadline = ""
    m = re.search(r"(?:до|допуск[а-я]* до|крайний срок)\s+(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|\d{1,2}\s+[а-яё]+|20\d{2}-\d{2}-\d{2})", low)
    if m:
        deadline = _find_date(m.group(1), today)
        if deadline and deadline == found:
            # «до 18» — дедлайн, а дата зачёта пусть останется второй датой, если она есть
            rest = low.replace(m.group(0), " ", 1)
            other = _find_date(rest, today)
            if other and other != deadline:
                found = other
            elif not re.search(r"зачет|экзамен|пересдач", low.split(m.group(0))[0][-40:]):
                found, deadline = deadline, ""
    place = ""
    pm = re.search(r"(?:ауд\.?|аудитори[яи]|каб\.?|кабинет)\s*№?\s*([0-9]+[a-zа-я]?)", low)
    if pm:
        place = ("ауд. " + pm.group(1))
    form = ""
    for word in ("письменно", "устно", "тест", "онлайн", "дистанционно"):
        if word in low:
            form = word
            break
    note = ""
    if not found and not place and not deadline:
        return {}
    title = "Экзамен" if "экзамен" in low else ("Пересдача" if "пересдач" in low else "Зачёт")
    return {"text": title, "date": found, "place": place, "format": form, "deadline": deadline, "note": note,
            "time": _time(text)}


def _merge_dict(base: dict, extra: dict) -> dict:
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if v is None or v == "":
            continue
        out[k] = v
    return out


def pieces_from_item(item: dict, today: date = None) -> list:
    today = today or date.today()
    text = str(item.get("summary") or "").strip()
    grade_raw = item.get("grade") if isinstance(item.get("grade"), dict) else {}
    exam_raw = item.get("exam") if isinstance(item.get("exam"), dict) else {}
    grade = _merge_dict(_guess_grade(text), grade_raw)
    exam = _merge_dict(_guess_exam(text, today), exam_raw)
    out = []
    if any(grade.get(k) is not None and grade.get(k) != "" for k in ("points", "points_max", "need", "passed")):
        out.append({"kind": "grade", **grade})
    exam_fields = {"kind": "exam", "text": exam.get("text") or "", "date": exam.get("date") or "",
                   "time": exam.get("time") or "", "place": exam.get("place") or "", "format": exam.get("format") or "",
                   "deadline": exam.get("deadline") or "", "note": exam.get("note") or ""}
    if any(exam_fields.get(k) for k in ("date", "place", "deadline", "note")):
        out.append({k: v for k, v in exam_fields.items() if v or k == "kind"})
    if text and not any(similar(text, _piece_line(p)) for p in out):
        out.append({"kind": "fact", "text": text[:MAX_TEXT]})
    return out


def _piece_line(piece: dict) -> str:
    if piece.get("kind") == "grade":
        return grade_line({"points": _num(piece.get("points")), "pointsMax": _num(piece.get("points_max", piece.get("pointsMax"))),
                           "need": piece.get("need") or "", "passed": _boolish(piece.get("passed")) if "passed" in piece else None})
    if piece.get("kind") == "exam":
        return exam_line(piece)
    return str(piece.get("text") or "")


def _personal(item: dict) -> bool:
    """Прямое обращение не прячем за «уже было»: человека зовут сейчас."""
    why = str(item.get("why") or "").casefold().replace("ё", "е")
    return "упомянул" in why or "ответ на ваше" in why


def prepare_scan(app, items: list, today: date = None) -> dict:
    """Пишет находки в журналы. novel — слать как новое, reminders — коротко напомнить."""
    today = today or date.today()
    novel, reminders, announce_ids, remind_ids = [], [], [], []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        subject = _match_item_subject(app, item)
        pieces = pieces_from_item(item, today) if subject else []
        if not subject or not pieces:
            novel.append(item)
            continue
        states = []
        ref = f"{item.get('source') or ''}:{item.get('chat_id')}:{item.get('message_id')}"
        for piece in pieces:
            res = write_entry(app, {**piece, "subject": subject.get("name")}, source="scan", ref=ref, today=today)
            if not res.get("ok"):
                continue
            states.append((res["state"], res["entry"]))
        if not states:
            novel.append(item)
            continue
        if _personal(item) or any(st == "new" for st, _e in states):
            novel.append(item)
            announce_ids.extend(e["id"] for st, e in states if st == "new")
            continue
        remind_rows = [(st, e) for st, e in states if st == "remind"]
        if remind_rows:
            lines = []
            for _st, e in remind_rows:
                line = entry_line(e)
                if line and line not in lines:
                    lines.append(line)
                remind_ids.append(e["id"])
            reminders.append({
                "item": item, "subject": subject.get("name") or "",
                "line": " · ".join(lines)[:400], "ids": [e["id"] for _st, e in remind_rows],
            })
    return {"novel": novel, "reminders": reminders, "announce_ids": announce_ids, "remind_ids": remind_ids}


def reminder_html(reminders: list) -> str:
    import html
    rows = []
    for r in (reminders or [])[:8]:
        rows.append(f"• <b>{html.escape(str(r.get('subject') or 'Предмет'))}</b>: {html.escape(str(r.get('line') or ''))}")
    if not rows:
        return ""
    return ("📚 <b>Уже есть в журнале</b>\n" + "\n".join(rows)
            + "\n\n<i>Это не новое. Обратили внимание?</i>")
