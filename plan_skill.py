"""
plan_skill.py — навык «Планирование и напоминания».

Ассистент сам ставит плановые задачи двух видов:
  • remind — напоминание: в назначенное время в бот уходит текст;
  • task   — поручение: в назначенное время ассистент выполняет инструкцию сам
             (без пользователя, поэтому всё, что требует «да», он не делает) и
             по желанию отчитывается в чат. Отчитываться или нет — спрашивается
             у пользователя при постановке (report обязателен для task).

Задачи лежат на диске (STORE_NAME) и видны в Настройки → Ассистент → «Плановые задачи»:
название, дата и время, статус, результат. Запуск — минутный цикл jarvis.py (claim_due).
"""

import calendar
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_ID = "plan"
STORE_NAME = "jarvis_plan_tasks.json"
KINDS = ("remind", "task")
REPEATS = ("none", "daily", "weekly", "monthly")
REPEAT_RU = {"none": "один раз", "daily": "каждый день", "weekly": "каждую неделю", "monthly": "каждый месяц"}
MAX_TASKS = 300            # всего записей (старые завершённые вытесняются)
MAX_RESULT = 3000          # символов результата в списке
OVERDUE_GRACE_MIN = 360    # пропущенную (сервер спал) разовую задачу ещё выполняем в пределах 6 часов
STALE_RUNNING_S = 1800     # «выполняется» дольше 30 минут — считаем оборвавшейся

MSK = timezone(timedelta(hours=3))
_lock = threading.RLock()

SKILLS = [
    {"id": SKILL_ID, "title": "Планирование и напоминания", "risk": "write", "default": True,
     "desc": (
         "Ассистент сам ставит плановые задачи: «напомни завтра в 9 про звонок» — в нужное время бот пришлёт напоминание; "
         "«в пятницу в 18:00 собери сводку по учёбе» — в срок он выполнит поручение сам. Можно один раз или повторять "
         "(каждый день, неделю, месяц). Для поручения он сначала спросит, нужно ли отчитаться в чат, и сделает как скажете. "
         "Без вас он не выполняет то, что требует вашего «да» (отправка от вашего имени, удаление, финансы) — вместо этого "
         "пишет, что нужно сделать. Все задачи со статусом и результатом — в Настройки → Ассистент → «Плановые задачи»; "
         "их можно отменить, запустить сразу или удалить. Время московское."
     ),
     "tools": ["plan_create", "plan_list", "plan_update", "plan_cancel"]},
]

_S = {"type": "string"}
TOOLS = [
    {"name": "plan_create",
     "description": (
         "Поставить плановую задачу. kind=remind — напоминание (в срок бот пришлёт text); kind=task — поручение "
         "(в срок ты сам выполнишь instruction с инструментами, пользователя рядом не будет). Время — ИЛИ in_minutes "
         "(«через 20 минут»), ИЛИ when «YYYY-MM-DD HH:MM» по Москве (вычисли от СЕГОДНЯ и текущего времени). "
         "repeat: none (по умолчанию), daily, weekly, monthly. Для kind=task параметр report ОБЯЗАТЕЛЕН: сначала спроси "
         "пользователя «Отчитаться о выполнении в чат?» и передай true/false по ответу — сам не решай. "
         "Для remind report не нужен. Если ответ содержит needs_answer — задай вопрос пользователю и вызови снова."
     ),
     "input_schema": {"type": "object", "properties": {
         "kind": {**_S, "enum": list(KINDS), "description": "remind — напомнить, task — выполнить поручение."},
         "title": {**_S, "description": "Короткое название для списка."},
         "text": {**_S, "description": "remind: текст напоминания; task: подробная инструкция, что сделать (самодостаточная — без контекста чата)."},
         "when": {**_S, "description": "YYYY-MM-DD HH:MM по Москве."},
         "in_minutes": {"type": "number", "description": "Через сколько минут (вместо when)."},
         "repeat": {**_S, "enum": list(REPEATS)},
         "report": {"type": "boolean", "description": "task: отчитаться в чат о результате. Только по ответу пользователя."},
     }, "required": ["kind", "text"]}},
    {"name": "plan_list",
     "description": "Список плановых задач: id, название, вид, время, повтор, статус, результат. status — фильтр (scheduled, done, failed, cancelled…).",
     "input_schema": {"type": "object", "properties": {"status": _S}, "required": []}},
    {"name": "plan_update",
     "description": "Изменить ожидающую задачу: новое время (when / in_minutes), title, text, repeat, report. Указывай id или название.",
     "input_schema": {"type": "object", "properties": {
         "id": _S, "title_match": {**_S, "description": "Часть названия, если id неизвестен."},
         "title": _S, "text": _S, "when": _S, "in_minutes": {"type": "number"},
         "repeat": {**_S, "enum": list(REPEATS)}, "report": {"type": "boolean"}}, "required": []}},
    {"name": "plan_cancel",
     "description": "Отменить плановую задачу (она останется в списке со статусом «отменена»). Указывай id или часть названия.",
     "input_schema": {"type": "object", "properties": {"id": _S, "title_match": _S}, "required": []}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
assert not _missing, _missing


class PlanError(Exception):
    pass


# ── Включённость и промпт ────────────────────────────────────────────────

def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


def now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(MSK)


def prompt_section(state=None) -> str:
    if not skill_on(state):
        return ""
    return (
        "12. ПЛАНИРОВАНИЕ (plan_create / plan_list / plan_update / plan_cancel). Сейчас " + now_msk().strftime("%Y-%m-%d %H:%M")
        + " МСК. «Напомни …» по умолчанию → plan_create(kind=remind): в срок бот пришлёт текст в чат; отчёт не нужен. "
        "Apple Напоминание (reminders_create) — только если пользователь прямо сказал «в Напоминания» / «в Apple» / «на айфон». "
        "«Сделай/проверь/собери … завтра/в пятницу/каждый день» → plan_create(kind=task, text=самодостаточная инструкция): "
        "в срок ты выполнишь её сам, БЕЗ пользователя — подтверждений получить будет нельзя, поэтому действия, требующие «да», "
        "ты тогда не выполнишь, а лишь опишешь. ОБЯЗАТЕЛЬНО перед постановкой поручения спроси: «Отчитаться о выполнении в чат?» — "
        "и передай report по ответу (напоминанию вопрос не нужен). Время называй пользователю по-человечески и подтверди, "
        "что поставил (что, когда, повтор, отчёт). Список и статусы — plan_list; отмена — plan_cancel. "
        "Все плановые задачи видны в Настройки → Ассистент → «Плановые задачи».\n\n"
    )


def voice_payload(state) -> dict:
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": prompt_section(state)}


# ── Хранилище ────────────────────────────────────────────────────────────

def store_path() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / STORE_NAME


def _load() -> list:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(tasks: list) -> None:
    if len(tasks) > MAX_TASKS:     # вытесняем самые старые завершённые
        done = [t for t in tasks if t["status"] in ("done", "failed", "cancelled", "missed")]
        done.sort(key=lambda t: t.get("finished_at") or t.get("created_at") or "")
        drop = {t["id"] for t in done[: len(tasks) - MAX_TASKS]}
        tasks = [t for t in tasks if t["id"] not in drop]
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _stamp(dt=None) -> str:
    return (dt or now_msk()).strftime("%Y-%m-%d %H:%M:%S")


def _parse_when(when=None, in_minutes=None, allow_past=False) -> str:
    """→ 'YYYY-MM-DD HH:MM' (МСК)."""
    if in_minutes not in (None, ""):
        try:
            m = float(in_minutes)
        except (TypeError, ValueError):
            raise PlanError("in_minutes — число минут.")
        if m < 0 or m > 60 * 24 * 365:
            raise PlanError("in_minutes вне допустимого диапазона.")
        return (now_msk() + timedelta(minutes=max(m, 1))).strftime("%Y-%m-%d %H:%M")
    s = str(when or "").strip().replace("T", " ")
    if not s:
        raise PlanError("Укажите время: when (YYYY-MM-DD HH:MM) или in_minutes.")
    try:
        dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
    except ValueError:
        raise PlanError("Время в формате YYYY-MM-DD HH:MM (по Москве).")
    if not allow_past and dt < now_msk() - timedelta(minutes=1):
        raise PlanError("Это время уже прошло: " + s[:16] + ". Уточните дату.")
    return dt.strftime("%Y-%m-%d %H:%M")


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=MSK)


def _add_month(dt: datetime, anchor: int) -> datetime:
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=y, month=m, day=min(anchor, calendar.monthrange(y, m)[1]))


def next_run(run_at: str, repeat: str, after: datetime = None, anchor_day: int = None) -> str:
    """Ближайшее повторение строго позже `after` (по умолчанию — сейчас). anchor_day — «31-е» не уезжает на 28-е."""
    after = after or now_msk()
    dt = _dt(run_at)
    anchor = anchor_day or dt.day
    for _ in range(4000):
        dt = {"daily": lambda d: d + timedelta(days=1), "weekly": lambda d: d + timedelta(days=7),
              "monthly": lambda d: _add_month(d, anchor)}[repeat](dt)
        if dt > after:
            break
    return dt.strftime("%Y-%m-%d %H:%M")


def public(t: dict) -> dict:
    out = {k: v for k, v in t.items() if k != "chat_id"}
    out["repeat_ru"] = REPEAT_RU.get(t.get("repeat") or "none", "")
    return out


# ── Операции ─────────────────────────────────────────────────────────────

def create(inp: dict, chat_id=None, source="bot") -> dict:
    kind = str(inp.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise PlanError("kind: remind (напоминание) или task (поручение).")
    text = str(inp.get("text") or inp.get("instruction") or "").strip()
    if not text:
        raise PlanError("Нужен текст: что напомнить или что сделать.")
    repeat = str(inp.get("repeat") or "none").strip().lower()
    if repeat not in REPEATS:
        raise PlanError("repeat: " + ", ".join(REPEATS) + ".")
    report = inp.get("report")
    if kind == "task" and not isinstance(report, bool):
        return {"needs_answer": True,
                "message": "Спроси пользователя: «Отчитаться о выполнении в чат?» — и вызови plan_create снова с report:true или false."}
    when = _parse_when(inp.get("when"), inp.get("in_minutes"))
    title = str(inp.get("title") or "").strip() or text.replace("\n", " ")[:60]
    t = {"id": uuid.uuid4().hex[:8], "kind": kind, "title": title[:120], "text": text[:4000], "run_at": when,
         "repeat": repeat, "report": (report if kind == "task" else True), "chat_id": chat_id,
         "status": "scheduled", "created_at": _stamp(), "runs": 0, "result": "", "source": source,
         "anchor_day": int(when[8:10])}
    with _lock:
        tasks = _load()
        tasks.append(t)
        _save(tasks)
    return {"ok": True, "id": t["id"], "kind": kind, "title": t["title"], "run_at": when,
            "repeat": REPEAT_RU[repeat], **({"report": report} if kind == "task" else {})}


def _find(tasks, inp, only_active=False):
    tid = str(inp.get("id") or "").strip()
    q = str(inp.get("title_match") or "").strip().lower()
    pool = [t for t in tasks if not only_active or t["status"] in ("scheduled", "running")]
    if tid:
        hit = [t for t in pool if t["id"] == tid]
    elif q:
        hit = [t for t in pool if q in (t["title"] or "").lower()]
    else:
        raise PlanError("Укажите id или часть названия задачи.")
    if not hit:
        raise PlanError("Задача не найдена. Список — plan_list.")
    if len(hit) > 1:
        raise PlanError("Подходит несколько: " + "; ".join(f"«{t['title']}» ({t['id']}, {t['run_at']})" for t in hit[:6])
                        + ". Уточните или укажите id.")
    return hit[0]


def cancel(inp: dict) -> dict:
    with _lock:
        tasks = _load()
        t = _find(tasks, inp, only_active=True)
        if t["status"] == "running":
            raise PlanError("Задача выполняется прямо сейчас — дождитесь результата.")
        t.update(status="cancelled", finished_at=_stamp())
        _save(tasks)
    return {"ok": True, "id": t["id"], "title": t["title"], "status": "cancelled"}


def update(inp: dict) -> dict:
    with _lock:
        tasks = _load()
        t = _find(tasks, inp, only_active=True)
        if t["status"] != "scheduled":
            raise PlanError("Менять можно только ожидающую задачу.")
        if inp.get("when") or inp.get("in_minutes") not in (None, ""):
            t["run_at"] = _parse_when(inp.get("when"), inp.get("in_minutes"))
            t["anchor_day"] = int(t["run_at"][8:10])
        for src, dst in (("title", "title"), ("text", "text")):
            if str(inp.get(src) or "").strip():
                t[dst] = str(inp[src]).strip()[: 120 if dst == "title" else 4000]
        if inp.get("repeat"):
            if inp["repeat"] not in REPEATS:
                raise PlanError("repeat: " + ", ".join(REPEATS) + ".")
            t["repeat"] = inp["repeat"]
        if isinstance(inp.get("report"), bool) and t["kind"] == "task":
            t["report"] = inp["report"]
        t["updated_at"] = _stamp()
        _save(tasks)
    return {"ok": True, **{k: t[k] for k in ("id", "title", "run_at", "status")}, "repeat": REPEAT_RU[t["repeat"]]}


def list_tasks(status="") -> list:
    with _lock:
        tasks = _load()
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    running = [t for t in tasks if t["status"] == "running"]
    waiting = sorted((t for t in tasks if t["status"] == "scheduled"), key=lambda t: t["run_at"])
    finished = sorted((t for t in tasks if t["status"] not in ("running", "scheduled")),
                      key=lambda t: t.get("finished_at") or t["run_at"], reverse=True)
    return [public(t) for t in running + waiting + finished]


def delete(task_id: str) -> dict:
    with _lock:
        tasks = _load()
        t = next((x for x in tasks if x["id"] == task_id), None)
        if not t:
            raise PlanError("Задача не найдена.")
        if t["status"] == "running":
            raise PlanError("Задача выполняется прямо сейчас.")
        _save([x for x in tasks if x["id"] != task_id])
    return {"ok": True}


def run_now(task_id: str) -> dict:
    """Сдвигает ожидающую задачу на «сейчас»: выполнится на ближайшей минуте цикла."""
    with _lock:
        tasks = _load()
        t = next((x for x in tasks if x["id"] == task_id), None)
        if not t or t["status"] != "scheduled":
            raise PlanError("Запустить можно только ожидающую задачу.")
        t["run_at"] = now_msk().strftime("%Y-%m-%d %H:%M")
        _save(tasks)
    return {"ok": True, "run_at": t["run_at"]}


def clear_finished() -> dict:
    with _lock:
        tasks = _load()
        keep = [t for t in tasks if t["status"] in ("scheduled", "running")]
        _save(keep)
    return {"ok": True, "removed": len(tasks) - len(keep)}


def summary() -> dict:
    with _lock:
        tasks = _load()
    return {"active": sum(1 for t in tasks if t["status"] in ("scheduled", "running")), "total": len(tasks)}


# ── Запуск по времени ────────────────────────────────────────────────────

def claim_due(now: datetime = None) -> list:
    """Берёт задачи, чьё время пришло, помечает «выполняется» и возвращает их. Вызывает минутный цикл."""
    now = now or now_msk()
    claimed = []
    with _lock:
        tasks = _load()
        changed = False
        for t in tasks:
            if t["status"] == "running":
                started = t.get("started_at") or t["created_at"]
                if (now - datetime.strptime(started, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)).total_seconds() > STALE_RUNNING_S:
                    t.update(status="failed", finished_at=_stamp(now), result="Выполнение оборвалось (перезапуск сервера).")
                    changed = True
                continue
            if t["status"] != "scheduled" or _dt(t["run_at"]) > now:
                continue
            late = (now - _dt(t["run_at"])).total_seconds() / 60
            if late > OVERDUE_GRACE_MIN:
                if t["repeat"] != "none":    # повторяющуюся пропускаем до ближайшего будущего срока
                    t["run_at"] = next_run(t["run_at"], t["repeat"], now, t.get("anchor_day"))
                else:
                    t.update(status="missed", finished_at=_stamp(now),
                             result="Сервер был недоступен в назначенное время — задача пропущена.")
                changed = True
                continue
            t.update(status="running", started_at=_stamp(now))
            claimed.append(dict(t))
            changed = True
        if changed:
            _save(tasks)
    return claimed


def finish(task_id: str, ok: bool, result: str) -> dict:
    """Записывает итог запуска; повторяющаяся задача возвращается в «ожидает» со следующим сроком."""
    with _lock:
        tasks = _load()
        t = next((x for x in tasks if x["id"] == task_id), None)
        if not t:
            return {}
        t["runs"] = int(t.get("runs") or 0) + 1
        t["result"] = (result or "").strip()[:MAX_RESULT]
        t["finished_at"] = _stamp()
        t["last_ok"] = bool(ok)
        if t["repeat"] != "none" and t["status"] == "running":
            t["run_at"] = next_run(t["run_at"], t["repeat"], None, t.get("anchor_day"))
            t["status"] = "scheduled"
        else:
            t["status"] = "done" if ok else "failed"
        _save(tasks)
        return dict(t)


# ── Исполнитель инструментов ─────────────────────────────────────────────

def execute(name: str, inp: dict, state=None, chat_id=None, unattended=False, source="bot") -> dict:
    if not skill_on(state):
        return {"error": "Навык «Планирование» выключен: Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    if unattended and name in ("plan_create", "plan_update"):
        return {"error": "Во время плановой задачи новые задачи ставить нельзя."}
    try:
        if name == "plan_create":
            return create(inp, chat_id, source)
        if name == "plan_list":
            rows = list_tasks(str(inp.get("status") or ""))[:40]
            return {"tasks": [{k: r.get(k) for k in ("id", "title", "kind", "run_at", "repeat_ru", "status", "report", "result")
                               if r.get(k) not in (None, "")} for r in rows]}
        if name == "plan_update":
            return update(inp)
        if name == "plan_cancel":
            return cancel(inp)
    except PlanError as e:
        return {"error": str(e)}
    return {"error": f"неизвестный инструмент {name}"}
