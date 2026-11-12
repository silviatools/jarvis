"""
health_skill.py — навык «Медицинские документы → Здоровье».

Пользователь присылает в бот фото медицинских документов («внеси в здоровье»). Все фото одного сообщения или
альбома — один разбор:
  1. модель с изображениями (роль healthExtract в Настройки → Ассистент → «Модели по ролям») делает полную
     расшифровку каждого фото, отмечая неразборчивое и рукописное;
  2. та же роль раскладывает расшифровку по сущностям: визит, исследование, анализы, памятки (диета, подготовка),
     назначения, препараты с этапами курса, будущие визиты;
  3. сервер ищет повторы (хэш фото, тот же визит/протокол), закрывает выполненные назначения, подбирает эпизод
     (случай болезни) и формирует вопросы — по одному, с кнопками;
  4. сводка «что найдено и куда ляжет» с кнопкой «Записать»; без неё ничего не пишется;
  5. после записи — предложение напоминаний через plan_skill (явка, смена этапа курса), только по кнопке.

Черновики разборов — DATA_DIR/jarvis_health_drafts.json (в бэкапе). Полный текст документов не попадает
в журналы: в print — только id и счётчики, в журнал расхода ИИ — только токены.
"""

import hashlib
import html
import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SKILL_ID = "health_docs"
STORE_NAME = "jarvis_health_drafts.json"
ROLE = "healthExtract"
MAX_PHOTOS = 10
DRAFT_TTL_S = 3 * 86400
MAX_DRAFTS = 40
MAX_MODEL_QUESTIONS = 4
MSK = timezone(timedelta(hours=3))
_lock = threading.RLock()

SKILLS = [
    {"id": SKILL_ID, "title": "Медицинские документы → «Здоровье»", "risk": "write", "default": True,
     "desc": (
         "Пришлите в бот фото медицинских документов (можно сразу несколько или альбомом) с подписью «внеси в здоровье». "
         "Джарвис распознает всё (модель роли «Здоровье — распознавание фото» в «Моделях по ролям»), поймёт, что это — приём врача, "
         "протокол УЗИ, анализы, диета, памятка подготовки — и разложит по разделу: диагноз с МКБ, показатели с нормами, "
         "назначенные обследования, курсы препаратов по этапам, повторную явку. Неясное спросит кнопками, предложит связать "
         "документы в эпизод (случай болезни), покажет сводку и запишет только после «Записать». Повторно присланный документ "
         "не дублируется. После записи предложит напоминания (явка, смена дозы) — ставит только по кнопке. "
         "Рукописное и неразборчивое помечает, не додумывает. PDF пока не разбирает — только фото и картинки."
     ),
     "tools": ["health_import", "health_import_answer", "health_import_commit", "health_import_cancel"]},
]

_S = {"type": "string"}
TOOLS = [
    {"name": "health_import",
     "description": (
         "Разобрать фото медицинских документов из текущего сообщения (все фото сообщения/альбома — один разбор) и подготовить "
         "запись в «Здоровье». Инструмент сам пришлёт в чат вопросы и сводку с кнопкой «Записать»; ничего не пишет без неё. "
         "note — что сказал пользователь о документах (к чему относятся, что за случай), если сказал."),
     "input_schema": {"type": "object", "properties": {"note": _S}, "required": []}},
    {"name": "health_import_answer",
     "description": (
         "Ответ пользователя текстом на вопрос разбора медицинских документов или правка разбора. question_id + answer — ответ на "
         "вопрос; episode_title — назвать новый эпизод так; episode_id — привязать к существующему эпизоду; no_episode — без эпизода; "
         "edits — правки полей [{doc: номер документа с 1, field: date|time|doctor|specialty|clinic|title|diagnosis|conclusion|"
         "recommendations|notes, value}]. После вызова инструмент сам пришлёт следующий вопрос или сводку."),
     "input_schema": {"type": "object", "properties": {
         "draft_id": _S, "question_id": _S, "answer": _S, "episode_title": _S, "episode_id": _S,
         "no_episode": {"type": "boolean"},
         "edits": {"type": "array", "items": {"type": "object", "properties": {
             "doc": {"type": "integer"}, "field": _S, "value": _S}, "required": ["doc", "field", "value"]}}},
         "required": ["draft_id"]}},
    {"name": "health_import_commit",
     "description": "Записать подготовленный разбор в «Здоровье». Только после явного «да/записывай» пользователя: confirmed:true.",
     "input_schema": {"type": "object", "properties": {"draft_id": _S, "confirmed": {"type": "boolean"}}, "required": ["draft_id"]}},
    {"name": "health_import_cancel",
     "description": "Отменить разбор медицинских документов (ничего не записывать).",
     "input_schema": {"type": "object", "properties": {"draft_id": _S}, "required": ["draft_id"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
assert not _missing, _missing

# Зависимости из jarvis.py (bind): чтение/запись данных, вызов модели, Telegram, планировщик.
_deps = {}


def bind(**kw):
    _deps.update(kw)


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


def now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(MSK)


def today_iso() -> str:
    return now_msk().strftime("%Y-%m-%d")


def prompt_section(state=None) -> str:
    if not skill_on(state):
        return ""
    return (
        "13. ЗДОРОВЬЕ ИЗ ФОТО (health_import / health_import_answer / health_import_commit / health_import_cancel). "
        "Фото или сканы медицинских документов (приём врача, УЗИ, анализы, выписка, рецепт, диета, памятка) с просьбой "
        "«внеси/запиши в здоровье» или явно для сохранения → ОДИН вызов health_import на всё сообщение (все фото — один разбор). "
        "Не раскладывай документы сам через create_record и не пересказывай их: инструмент сам распознает, спросит неясное "
        "кнопками и пришлёт сводку с кнопкой «Записать». После него ответь одной короткой фразой. Текстовый ответ пользователя "
        "на вопрос разбора или правка (дата, доза, название эпизода, «без эпизода») → health_import_answer. «Да, записывай» "
        "текстом → health_import_commit(confirmed:true). Медицинские данные не выдумывай; назначения и курсы препаратов "
        "напоминаниями без кнопки или явной просьбы не ставь.\n\n"
    )


# ── Хранилище черновиков ─────────────────────────────────────────────────

def _dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return _dir() / STORE_NAME


def _load() -> dict:
    try:
        d = json.loads(store_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(drafts: dict):
    now = time.time()
    for k in list(drafts):
        d = drafts[k]
        if now - d.get("created", 0) > DRAFT_TTL_S:
            if d.get("status") == "pending":
                _drop_photos(d)
            drafts.pop(k)
    if len(drafts) > MAX_DRAFTS:
        for k in sorted(drafts, key=lambda k: drafts[k].get("created", 0))[: len(drafts) - MAX_DRAFTS]:
            drafts.pop(k)
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get_draft(draft_id: str):
    with _lock:
        return _load().get(str(draft_id or ""))


def put_draft(d: dict):
    with _lock:
        drafts = _load()
        drafts[d["id"]] = d
        _save(drafts)


def pending_for(chat_id):
    """Последний ещё не записанный разбор этого чата."""
    with _lock:
        drafts = [d for d in _load().values() if d.get("status") == "pending" and str(d.get("chat_id")) == str(chat_id)]
    return max(drafts, key=lambda d: d.get("created", 0)) if drafts else None


def pending_note(chat_id, state=None) -> str:
    if not skill_on(state):
        return ""
    d = pending_for(chat_id)
    if not d:
        return ""
    q = _current_question(d)
    if q:
        opts = "; ".join(q.get("options") or [])
        return (f"\n\nЖдёт ответа разбор медицинских документов draft_id={d['id']}, вопрос question_id={q['id']}: «{q['text']}»"
                + (f" (варианты: {opts})" if opts else "")
                + ". Если сообщение пользователя — ответ на него или правка разбора, вызови health_import_answer.")
    return (f"\n\nЖдёт подтверждения разбор медицинских документов draft_id={d['id']} (сводка с кнопкой «Записать» уже в чате). "
            "Правка → health_import_answer; «да, записывай» → health_import_commit(confirmed:true); «не надо» → health_import_cancel.")


# ── Фото ─────────────────────────────────────────────────────────────────

def _photos_dir() -> Path:
    return _dir() / "photos"


def _save_photos(images: list) -> list:
    out = []
    import base64
    for i, im in enumerate(images, 1):
        try:
            raw = base64.b64decode(im.get("b64") or "")
        except Exception:
            raw = b""
        sha = hashlib.sha256(raw).hexdigest()
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(im.get("media_type"), "jpg")
        name = f"{uuid.uuid4()}.{ext}"
        try:
            _photos_dir().mkdir(parents=True, exist_ok=True)
            (_photos_dir() / name).write_bytes(raw)
        except OSError:
            name = ""
        out.append({"n": i, "sha": sha, "file": name})
    return out


def _drop_photos(d: dict, keep: set = frozenset()):
    for p in d.get("photos") or []:
        if p.get("file") and p["file"] not in keep:
            try:
                (_photos_dir() / p["file"]).unlink()
            except OSError:
                pass


# ── Промпты модели ───────────────────────────────────────────────────────

TRANSCRIBE_SYSTEM = """Ты расшифровываешь фотографии медицинских документов дословно, для последующей структурной записи.
Правила:
- Для каждого фото начни раздел строкой «=== Фото N ===» (N — номер фото по порядку) и перепиши ВЕСЬ текст документа сверху вниз: шапку (клиника, адрес), дату и время, коды услуг, диагнозы с кодами МКБ, таблицы, нормы, заключение, рекомендации, назначения, подписи, должность и ФИО врача.
- Таблицы и размеры органов — построчно: «Показатель: значение единица (норма …)». Ничего не сокращай и не пересказывай.
- Рукописные вставки помечай [рукописно: текст]. Неразборчивое — [неразборчиво: что видно], без догадок. Обрезанный край — [обрезано].
- Данные пациента (ФИО, дата рождения, номера карты, ЭМК, полиса, телефон) не переписывай — одна строка «Пациент: [данные пациента], пол, возраст», если они указаны.
- Печати и штампы — [печать: что читается]. Латинские рецепты (Rp., D.t.d., S.) переписывай как есть.
- Ничего не добавляй от себя, не толкуй и не исправляй медицинские данные. Ответ — только расшифровка."""

STRUCTURE_SYSTEM = """Ты раскладываешь расшифровку медицинских документов по сущностям приложения «Здоровье». Используй только то, что есть в расшифровке, ничего не придумывай. Нет данных — пустая строка или пустой массив.
Верни СТРОГО JSON без markdown:
{"documents":[{"photos":[1],"kind":"visit|study|labs|memo|procedure|other","title":"","date":"YYYY-MM-DD","time":"HH:MM","clinic":"","doctor":"","specialty":"","complaints":"","anamnesis":"","diagnosis":"","icd":[""],"study_kind":"","service_code":"","metrics":[{"group":"","name":"","value":"","unit":"","ref":"","flag":""}],"conclusion":"","recommendations":"","orders":[{"title":"","kind":"lab|study|consult|visit|procedure|other","code":"","group":"","due_date":"","due_time":"","handwritten":false,"note":""}],"medications":[{"name":"","form":"","dosage":"","route":"","schedule":"","duration":"","stages":[{"dosage":"","schedule":"","duration":""}],"quantity":"","notes":""}],"memo":{"kind":"diet|prep|other","for":"","principles":[],"allowed":[],"excluded":[],"steps":[]},"uncertain":[{"field":"","text":"","reason":"неразборчиво|рукописно|противоречие|обрезано"}]}],
"episode":{"title":"","reason":"","existing_id":""},
"questions":[{"doc":1,"field":"","text":"","options":["",""]}],
"missing":[""]}
Пояснения:
- documents — по одному на каждый самостоятельный документ; photos — номера фото этого документа. Несколько страниц одного документа — один документ.
- kind: visit — приём/консультация врача; study — инструментальное исследование (УЗИ, ЭГДС, МРТ, КТ, рентген, ЭКГ); labs — результаты анализов; memo — памятка, диета, подготовка к исследованию; procedure — процедура/операция.
- title: коротко, например «Приём гастроэнтеролога», «УЗИ брюшной полости», «Принципы питания», «Подготовка к УЗИ и ЭГДС».
- date/time — дата и время приёма/исследования/забора (ДД.ММ.ГГГГ → ГГГГ-ММ-ДД). У памятки без даты — пусто.
- diagnosis — текст диагноза как в документе; icd — коды МКБ-10 из документа (например K30), без догадок.
- metrics — каждый измеренный показатель отдельно: group — орган/раздел («Печень», «Общий анализ крови»), name, value — число или текст как в документе, unit, ref — норма/референс как в документе, flag — high|low|abnormal, если значение вне нормы, иначе пусто. Качественные находки без чисел опиши в conclusion или recommendations, не в metrics.
- conclusion — заключение исследования дословно; recommendations — рекомендации и режим, которые не попали в orders/medications.
- orders — назначенные обследования, анализы, консультации, повторная явка: каждый пункт отдельно, code — код услуги (T01.005), group — раздел («Биохимический анализ крови»), kind=visit — повторная явка/следующий приём (due_date — её дата), consult — направление к другому врачу. Рукописное — handwritten:true.
- medications — назначенные препараты: dosage (200 мг), route (внутрь, в/м, в/в…), schedule (по 1 таб. 3 раза в день за 15 мин до еды), duration (4 недели), stages — если схема меняется по этапам, перечисли этапы по порядку (первый этап тоже), quantity (№10).
- memo — только для kind=memo: diet — что можно (allowed), что исключить (excluded), общие принципы (principles); prep — подготовка: for — к какому исследованию, steps — шаги по порядку.
- uncertain — всё неразборчивое, рукописное, противоречивое, с указанием поля.
- episode — к какому случаю болезни относятся документы: title — предложи короткое название по смыслу и жалобам (например «Обследование ЖКТ, ноябрь 2026»). Если в списке открытых эпизодов есть подходящий — existing_id.
- questions — только по-настоящему неясное, что мешает правильно записать (неразборчивая доза или дата, противоречие, непонятно, к чему относится документ): каждый вопрос про один пункт, коротко, 2–4 варианта ответа по 1–4 слова, если уместно. doc — номер документа с 1, field — поле (date, time, doctor, diagnosis, medications.1.dosage …). Не спрашивай то, что в документе написано ясно.
- missing — чего важного не хватает для полноты (например, результатов назначенных анализов нет среди документов), коротко."""


def _strip_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        raise ValueError("нет JSON")
    return json.loads(t[i:j + 1])


# ── Нормализация ────────────────────────────────────────────────────────

def _s(v, n=2000) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _cut(v, n: int) -> str:
    s = _s(v, 400)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0] if " " in s[:n] else s[:n]
    return cut.rstrip(",.;:") + "…"


def _date(v) -> str:
    s = _s(v, 40)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return ""
    return ""


def _time(v) -> str:
    m = re.match(r"^(\d{1,2})[:.](\d{2})", _s(v, 10))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 else ""


def _num(v):
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(v or ""))
    return float(m.group(0).replace(",", ".")) if m else None


_NUM = r"(\d+(?:[.,]\d+)?)"


def ref_flag(value, ref) -> str:
    """Флаг вне нормы по референсу: «a–b», «до X», «< X», «от X», «> X». Неоднозначный референс — пусто."""
    v = _num(value)
    r = str(ref or "").lower().replace("—", "-").replace("–", "-").replace(",", ".")
    if v is None or not r.strip():
        return ""
    if len(re.findall(r"\d+(?:\.\d+)?", r)) > 2 or re.search(r"муж|жен|дет", r):
        return ""
    f = lambda s: float(s.replace(",", "."))  # noqa: E731
    m = re.search(r"до\s*" + _NUM + r"\s*-\s*" + _NUM, r)
    if m:
        return "high" if v > f(m.group(2)) else ""
    m = re.search(_NUM + r"\s*-\s*" + _NUM, r)
    if m:
        lo, hi = f(m.group(1)), f(m.group(2))
        return "low" if v < lo else "high" if v > hi else ""
    m = re.search(r"(?:до|<|≤|не более|менее)\s*=?\s*" + _NUM, r)
    if m:
        return "high" if v > f(m.group(1)) else ""
    m = re.search(r"(?:от|>|≥|не менее|более)\s*=?\s*" + _NUM, r)
    if m:
        return "low" if v < f(m.group(1)) else ""
    return ""


_KINDS = {"visit": "visit", "study": "study", "labs": "labs", "memo": "memo", "procedure": "procedure"}
_ORDER_KINDS = ("lab", "study", "consult", "visit", "procedure", "other")


def _norm_med(m: dict) -> dict:
    stages = []
    for st in (m.get("stages") or [])[:6]:
        if isinstance(st, dict) and any(_s(st.get(k)) for k in ("dosage", "schedule", "duration")):
            stages.append({"dosage": _s(st.get("dosage"), 120), "schedule": _s(st.get("schedule"), 300),
                           "duration": _s(st.get("duration"), 80)})
    out = {"id": str(uuid.uuid4()), "name": _s(m.get("name"), 200), "form": _s(m.get("form"), 80),
           "dosage": _s(m.get("dosage"), 120), "route": _s(m.get("route"), 60), "schedule": _s(m.get("schedule"), 300),
           "duration": _s(m.get("duration"), 80), "quantity": _s(m.get("quantity"), 60), "notes": _s(m.get("notes"), 400),
           "stages": stages if len(stages) > 1 else [], "status": "active"}
    if stages and not (out["dosage"] or out["schedule"] or out["duration"]):
        out.update(dosage=stages[0]["dosage"], schedule=stages[0]["schedule"], duration=stages[0]["duration"])
    return out


def normalize_doc(raw: dict, n_photos: int) -> dict:
    kind = _KINDS.get(_s(raw.get("kind")).lower(), "note")
    photos = sorted({int(p) for p in (raw.get("photos") or []) if str(p).isdigit() and 1 <= int(p) <= n_photos})
    metrics = []
    for m in (raw.get("metrics") or [])[:80]:
        if not isinstance(m, dict) or not _s(m.get("name")):
            continue
        mm = {"id": str(uuid.uuid4()), "group": _s(m.get("group"), 80), "name": _s(m.get("name"), 160),
              "value": _s(m.get("value"), 80), "unit": _s(m.get("unit"), 30), "ref": _s(m.get("ref"), 120)}
        model_flag = _s(m.get("flag")).lower()
        mm["flag"] = ref_flag(mm["value"], mm["ref"]) or (model_flag if model_flag in ("high", "low", "abnormal") else "")
        metrics.append(mm)
    orders = []
    for o in (raw.get("orders") or [])[:60]:
        if not isinstance(o, dict) or not _s(o.get("title")):
            continue
        k = _s(o.get("kind")).lower()
        orders.append({"id": str(uuid.uuid4()), "title": _s(o.get("title"), 240), "kind": k if k in _ORDER_KINDS else "other",
                       "code": _s(o.get("code"), 40), "group": _s(o.get("group"), 120), "status": "assigned",
                       "dueDate": _date(o.get("due_date")), "dueTime": _time(o.get("due_time")),
                       "handwritten": bool(o.get("handwritten")), "note": _s(o.get("note"), 300), "resultEventId": ""})
    meds = [_norm_med(m) for m in (raw.get("medications") or [])[:20] if isinstance(m, dict) and _s(m.get("name"))]
    memo = None
    if kind == "memo":
        mr = raw.get("memo") if isinstance(raw.get("memo"), dict) else {}
        mk = _s(mr.get("kind")).lower()
        lst = lambda k: [_s(x, 400) for x in (mr.get(k) or []) if _s(x)][:60]  # noqa: E731
        memo = {"kind": mk if mk in ("diet", "prep") else "other", "for": _s(mr.get("for"), 200),
                "principles": lst("principles"), "allowed": lst("allowed"), "excluded": lst("excluded"), "steps": lst("steps")}
    unc = []
    for u in (raw.get("uncertain") or [])[:20]:
        if isinstance(u, dict) and _s(u.get("text")):
            unc.append({"field": _s(u.get("field"), 60), "text": _s(u.get("text"), 300), "reason": _s(u.get("reason"), 40)})
    for o in orders:
        head = (o["title"].split() or [""])[0].lower()
        if o["handwritten"] and not any(o["title"][:20].lower() in u["text"].lower()
                                        or (u["field"] == "orders" and head in u["text"].lower()) for u in unc):
            unc.append({"field": "orders", "text": o["title"], "reason": "рукописно"})
    icd = [c.upper() for c in (_s(x, 12) for x in (raw.get("icd") or [])) if re.match(r"^[A-ZА-Я]\d{2}(\.\d{1,2})?$", c.upper())]
    doc = {"kind": kind, "photos": photos, "title": _s(raw.get("title"), 160), "date": _date(raw.get("date")),
           "time": _time(raw.get("time")), "clinic": _s(raw.get("clinic"), 200), "doctor": _s(raw.get("doctor"), 160),
           "specialty": _s(raw.get("specialty"), 80), "complaints": _s(raw.get("complaints"), 1500),
           "anamnesis": _s(raw.get("anamnesis"), 1500), "diagnosis": _s(raw.get("diagnosis"), 800), "icd": icd,
           "studyKind": _s(raw.get("study_kind"), 200), "serviceCode": _s(raw.get("service_code"), 40),
           "metrics": metrics, "conclusion": _s(raw.get("conclusion"), 2000), "recommendations": _s(raw.get("recommendations"), 2000),
           "orders": orders, "medications": meds, "memo": memo, "uncertain": unc, "notes": ""}
    if not doc["title"]:
        doc["title"] = {"visit": "Приём " + (doc["specialty"] or "врача"), "study": doc["studyKind"] or "Исследование",
                        "labs": "Анализы", "memo": "Памятка"}.get(kind, "Медицинский документ")
    return doc


# ── Сравнение текстов ────────────────────────────────────────────────────

_STOP = {"для", "при", "или", "без", "под", "над", "врача", "приём", "прием", "первичный", "повторный", "осмотр",
         "консультация", "органов", "исследование", "анализ", "общий", "фл"}


def _tokens(s: str) -> set:
    words = re.findall(r"[a-zа-яё0-9]+", (s or "").lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 3 and w not in _STOP}


def _similar(a: str, b: str, need: float = 0.6) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= need


_ALIASES = [(r"\bузи\b|ультразвук", "узи"), (r"эгдс|эзофагогастродуоденоскоп|гастроскоп", "эгдс"),
            (r"брюшн|\bобп\b", "брюшн"), (r"\bоак\b|клинический анализ крови|общий \(клинический\) анализ крови", "оак")]


def _canon(s: str) -> str:
    t = (s or "").lower()
    for rx, rep in _ALIASES:
        t = re.sub(rx, " " + rep + " ", t)
    return t


def _event_key_text(ev: dict) -> str:
    return " ".join(filter(None, [ev.get("specialty"), ev.get("studyKind"), ev.get("title")]))


# ── Повторы, назначения, эпизод ─────────────────────────────────────────

def _find_duplicate(doc: dict, photos: list, events: list):
    """→ (event, mode): same — уже есть целиком (те же фото), merge — тот же визит/протокол (дополнить)."""
    hashes = {p["sha"] for p in photos if p["n"] in doc["photos"]}
    for ev in events:
        known = set(((ev.get("source") or {}).get("hashes")) or [])
        if hashes and hashes <= known:
            return ev, "same"
    typ = doc["kind"]
    for ev in events:
        if ev.get("type") != typ:
            continue
        if typ == "memo":
            em = ev.get("memo") or {}
            if em.get("kind") == (doc["memo"] or {}).get("kind") and _similar(_canon(ev.get("title")), _canon(doc["title"]), 0.7):
                return ev, "merge"
            continue
        if not doc["date"] or ev.get("date") != doc["date"]:
            continue
        if doc["time"] and ev.get("time") and doc["time"] != ev.get("time"):
            continue
        if doc["doctor"] and ev.get("doctor") and not _similar(doc["doctor"], ev["doctor"], 0.5):
            continue
        if _similar(_canon(_event_key_text(ev)), _canon(" ".join([doc["specialty"], doc["studyKind"], doc["title"]])), 0.5):
            return ev, "merge"
    return None, ""


def _merge_into(ev: dict, doc: dict) -> int:
    """Дополняет существующую запись тем, чего в ней нет. → число добавленных пунктов."""
    added = 0
    for k_doc, k_ev in (("time", "time"), ("clinic", "clinic"), ("doctor", "doctor"), ("specialty", "specialty"),
                        ("complaints", "symptoms"), ("diagnosis", "diagnosis"), ("studyKind", "studyKind"),
                        ("conclusion", "conclusion"), ("recommendations", "recommendations"), ("anamnesis", "anamnesis")):
        if doc.get(k_doc) and not ev.get(k_ev):
            ev[k_ev] = doc[k_doc]
            added += 1
    icd = list(ev.get("icd") or [])
    for c in doc["icd"]:
        if c not in icd:
            icd.append(c)
            added += 1
    ev["icd"] = icd
    for key, ident in (("metrics", lambda x: (x.get("group", "").lower(), x.get("name", "").lower())),
                       ("orders", lambda x: (x.get("code") or x.get("title", "").lower())),
                       ("medications", lambda x: x.get("name", "").lower())):
        cur = list(ev.get(key) or [])
        have = {ident(x) for x in cur}
        for x in doc[key]:
            if ident(x) not in have:
                cur.append(x)
                have.add(ident(x))
                added += 1
        ev[key] = cur
    if doc.get("memo"):
        memo = dict(ev.get("memo") or {"kind": doc["memo"]["kind"]})
        for k in ("principles", "allowed", "excluded", "steps"):
            cur = list(memo.get(k) or [])
            for x in doc["memo"][k]:
                if x not in cur:
                    cur.append(x)
                    added += 1
            memo[k] = cur
        ev["memo"] = memo
    return added


def _order_matches(order: dict, doc: dict) -> bool:
    if order.get("status") != "assigned" or doc["kind"] not in ("study", "labs", "visit"):
        return False
    if doc["kind"] == "visit" and order.get("kind") not in ("visit", "consult"):
        return False
    if doc["kind"] in ("study", "labs") and order.get("kind") in ("visit", "consult"):
        return False
    if order.get("code") and doc.get("serviceCode") and order["code"] == doc["serviceCode"]:
        return True
    text = _canon(" ".join([doc["studyKind"], doc["title"]] + ([doc["specialty"]] if doc["kind"] == "visit" else [])))
    return _similar(_canon(order.get("title")), text, 0.6)


def _plan_order_closures(docs: list, events: list) -> list:
    """Какие назначенные обследования закрывают присланные результаты: [{doc, eventId|None, docRef, orderId, title}]."""
    out = []
    for i, doc in enumerate(docs):
        if doc.get("skip") or doc["kind"] not in ("study", "labs", "visit"):
            continue
        for ev in events:
            if ev.get("date") and doc["date"] and ev["date"] > doc["date"]:
                continue
            for o in ev.get("orders") or []:
                if _order_matches(o, doc):
                    out.append({"doc": i, "eventId": ev["id"], "orderId": o["id"], "title": o["title"]})
        for j, other in enumerate(docs):
            if j == i or other.get("skip"):
                continue
            if other["date"] and doc["date"] and other["date"] > doc["date"]:
                continue
            for o in other["orders"]:
                if _order_matches(o, doc):
                    out.append({"doc": i, "docRef": j, "orderId": o["id"], "title": o["title"]})
    seen, uniq = set(), []
    for c in out:
        if c["orderId"] not in seen:
            seen.add(c["orderId"])
            uniq.append(c)
    return uniq


def _episode_candidates(app: dict, docs: list, hint: dict) -> list:
    conds = [c for c in (app.get("healthConditions") or []) if c.get("status", "active") == "active"]
    events = app.get("healthEvents") or []
    dates = sorted(d["date"] for d in docs if d["date"]) or [today_iso()]
    ref = datetime.strptime(dates[0], "%Y-%m-%d").date()
    words = " ".join([hint.get("title") or ""] + [d["diagnosis"] + " " + d["complaints"] + " " + d["title"] for d in docs])
    icds = {c for d in docs for c in d["icd"]}
    scored = []
    for c in conds:
        linked = [e for e in events if e.get("conditionId") == c["id"]]
        last = max([e.get("date") or "" for e in linked] + [c.get("diagnosedDate") or ""])
        score = 0
        if c["id"] == (hint.get("existing_id") or ""):
            score += 3
        if _similar(words, " ".join([c.get("name", ""), c.get("notes", ""), c.get("symptoms", "")] +
                                    [e.get("diagnosis", "") for e in linked]), 0.25):
            score += 2
        if icds & {x for e in linked for x in (e.get("icd") or [])}:
            score += 3
        if not c.get("isChronic") and last:
            try:
                gap = abs((ref - datetime.strptime(last[:10], "%Y-%m-%d").date()).days)
                score += 1 if gap <= 45 else 0
            except ValueError:
                pass
        if score >= (2 if c.get("isChronic") else 1):
            scored.append((score, last, c))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [{"id": c["id"], "name": c.get("name") or "Эпизод", "since": c.get("diagnosedDate") or "", "score": s}
            for s, _, c in scored[:3]]


def _short_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return ""


def _default_episode_title(docs: list, hint: dict) -> str:
    t = _s(hint.get("title"), 80)
    if t:
        return t
    dx = next((d["diagnosis"] for d in docs if d["diagnosis"]), "")
    if dx:
        return dx[:60]
    d0 = next((d["date"] for d in docs if d["date"]), today_iso())
    months = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    return f"Обследование, {months[int(d0[5:7]) - 1]} {d0[:4]}"


def _build_questions(draft: dict, parsed: dict):
    docs = draft["docs"]
    qs = []
    for q in (parsed.get("questions") or [])[:MAX_MODEL_QUESTIONS]:
        if not isinstance(q, dict) or not _s(q.get("text")):
            continue
        try:
            di = int(q.get("doc") or 0) - 1
        except (TypeError, ValueError):
            di = -1
        opts = [_s(o, 40) for o in (q.get("options") or []) if _s(o)][:4]
        qs.append({"id": f"q{len(qs) + 1}", "kind": "field", "doc": di if 0 <= di < len(docs) else -1,
                   "field": _s(q.get("field"), 60), "text": _s(q.get("text"), 300), "options": opts, "answer": None})
    for i, d in enumerate(docs):
        if d.get("skip") or d["date"] or d["kind"] == "memo":
            continue
        if any(q["doc"] == i and q["field"] == "date" for q in qs):
            continue
        known = sorted({x["date"] for x in docs if x["date"]})
        opts = [_short_date(x) for x in known[:2]] + ["Сегодня"]
        qs.append({"id": f"q{len(qs) + 1}", "kind": "field", "doc": i, "field": "date",
                   "text": f"Какая дата у документа «{d['title']}»? На фото её не видно.", "options": opts, "answer": None})
    active = [d for d in docs if not d.get("skip")]
    if active:
        cands = draft["episode"]["candidates"]
        title = draft["episode"]["suggested"]
        opts, acts = [], []
        for c in cands[:2]:
            opts.append(f"К «{_cut(c['name'], 30)}»")
            acts.append({"mode": "link", "id": c["id"], "title": c["name"]})
        opts.append(f"Новый: «{_cut(title, 30)}»")
        acts.append({"mode": "new", "title": title})
        opts.append("Без эпизода")
        acts.append({"mode": "none"})
        if cands:
            c = cands[0]
            text = (f"Похоже на продолжение эпизода «{c['name']}»" + (f" (с {_short_date(c['since'])})" if c["since"] else "")
                    + ". Привязать документы к нему?")
        else:
            text = (f"Связать документы в эпизод (случай болезни)? Предлагаю создать «{title}». "
                    "Название можно поправить — просто напишите своё.")
        qs.append({"id": f"q{len(qs) + 1}", "kind": "episode", "doc": -1, "field": "episode", "text": text,
                   "options": opts, "actions": acts, "answer": None})
    return qs


def _current_question(d: dict):
    return next((q for q in d.get("questions") or [] if q.get("answer") is None), None)


def _missing_lines(draft: dict, app: dict) -> list:
    docs = [d for d in draft["docs"] if not d.get("skip")]
    closed = {c["orderId"] for c in draft.get("closures") or []}
    pending = [o for d in docs for o in d["orders"] if o["kind"] in ("lab", "study", "procedure") and o["id"] not in closed]
    ep = draft["episode"]
    if ep.get("mode") == "link":
        for ev in app.get("healthEvents") or []:
            if ev.get("conditionId") == ep.get("id"):
                pending += [o for o in ev.get("orders") or [] if o.get("status") == "assigned"
                            and o.get("kind") in ("lab", "study", "procedure") and o["id"] not in closed]
    out = []
    if pending:
        names = []
        for o in pending:
            n = o.get("group") or o["title"]
            if n not in names:
                names.append(n)
        out.append("Результатов назначенного пока нет: " + ", ".join(names[:5]) + (f" и ещё {len(names) - 5}" if len(names) > 5 else "")
                   + ". Пришлите, когда будут.")
    for m in draft.get("model_missing") or []:
        if m and not any(_similar(m, x, 0.6) for x in out):
            out.append(m)
    return out[:4]


# ── Разбор ───────────────────────────────────────────────────────────────

def _split_transcript(text: str) -> dict:
    parts, cur, buf = {}, None, []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*=+\s*Фото\s+(\d+)\s*=+\s*$", line, re.I)
        if m:
            if cur is not None:
                parts[cur] = "\n".join(buf).strip()
            cur, buf = int(m.group(1)), []
        else:
            buf.append(line)
    if cur is not None:
        parts[cur] = "\n".join(buf).strip()
    elif text:
        parts[1] = text.strip()
    return parts


def _open_episodes_line(app: dict) -> str:
    rows = [c for c in (app.get("healthConditions") or []) if c.get("status", "active") == "active"][:12]
    if not rows:
        return "Открытых эпизодов нет."
    return "Открытые эпизоды и состояния: " + "; ".join(
        f"id={c['id']} «{c.get('name', '')}»" + (f" с {c['diagnosedDate']}" if c.get("diagnosedDate") else "")
        + (" (хроническое)" if c.get("isChronic") else "") for c in rows)


def analyze(app: dict, images: list, note: str = "", chat_id=None) -> dict:
    """Расшифровка → сущности → черновик (без записи в данные)."""
    llm = _deps["llm"]
    images = images[:MAX_PHOTOS]
    transcript = llm(app, TRANSCRIBE_SYSTEM,
                     f"Фото по порядку: {len(images)}. Расшифруй каждое." + (f"\nПояснение пользователя: {note}" if note else ""),
                     images, 8000, "health_transcribe")
    if not transcript.strip():
        raise ValueError("модель не вернула расшифровку")
    raw = llm(app, STRUCTURE_SYSTEM,
              f"Сегодня {today_iso()}. Фото: {len(images)}.\n{_open_episodes_line(app)}\n"
              + (f"Пояснение пользователя: {note}\n" if note else "") + "\nРасшифровка:\n" + transcript,
              None, 8000, "health_structure")
    parsed = _strip_json(raw)
    photos = _save_photos(images)
    docs = [normalize_doc(d, len(images)) for d in (parsed.get("documents") or []) if isinstance(d, dict)]
    if not docs:
        _drop_photos({"photos": photos})
        raise ValueError("в документах не нашлось медицинских данных")
    used = {n for d in docs for n in d["photos"]}
    for p in photos:
        if p["n"] not in used:
            target = docs[min(p["n"], len(docs)) - 1]
            target["photos"] = sorted(set(target["photos"]) | {p["n"]})
    parts = _split_transcript(transcript)
    for d in docs:
        d["sourceText"] = "\n\n".join(parts.get(n, "") for n in d["photos"]).strip()[:20000]
    events = app.get("healthEvents") or []
    for d in docs:
        ev, mode = _find_duplicate(d, photos, events)
        d["dup"] = {"eventId": ev["id"], "mode": mode, "title": ev.get("title") or ""} if ev else None
        if ev and mode == "same":
            d["skip"] = True
        elif ev and mode == "merge":
            probe = json.loads(json.dumps(ev))
            if _merge_into(probe, d) == 0:
                d["skip"] = True
                d["dup"]["mode"] = "same"
    hint = parsed.get("episode") if isinstance(parsed.get("episode"), dict) else {}
    draft = {"id": uuid.uuid4().hex[:8], "chat_id": chat_id, "created": time.time(), "status": "pending",
             "note": _s(note, 500), "transcript": transcript[:60000], "photos": photos, "docs": docs,
             "episode": {"mode": None, "suggested": _default_episode_title(docs, hint),
                         "candidates": _episode_candidates(app, docs, hint)},
             "model_missing": [_s(m, 200) for m in (parsed.get("missing") or []) if _s(m)][:4],
             "message_id": None}
    for d in docs:
        if d.get("dup") and d["dup"]["mode"] == "merge":
            ev = next((e for e in events if e["id"] == d["dup"]["eventId"]), None)
            if ev and ev.get("conditionId") and not any(c["id"] == ev["conditionId"] for c in draft["episode"]["candidates"]):
                cond = next((c for c in app.get("healthConditions") or [] if c["id"] == ev["conditionId"]), None)
                if cond:
                    draft["episode"]["candidates"].insert(0, {"id": cond["id"], "name": cond.get("name", ""),
                                                              "since": cond.get("diagnosedDate", ""), "score": 9})
    draft["closures"] = _plan_order_closures(docs, events)
    draft["questions"] = _build_questions(draft, parsed)
    return draft


# ── Ответы и правки ─────────────────────────────────────────────────────

_EDITABLE = {"date", "time", "doctor", "specialty", "clinic", "title", "diagnosis", "conclusion", "recommendations",
             "notes", "complaints", "studyKind"}


def _set_field(doc: dict, field: str, value: str) -> bool:
    field = (field or "").strip()
    if field == "date":
        v = _date(value)
        if not v and value.strip().lower() == "сегодня":
            v = today_iso()
        if not v:
            return False
        doc["date"] = v
    elif field == "time":
        v = _time(value)
        if not v:
            return False
        doc["time"] = v
    elif field in _EDITABLE:
        doc[field] = _s(value, 2000)
    else:
        m = re.match(r"^(medications|orders|metrics)\.(\d+)\.(\w+)$", field)
        if not m:
            return False
        items = doc.get(m.group(1)) or []
        i = int(m.group(2)) - 1
        if not (0 <= i < len(items)) or m.group(3) in ("id",):
            return False
        items[i][m.group(3)] = _s(value, 400)
    doc["uncertain"] = [u for u in doc.get("uncertain") or [] if (u.get("field") or "") != field]
    return True


def _apply_answer(d: dict, q: dict, answer: str, option: int = None):
    q["answer"] = answer
    if q["kind"] == "episode":
        acts = q.get("actions") or []
        if option is not None and 0 <= option < len(acts):
            d["episode"].update({k: v for k, v in acts[option].items()})
        elif answer.strip().lower() in ("нет", "без эпизода", "не надо", "не нужно"):
            d["episode"].update(mode="none")
        else:
            d["episode"].update(mode="new", title=_s(answer, 80))
        return
    doc = d["docs"][q["doc"]] if 0 <= q.get("doc", -1) < len(d["docs"]) else None
    if doc is None:
        d.setdefault("answers_note", []).append(f"{q['text']} — {answer}")
        return
    if not (q.get("field") and _set_field(doc, q["field"], answer)):
        doc["notes"] = (doc.get("notes", "") + f"\nУточнение: {q['text']} — {answer}").strip()
        doc["uncertain"] = [u for u in doc.get("uncertain") or [] if (u.get("field") or "") != q.get("field")]
    if q.get("field") == "date":
        d["closures"] = _plan_order_closures(d["docs"], (_deps["load"]() or {}).get("healthEvents") or [])


def answer(draft_id: str, inp: dict) -> dict:
    with _lock:
        d = get_draft(draft_id)
        if not d or d.get("status") != "pending":
            return {"error": "Разбор не найден или уже записан/отменён."}
        changed = []
        qid = _s(inp.get("question_id"))
        if qid and inp.get("answer") not in (None, ""):
            q = next((x for x in d["questions"] if x["id"] == qid), None)
            if not q:
                return {"error": "Нет такого вопроса в разборе."}
            _apply_answer(d, q, _s(inp["answer"], 300))
            changed.append(qid)
        epq = next((x for x in d["questions"] if x["kind"] == "episode"), None)
        if inp.get("episode_title"):
            d["episode"].update(mode="new", title=_s(inp["episode_title"], 80), id="")
            if epq:
                epq["answer"] = epq["answer"] or inp["episode_title"]
            changed.append("episode")
        if inp.get("episode_id"):
            cond = next((c for c in (_deps["load"]() or {}).get("healthConditions") or [] if c["id"] == inp["episode_id"]), None)
            if not cond:
                return {"error": "Эпизод не найден."}
            d["episode"].update(mode="link", id=cond["id"], title=cond.get("name", ""))
            if epq:
                epq["answer"] = epq["answer"] or cond.get("name", "")
            changed.append("episode")
        if inp.get("no_episode"):
            d["episode"].update(mode="none")
            if epq:
                epq["answer"] = epq["answer"] or "Без эпизода"
            changed.append("episode")
        for e in inp.get("edits") or []:
            try:
                di = int(e.get("doc")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= di < len(d["docs"]) and _set_field(d["docs"][di], _s(e.get("field"), 60), _s(e.get("value"), 400)):
                changed.append(f"doc{di + 1}.{e.get('field')}")
        if not changed:
            return {"error": "Нечего применить: укажи question_id и answer, episode_* или edits."}
        put_draft(d)
    show(d)
    return {"ok": True, "applied": changed, "next": "вопрос" if _current_question(d) else "сводка с кнопкой «Записать»",
            "note": "Карточка уже в чате — ответь пользователю одной короткой фразой."}


# ── Карточки в Telegram ─────────────────────────────────────────────────

_TYPE_RU = {"visit": "приём врача", "study": "исследование", "labs": "анализы", "memo": "памятка", "procedure": "процедура",
            "note": "заметка"}
_DEST = {"visit": "История", "study": "История", "labs": "История", "memo": "Памятки", "procedure": "История", "note": "История"}


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _doc_line(i: int, d: dict, closures: list) -> str:
    when = " · ".join(x for x in (_short_date(d["date"]), d["time"]) if x)
    head = f"{i}. <b>{_e(d['title'])}</b>" + (f" · {when}" if when else "") + f" → {_DEST[d['kind']]} ({_TYPE_RU[d['kind']]})"
    if d.get("dup"):
        if d["dup"]["mode"] == "same":
            return head + "\n   ♻️ уже внесено — пропущу"
        head += "\n   ♻️ такая запись уже есть — дополню недостающим"
    bits = []
    if d["doctor"] or d["specialty"]:
        bits.append(_e(" ".join(x for x in (d["specialty"], d["doctor"]) if x)))
    if d["diagnosis"]:
        bits.append("диагноз: " + _e(d["diagnosis"][:120]) + (f" ({', '.join(d['icd'])})" if d["icd"] and not any(c in d["diagnosis"] for c in d["icd"]) else ""))
    if d["metrics"]:
        out = sum(1 for m in d["metrics"] if m["flag"])
        bits.append(f"показателей: {len(d['metrics'])}" + (f", вне нормы: {out}" if out else ", все в норме" if any(m["ref"] for m in d["metrics"]) else ""))
    if d["conclusion"]:
        bits.append("заключение: " + _e(d["conclusion"][:160]))
    orders = [o for o in d["orders"] if o["kind"] not in ("visit",)]
    if orders:
        bits.append(f"назначено: {len(orders)} (" + _e(", ".join(o["title"][:40] for o in orders[:3])) + (", …" if len(orders) > 3 else "") + ")")
    visits = [o for o in d["orders"] if o["kind"] == "visit"]
    for o in visits:
        bits.append("явка: " + _e(o["title"][:60]) + (f" {_short_date(o['dueDate'])}" if o["dueDate"] else ""))
    for m in d["medications"][:4]:
        line = " ".join(x for x in (m["name"], m["dosage"], m["route"]) if x)
        sch = " → ".join(" ".join(x for x in (st["dosage"], st["schedule"], st["duration"]) if x) for st in m["stages"]) \
            if m["stages"] else " ".join(x for x in (m["schedule"], m["duration"], m["quantity"]) if x)
        bits.append("💊 " + _e(line) + (": " + _e(sch[:160]) if sch else ""))
    if d["memo"]:
        mm = d["memo"]
        if mm["kind"] == "diet":
            bits.append(f"можно: {len(mm['allowed'])}, исключить: {len(mm['excluded'])}, принципов: {len(mm['principles'])}")
        else:
            bits.append(f"шагов подготовки: {len(mm['steps'])}" + (f" ({_e(mm['for'])})" if mm["for"] else ""))
    for c in closures:
        bits.append("✔️ закроет назначение: " + _e(c["title"][:60]))
    return head + "".join("\n   " + b for b in bits)


def summary_text(d: dict, app: dict = None) -> str:
    app = app if app is not None else (_deps["load"]() or {})
    docs = d["docs"]
    n = len(d["photos"])
    lines = [f"🩺 <b>Разбор {n} фото: {len(docs)} {'документ' if len(docs) == 1 else 'документа' if len(docs) < 5 else 'документов'}</b>"]
    for i, doc in enumerate(docs, 1):
        lines.append(_doc_line(i, doc, [c for c in d.get("closures") or [] if c["doc"] == i - 1 and not doc.get("skip")]))
    ep = d["episode"]
    if any(not x.get("skip") for x in docs):
        if ep.get("mode") == "link":
            lines.append(f"📁 Эпизод: привяжу к «{_e(ep.get('title'))}»")
        elif ep.get("mode") == "new":
            lines.append(f"📁 Эпизод: создам «{_e(ep.get('title'))}» и свяжу документы")
        elif ep.get("mode") == "none":
            lines.append("📁 Без эпизода")
    unc = [(i, u) for i, doc in enumerate(docs, 1) if not doc.get("skip") for u in doc.get("uncertain") or []]
    if unc:
        lines.append("⚠️ <b>Проверьте</b> (не додумывал):" + "".join(
            f"\n   • док. {i}: {_e(u['reason'] + ': ') if u.get('reason') else ''}{_e(u['text'][:120])}" for i, u in unc[:6]))
    miss = _missing_lines(d, app)
    if miss:
        lines.append("ℹ️ " + "\n   ".join(_e(m) for m in miss))
    if all(x.get("skip") for x in docs):
        lines.append("Всё это уже есть в «Здоровье» — записывать нечего.")
    else:
        lines.append("<i>Поправить — напишите, что изменить. Записать — кнопкой.</i>")
    text = "\n\n".join(lines)
    return text if len(text) < 3900 else text[:3880] + "…"


def _question_text(d: dict, q: dict, chosen: str = None) -> str:
    qs = d["questions"]
    pos = next(i for i, x in enumerate(qs, 1) if x["id"] == q["id"])
    found = ", ".join(_e(x["title"]) for x in d["docs"])
    tail = f"→ <b>{_e(chosen)}</b>" if chosen is not None else "<i>Ответьте кнопкой или напишите свой вариант.</i>"
    return f"🩺 <b>Разбор документов</b> · вопрос {pos} из {len(qs)}\n<i>Нашёл: {found}</i>\n\n{_e(q['text'])}\n\n{tail}"


def card(d: dict):
    """→ (text, reply_markup) — текущий вопрос или сводка."""
    q = _current_question(d)
    if q:
        rows, row = [], []
        for i, o in enumerate(q.get("options") or []):
            row.append({"text": o[:40], "callback_data": f"hd:{d['id']}:a:{q['id']}:{i}"})
            if len(row) == (1 if q["kind"] == "episode" else 2):
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "Пропустить", "callback_data": f"hd:{d['id']}:s:{q['id']}"},
                     {"text": "✖ Отмена", "callback_data": f"hd:{d['id']}:no"}])
        return _question_text(d, q), {"inline_keyboard": rows}
    if all(x.get("skip") for x in d["docs"]):
        return summary_text(d), {"inline_keyboard": [[{"text": "Понятно", "callback_data": f"hd:{d['id']}:no"}]]}
    return summary_text(d), {"inline_keyboard": [[{"text": "✅ Записать", "callback_data": f"hd:{d['id']}:ok"},
                                                  {"text": "✖ Отмена", "callback_data": f"hd:{d['id']}:no"}]]}


def show(d: dict, edit: bool = False):
    tg = _deps.get("tg")
    token = _deps["token"]()
    if not (tg and token and d.get("chat_id") is not None):
        return
    text, markup = card(d)
    if edit and d.get("message_id"):
        try:
            tg.api(token, "editMessageText", {"chat_id": d["chat_id"], "message_id": d["message_id"], "text": text,
                                              "parse_mode": "HTML", "reply_markup": markup})
            return
        except Exception:
            pass
    ids = tg.send_text(token, d["chat_id"], text, reply_markup=markup)
    with _lock:
        cur = get_draft(d["id"]) or d
        cur["message_id"] = ids[-1] if ids else None
        d["message_id"] = cur["message_id"]
        put_draft(cur)


# ── Запись ──────────────────────────────────────────────────────────────

def _event_from_doc(d: dict, draft: dict, condition_id: str) -> dict:
    now = int(time.time() * 1000)
    photos = [{"filename": p["file"], "category": "выписка" if d["kind"] != "labs" else "анализы"}
              for p in draft["photos"] if p["n"] in d["photos"] and p["file"]]
    ev = {"id": str(uuid.uuid4()), "type": d["kind"], "date": d["date"] or today_iso(), "endDate": "", "time": d["time"],
          "title": d["title"], "conditionId": condition_id, "doctor": d["doctor"], "specialty": d["specialty"],
          "clinic": d["clinic"], "symptoms": d["complaints"], "anamnesis": d["anamnesis"], "diagnosis": d["diagnosis"],
          "icd": d["icd"], "studyKind": d["studyKind"], "serviceCode": d["serviceCode"], "metrics": d["metrics"],
          "conclusion": d["conclusion"], "recommendations": d["recommendations"], "orders": d["orders"],
          "medications": [dict(m, startDate=m.get("startDate") or d["date"] or today_iso()) for m in d["medications"]],
          "outcome": "", "uncertain": d["uncertain"], "photos": photos, "files": [], "tags": [], "notes": d.get("notes", ""),
          "notify": False, "source": {"via": "telegram", "draftId": draft["id"], "importedAt": now,
                                      "hashes": [p["sha"] for p in draft["photos"] if p["n"] in d["photos"]],
                                      "text": d.get("sourceText", "")},
          "createdAt": now, "updatedAt": now}
    if d["memo"]:
        ev["memo"] = d["memo"]
    if d["specialty"] and d["kind"] == "visit":
        ev["tags"] = [d["specialty"].lower()[:30]]
    return ev


def commit(draft_id: str) -> dict:
    """Пишет разбор в данные. Повторный вызов ничего не дублирует."""
    with _lock:
        d = get_draft(draft_id)
        if not d:
            return {"error": "Разбор не найден (устарел?) — пришлите фото ещё раз."}
        if d.get("status") == "committed":
            return {"ok": True, "already": True, "result": d.get("result") or {}}
        if d.get("status") != "pending":
            return {"error": "Разбор отменён."}
        q = _current_question(d)
        if q and q["kind"] == "episode":
            return {"error": "Сначала ответьте на вопрос про эпизод.", "question": q["text"]}
        lock = _deps.get("lock") or threading.RLock()
        with lock:
            app = _deps["load"]() or {}
            events = app.setdefault("healthEvents", [])
            conds = app.setdefault("healthConditions", [])
            now = int(time.time() * 1000)
            result = {"created": [], "merged": [], "skipped": [], "closed": [], "conditionId": "", "conditionCreated": False}
            ep = d["episode"]
            todo = [x for x in d["docs"] if not x.get("skip")]
            cid = ""
            if todo and ep.get("mode") == "link" and any(c.get("id") == ep.get("id") for c in conds):
                cid = ep["id"]
            elif todo and ep.get("mode") == "new":
                dates = sorted(x["date"] for x in todo if x["date"])
                symptoms = "; ".join(dict.fromkeys(x["complaints"] for x in todo if x["complaints"]))
                cond = {"id": str(uuid.uuid4()), "name": ep.get("title") or d["episode"]["suggested"], "kind": "episode",
                        "isChronic": False, "status": "active", "diagnosedDate": dates[0] if dates else today_iso(),
                        "resolvedDate": "", "symptoms": symptoms[:1000], "notes": "", "tags": [],
                        "createdAt": now, "updatedAt": now}
                conds.append(cond)
                cid = cond["id"]
                result["conditionCreated"] = True
            result["conditionId"] = cid
            ids_by_doc, created_ids = {}, set()
            for i, doc in enumerate(d["docs"]):
                if doc.get("skip"):
                    result["skipped"].append(doc["title"])
                    if doc.get("dup"):
                        ids_by_doc[i] = doc["dup"]["eventId"]
                    continue
                ev, mode = _find_duplicate(doc, d["photos"], events)
                if ev and mode == "same":
                    result["skipped"].append(doc["title"])
                    ids_by_doc[i] = ev["id"]
                    continue
                if ev:
                    _merge_into(ev, doc)
                    src = ev.setdefault("source", {})
                    src["hashes"] = sorted(set(src.get("hashes") or []) | {p["sha"] for p in d["photos"] if p["n"] in doc["photos"]})
                    have = {p.get("filename") for p in ev.get("photos") or []}
                    ev["photos"] = list(ev.get("photos") or []) + [
                        {"filename": p["file"], "category": "выписка"} for p in d["photos"]
                        if p["n"] in doc["photos"] and p["file"] and p["file"] not in have]
                    if cid and not ev.get("conditionId"):
                        ev["conditionId"] = cid
                    ev["updatedAt"] = now
                    result["merged"].append(doc["title"])
                    ids_by_doc[i] = ev["id"]
                    continue
                ev = _event_from_doc(doc, d, cid)
                events.append(ev)
                result["created"].append(doc["title"])
                ids_by_doc[i] = ev["id"]
                created_ids.add(ev["id"])
            for c in _plan_order_closures(d["docs"], [e for e in events if e["id"] not in created_ids]):
                owner = c.get("eventId") or ids_by_doc.get(c.get("docRef"))
                target = next((e for e in events if e["id"] == owner), None)
                if not target:
                    continue
                for o in target.get("orders") or []:
                    if o["id"] == c["orderId"] and o.get("status") == "assigned":
                        o.update(status="done", resultEventId=ids_by_doc.get(c["doc"], ""), doneDate=d["docs"][c["doc"]]["date"])
                        target["updatedAt"] = now
                        result["closed"].append(o["title"])
            for i, doc in enumerate(d["docs"]):
                if doc.get("skip") or i not in ids_by_doc:
                    continue
                ev = next((e for e in events if e["id"] == ids_by_doc[i]), None)
                for o in (ev or {}).get("orders") or []:
                    for other in events:
                        if other["id"] == ev["id"] or o.get("status") != "assigned":
                            continue
                        if other.get("date") and ev.get("date") and other["date"] >= ev["date"] and \
                                _order_matches(o, {"kind": other.get("type"), "serviceCode": other.get("serviceCode", ""),
                                                   "studyKind": other.get("studyKind", ""), "title": other.get("title", ""),
                                                   "specialty": other.get("specialty", "")}):
                            o.update(status="done", resultEventId=other["id"], doneDate=other["date"])
                            result["closed"].append(o["title"])
            _deps["save"](app)
            keep = {p.get("filename") for e in events for p in e.get("photos") or []}
        _drop_photos(d, keep)
        d["status"] = "committed"
        d["result"] = result
        d["reminders"] = reminder_proposals(d, app)
        d["transcript"] = ""
        put_draft(d)
    print(f"  health: draft {d['id']} committed: +{len(result['created'])} ~{len(result['merged'])} ={len(result['skipped'])}", flush=True)
    return {"ok": True, "result": result}


def cancel(draft_id: str) -> dict:
    with _lock:
        d = get_draft(draft_id)
        if not d or d.get("status") != "pending":
            return {"error": "Разбор не найден или уже закрыт."}
        _drop_photos(d)
        d["status"] = "cancelled"
        d["transcript"] = ""
        for x in d["docs"]:
            x["sourceText"] = ""
        put_draft(d)
    return {"ok": True}


def committed_text(d: dict) -> str:
    r = d.get("result") or {}
    parts = []
    if r.get("created"):
        parts.append("добавлено: " + ", ".join(_e(x) for x in r["created"]))
    if r.get("merged"):
        parts.append("дополнено: " + ", ".join(_e(x) for x in r["merged"]))
    if r.get("skipped"):
        parts.append("уже было: " + ", ".join(_e(x) for x in r["skipped"]))
    if r.get("closed"):
        parts.append("выполнено: " + ", ".join(_e(x) for x in r["closed"]))
    ep = d["episode"]
    if r.get("conditionId"):
        parts.append(("создан эпизод «" if r.get("conditionCreated") else "эпизод «") + _e(ep.get("title")) + "»")
    site = _deps.get("site", lambda: "")()
    link = f'\n\n<a href="{html.escape(site.rstrip("/") + "/health")}">Открыть «Здоровье»</a>' if site else ""
    return "✅ <b>Записал в «Здоровье»</b>\n" + "\n".join("• " + p for p in parts) + link


# ── Напоминания ──────────────────────────────────────────────────────────

def _days(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(дн|день|дня|сут|недел|нед|месяц|мес)", (text or "").lower())
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    return int(round(n * (1 if unit.startswith(("дн", "ден", "сут")) else 7 if unit.startswith("нед") else 30)))


def reminder_proposals(d: dict, app: dict) -> list:
    now = now_msk()
    out = []
    ids = set()
    for e in app.get("healthEvents") or []:
        if (e.get("source") or {}).get("draftId") == d["id"]:
            ids.add(e["id"])
    events = [e for e in app.get("healthEvents") or [] if e["id"] in ids]

    def add(when: datetime, title: str, text: str):
        if when > now + timedelta(minutes=5):
            out.append({"when": when.strftime("%Y-%m-%d %H:%M"), "title": title[:80], "text": text[:500]})

    for e in events:
        for o in e.get("orders") or []:
            if o.get("kind") == "visit" and o.get("dueDate") and o.get("status") == "assigned":
                day = datetime.strptime(o["dueDate"], "%Y-%m-%d").replace(tzinfo=MSK)
                at = f" в {o['dueTime']}" if o.get("dueTime") else ""
                where = f" ({e['clinic']})" if e.get("clinic") else ""
                add(day - timedelta(days=1) + timedelta(hours=19), f"Явка {_short_date(o['dueDate'])}",
                    f"Завтра{at}: {o['title']}{where}.")
        for m in e.get("medications") or []:
            start = m.get("startDate") or e.get("date")
            if not start:
                continue
            t0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=MSK) + timedelta(hours=10)
            stages = m.get("stages") or []
            acc = 0
            for k, st in enumerate(stages[:-1]):
                n = _days(st.get("duration"))
                if n is None:
                    break
                acc += n
                nxt = stages[k + 1]
                add(t0 + timedelta(days=acc), f"{m['name']}: этап {k + 2}",
                    f"{m['name']}: с сегодня новая схема — " + " ".join(x for x in (nxt.get("dosage"), nxt.get("schedule"), nxt.get("duration")) if x) + ".")
            total = sum(_days(st.get("duration")) or 0 for st in stages) if stages else (_days(m.get("duration")) or 0)
            if total:
                add(t0 + timedelta(days=total), f"{m['name']}: конец курса", f"Курс «{m['name']}» по назначению закончился. Обсудите с врачом, что дальше.")
    pending = [o for e in events for o in e.get("orders") or [] if o.get("status") == "assigned" and o.get("kind") in ("lab", "study")]
    if pending:
        tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        add(tomorrow, "Сдать назначенные обследования",
            "Назначено и ещё не сделано: " + ", ".join(dict.fromkeys((o.get("group") or o["title"]) for o in pending[:8])) + ".")
    return out[:6]


def reminders_card(d: dict):
    rem = d.get("reminders") or []
    if not rem:
        return None, None
    lines = ["🔔 <b>Поставить напоминания?</b> Без вашего выбора ничего не ставлю."]
    rows = []
    for i, r in enumerate(rem):
        dt = datetime.strptime(r["when"], "%Y-%m-%d %H:%M")
        lines.append(f"{i + 1}. {dt.strftime('%d.%m %H:%M')} — {_e(r['text'])}")
        rows.append([{"text": f"🔔 {i + 1}. {r['title'][:34]}", "callback_data": f"hd:{d['id']}:r:{i}"}])
    rows.append([{"text": "Все", "callback_data": f"hd:{d['id']}:r:all"}, {"text": "Не нужно", "callback_data": f"hd:{d['id']}:r:none"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def set_reminders(d: dict, which) -> list:
    plan = _deps.get("plan")
    if not plan:
        return []
    state = (_deps["load"]() or {}).get("telegramSkills")
    if not plan.skill_on(state):
        return [{"error": "Навык «Планирование и напоминания» выключен."}]
    rem = d.get("reminders") or []
    done = set(d.get("reminders_set") or [])
    picked = range(len(rem)) if which == "all" else [which]
    out = []
    for i in picked:
        if not (0 <= i < len(rem)) or i in done:
            continue
        r = rem[i]
        res = plan.execute("plan_create", {"kind": "remind", "title": "🩺 " + r["title"], "text": "🩺 " + r["text"], "when": r["when"]},
                           state, d["chat_id"], False, "health")
        if res.get("ok"):
            done.add(i)
        out.append(res)
    d["reminders_set"] = sorted(done)
    put_draft(d)
    return out


# ── Нажатия кнопок ──────────────────────────────────────────────────────

def handle_callback(token: str, cq: dict):
    tg = _deps["tg"]
    parts = (cq.get("data") or "").split(":")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")

    def toast(text=""):
        try:
            tg.api(token, "answerCallbackQuery", {"callback_query_id": cq.get("id"), **({"text": text} if text else {})})
        except Exception:
            pass

    def edit(text, markup=None):
        try:
            tg.api(token, "editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": text,
                                              "parse_mode": "HTML", "reply_markup": markup or {"inline_keyboard": []}})
        except Exception:
            pass

    if len(parts) < 3:
        return toast()
    d = get_draft(parts[1])
    if not d or str(d.get("chat_id")) != str(chat_id):
        return toast("Разбор устарел")
    if not skill_on((_deps["load"]() or {}).get("telegramSkills")):
        return toast("Навык выключен в настройках")
    act = parts[2]
    if act in ("a", "s") and len(parts) >= 4:
        with _lock:
            d = get_draft(parts[1])
            q = next((x for x in d["questions"] if x["id"] == parts[3]), None)
            if d.get("status") != "pending" or not q or q.get("answer") is not None:
                return toast("Уже отвечено")
            if act == "s":
                q["answer"] = ""
                if q["kind"] == "episode":
                    d["episode"]["mode"] = "none"
                chosen = "пропущено"
            else:
                try:
                    oi = int(parts[4])
                except (IndexError, ValueError):
                    return toast()
                opts = q.get("options") or []
                if not (0 <= oi < len(opts)):
                    return toast()
                chosen = opts[oi]
                _apply_answer(d, q, chosen, oi)
            put_draft(d)
        toast()
        edit(_question_text(d, q, chosen))
        d["message_id"] = None
        show(d)
        return
    if act == "ok":
        res = commit(d["id"])
        if res.get("error"):
            return toast(res["error"][:180])
        toast("Записано" if not res.get("already") else "Уже записано")
        d = get_draft(d["id"])
        edit(committed_text(d))
        text, markup = reminders_card(d)
        if text and not res.get("already"):
            tg.send_text(token, chat_id, text, reply_markup=markup)
        return
    if act == "no":
        cancel(d["id"])
        toast()
        edit(("Ок, не записываю." if not all(x.get("skip") for x in d["docs"]) else "Ок.") + " Разбор закрыт.")
        return
    if act == "r" and len(parts) >= 4:
        if d.get("status") != "committed":
            return toast()
        if parts[3] == "none":
            toast()
            edit("Напоминания не ставлю.")
            return
        which = "all" if parts[3] == "all" else int(parts[3]) if parts[3].isdigit() else -1
        res = set_reminders(d, which)
        errs = [r["error"] for r in res if r.get("error")]
        if errs:
            return toast(errs[0][:180])
        d = get_draft(d["id"])
        rem = d.get("reminders") or []
        left = [i for i in range(len(rem)) if i not in set(d.get("reminders_set") or [])]
        toast("Поставил")
        done_lines = "\n".join(f"✅ {datetime.strptime(rem[i]['when'], '%Y-%m-%d %H:%M').strftime('%d.%m %H:%M')} — {_e(rem[i]['text'])}"
                               for i in d.get("reminders_set") or [])
        if left:
            _, markup = reminders_card(d)
            markup["inline_keyboard"] = [r for r in markup["inline_keyboard"]
                                         if not r[0]["callback_data"].split(":")[-1].isdigit()
                                         or int(r[0]["callback_data"].split(":")[-1]) in left]
            edit("🔔 <b>Напоминания</b>\n" + done_lines, markup)
        else:
            edit("🔔 <b>Поставил напоминания</b> (видны в Настройки → Ассистент → «Плановые задачи»):\n" + done_lines)
        return
    toast()


# ── Исполнитель инструментов ─────────────────────────────────────────────

def execute(name: str, inp: dict, app: dict, ctx=None, unattended: bool = False) -> dict:
    state = (app or {}).get("telegramSkills")
    if not skill_on(state):
        return {"error": "Навык «Медицинские документы → Здоровье» выключен: Настройки → Ассистент → Навыки ассистента."}
    if unattended:
        return {"error": "Разбор медицинских документов — только в разговоре с пользователем."}
    inp = inp or {}
    if name == "health_import":
        if ctx is None or getattr(ctx, "chat_id", None) is None:
            return {"error": "Разбор фото документов работает в Telegram-боте: пришлите туда фото."}
        images = list(getattr(ctx, "images", None) or [])
        if not images and _deps.get("recent_photos"):
            images = _deps["recent_photos"](ctx.chat_id)
        if not images:
            return {"error": "В сообщении нет фото документов. Попросите прислать фото (можно альбомом)."}
        tg, token = _deps.get("tg"), _deps["token"]()
        progress = None
        if tg and token:
            try:
                progress = tg.send_text(token, ctx.chat_id, f"🩺 Распознаю документы ({len(images[:MAX_PHOTOS])} фото)…", silent=True)
            except Exception:
                progress = None
        try:
            d = analyze(app, images, _s(inp.get("note"), 500), ctx.chat_id)
        except Exception as e:
            print(f"  health: analyze failed: {type(e).__name__}", flush=True)
            msg = str(e) if isinstance(e, ValueError) else "не удалось распознать"
            if progress and tg:
                try:
                    tg.api(token, "deleteMessage", {"chat_id": ctx.chat_id, "message_id": progress[-1]})
                except Exception:
                    pass
            return {"error": f"Разбор не получился: {msg}"[:300]}
        if progress:
            d["message_id"] = progress[-1]
        put_draft(d)
        print(f"  health: draft {d['id']} docs={len(d['docs'])} photos={len(d['photos'])} questions={len(d['questions'])}", flush=True)
        show(d, edit=bool(progress))
        ctx.sent.append("разбор медицинских документов с кнопками (вопросы/сводка)")
        return {"ok": True, "draft_id": d["id"],
                "documents": [{"n": i + 1, "title": x["title"], "kind": x["kind"], "date": x["date"],
                               "duplicate": (x.get("dup") or {}).get("mode") or ""} for i, x in enumerate(d["docs"])],
                "questions": len(d["questions"]),
                "note": "Вопросы и сводка с кнопкой «Записать» уже отправлены в чат. Ответь одной короткой фразой, не пересказывай документы."}
    if name == "health_import_answer":
        return answer(inp.get("draft_id"), inp)
    if name == "health_import_commit":
        if not inp.get("confirmed"):
            return {"needs_confirmation": True, "message": "Нужно явное согласие пользователя записать разбор в «Здоровье»."}
        res = commit(inp.get("draft_id"))
        if res.get("ok") and not res.get("already"):
            d = get_draft(inp.get("draft_id"))
            tg, token = _deps.get("tg"), _deps["token"]()
            if tg and token and d:
                if d.get("message_id"):
                    try:
                        tg.api(token, "editMessageReplyMarkup", {"chat_id": d["chat_id"], "message_id": d["message_id"],
                                                                 "reply_markup": {"inline_keyboard": []}})
                    except Exception:
                        pass
                tg.send_text(token, d["chat_id"], committed_text(d))
                text, markup = reminders_card(d)
                if text:
                    tg.send_text(token, d["chat_id"], text, reply_markup=markup)
            return {"ok": True, "result": res["result"], "note": "Итог и предложение напоминаний уже в чате — ответь коротко."}
        return res
    if name == "health_import_cancel":
        return cancel(inp.get("draft_id"))
    return {"error": f"неизвестный инструмент {name}"}
