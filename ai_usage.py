"""
ai_usage.py — модели ИИ по ролям, уровни рассуждений и журнал расхода.

• Каталог моделей (MODELS): цены за 1M токенов и поддерживаемые уровни рассуждений (reasoning_effort).
  Уровень нигде не зашит в код вызовов — он берётся из settings.aiModels (Настройки → Ассистент),
  а здесь только справочник «что поддерживает модель».
• Роли (main / complex / background и отдельные задачи): resolve_role(settings, role).
• Журнал расхода: jarvis_ai_usage.jsonl (в бэкапе) — по записи на каждый запрос к модели и на каждый ход;
  jarvis_ai_turns.jsonl (не в бэкапе, 14 дней) — текст сообщения и вызванные инструменты для проверки маршрутизатора групп.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_NAME = "jarvis_ai_usage.jsonl"
TURNS_NAME = "jarvis_ai_turns.jsonl"
USAGE_KEEP_DAYS = 120
TURNS_KEEP_DAYS = 14
MSK = timezone(timedelta(hours=3))
_lock = threading.RLock()

EFFORT_LABELS = {
    "": "по умолчанию модели", "none": "без рассуждений", "low": "низкий", "medium": "средний",
    "high": "высокий", "xhigh": "очень высокий", "max": "максимум",
}

_FULL = ["none", "low", "medium", "high", "xhigh", "max"]
_NO_NONE = ["low", "medium", "high", "xhigh", "max"]
_OLD = ["none", "low", "medium", "high", "xhigh"]

# id → цены ($ за 1M токенов, контекст ≤272K) и поддерживаемые уровни рассуждений.
# write — цена записи в кэш (30 мин); None — у модели платной записи нет.
MODELS = {
    "gpt-6-luna":    {"title": "GPT-6 Luna — экономичная, простые и массовые задачи", "in": 0.10, "cache": 0.01, "write": 0.125, "out": 0.50, "efforts": _FULL},
    "gpt-6.1-terra": {"title": "GPT-6.1 Terra — тексты, изображения, интерфейсы", "in": 1.00, "cache": 0.05, "write": 1.25, "out": 5.00, "efforts": _FULL},
    "gpt-6.1-sol":   {"title": "GPT-6.1 Sol — универсальная, сложное, код", "in": 2.00, "cache": 0.10, "write": 2.50, "out": 10.00, "efforts": _NO_NONE},
    "gpt-6-sol":     {"title": "GPT-6 Sol", "in": 2.00, "cache": 0.20, "write": 2.50, "out": 10.00, "efforts": _NO_NONE},
    "gpt-6-astra":   {"title": "GPT-6 Astra — максимальное качество", "in": 10.00, "cache": 1.00, "write": 12.50, "out": 50.00, "efforts": _NO_NONE},
    "gpt-5.6-luna":  {"title": "GPT-5.6 Luna", "in": 0.20, "cache": 0.02, "write": 0.25, "out": 1.20, "efforts": _FULL},
    "gpt-5.6-terra": {"title": "GPT-5.6 Terra", "in": 2.00, "cache": 0.20, "write": 2.50, "out": 12.00, "efforts": _FULL},
    "gpt-5.6-sol":   {"title": "GPT-5.6 Sol", "in": 4.00, "cache": 0.40, "write": 5.00, "out": 20.00, "efforts": _FULL},
    "gpt-5.5":       {"title": "GPT-5.5", "in": 5.00, "cache": 0.50, "write": None, "out": 30.00, "efforts": _OLD},
    "gpt-5.5-pro":   {"title": "GPT-5.5 Pro", "in": 30.00, "cache": None, "write": None, "out": 180.00, "efforts": ["medium", "high", "xhigh"]},
    "gpt-5.4":       {"title": "GPT-5.4 (прежняя)", "in": 2.50, "cache": 0.25, "write": None, "out": 15.00, "efforts": _OLD},
    "gpt-5.4-mini":  {"title": "GPT-5.4 mini", "in": 0.75, "cache": 0.075, "write": None, "out": 4.50, "efforts": _OLD},
    "gpt-5.4-nano":  {"title": "GPT-5.4 nano", "in": 0.20, "cache": 0.02, "write": None, "out": 1.25, "efforts": _OLD},
    "gpt-5.4-pro":   {"title": "GPT-5.4 Pro", "in": 30.00, "cache": None, "write": None, "out": 180.00, "efforts": ["medium", "high", "xhigh"]},
    "gpt-5.2":       {"title": "GPT-5.2", "in": 1.75, "cache": 0.175, "write": None, "out": 14.00, "efforts": _OLD},
    "gpt-5.2-pro":   {"title": "GPT-5.2 Pro", "in": 21.00, "cache": None, "write": None, "out": 168.00, "efforts": ["medium", "high", "xhigh"]},
    "gpt-4o":        {"title": "GPT-4o", "in": 2.50, "cache": 1.25, "write": None, "out": 10.00, "efforts": []},
    "gpt-4o-mini":   {"title": "GPT-4o mini", "in": 0.15, "cache": 0.075, "write": None, "out": 0.60, "efforts": []},
}
MODEL_ORDER = ["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
               "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2", "gpt-4o", "gpt-4o-mini"]

# Роли и отдельные задачи. fallback — чья настройка берётся, если у задачи модель не указана.
ROLES = {
    "main": {"label": "Основная", "hint": "чат в Telegram, голос в приложении, плановые поручения", "fallback": None},
    "complex": {"label": "Сложная", "hint": "эскалация: код, большие документы, многошаговые задачи, неудача основной", "fallback": "main"},
    "background": {"label": "Фоновая", "hint": "простые одиночные запросы без инструментов", "fallback": "main"},
    "studyBrief": {"label": "Сбор информации по задаче", "hint": "фоновый агент «Учёбы» (до 18 шагов)", "fallback": "main"},
    "triage": {"label": "Контроль учебных чатов", "hint": "ежедневный разбор новых сообщений", "fallback": "background"},
    "title": {"label": "Название диалога", "hint": "короткий заголовок по первому сообщению", "fallback": "background"},
    "notesTargets": {"label": "Чаты из «Базы данных»", "hint": "поиск учебных чатов в заметках", "fallback": "background"},
}
TASK_ROLES = ["studyBrief", "triage", "title", "notesTargets"]


def default_ai_models() -> dict:
    return {
        "enabled": True,
        "main": {"model": "gpt-6.1-terra", "effort": "low"},
        "complex": {"model": "gpt-6.1-sol", "effort": "medium"},
        "background": {"model": "gpt-6-luna", "effort": "none"},
        "studyBrief": {"model": "", "effort": "medium"},
        "triage": {"model": "", "effort": "low"},
        "title": {"model": "", "effort": ""},
        "notesTargets": {"model": "", "effort": ""},
        "escalation": True,
        "escalationDailyLimit": 40,
        "toolGroups": False,
        "promptCache": True,
        "compactPrompt": True,
        "turnLog": True,
        "prev": None,
    }


def normalize(settings) -> dict:
    """settings.aiModels, дополненный значениями по умолчанию (поля, которых нет, берутся из default_ai_models)."""
    base = default_ai_models()
    cur = (settings or {}).get("aiModels")
    if not isinstance(cur, dict):
        return base
    out = dict(base)
    for k, v in cur.items():
        if k in ROLES and isinstance(v, dict):
            out[k] = {"model": str(v.get("model") or "").strip(), "effort": str(v.get("effort") or "").strip()}
        elif k in base:
            out[k] = v
    try:
        out["escalationDailyLimit"] = max(0, int(out.get("escalationDailyLimit") or 0))
    except (TypeError, ValueError):
        out["escalationDailyLimit"] = base["escalationDailyLimit"]
    return out


def model_info(model: str):
    """Запись каталога по id модели, в том числе по снимку («gpt-5.4-2026-03-05»); None — модель неизвестна."""
    m = (model or "").strip().lower()
    if m in MODELS:
        return MODELS[m]
    best = None
    for k in MODELS:
        if m.startswith(k + "-") and (best is None or len(k) > len(best)):
            best = k
    return MODELS[best] if best else None


def efforts_for(model: str) -> list:
    """Какие уровни рассуждений принимает модель (без пустого «по умолчанию»)."""
    info = model_info(model)
    if info:
        return list(info["efforts"])
    m = (model or "").strip().lower()
    if re.match(r"^(gpt-([5-9]|\d\d)|o\d)", m):
        return ["low", "medium", "high"]
    return []


def supports_effort(model: str) -> bool:
    return bool(efforts_for(model))


def fit_effort(model: str, effort: str) -> str:
    """Подгоняет выбранный уровень под модель: неподдерживаемый «none» → ближайший низкий, чужой → пусто (по умолчанию)."""
    effort = (effort or "").strip()
    allowed = efforts_for(model)
    if not effort or not allowed:
        return ""
    if effort in allowed:
        return effort
    if effort == "none":
        return "low" if "low" in allowed else allowed[0]
    return ""


def catalog() -> dict:
    """Справочник для интерфейса и браузерного агента."""
    models = []
    for mid in MODEL_ORDER:
        i = MODELS[mid]
        models.append({"id": mid, "title": i["title"], "in": i["in"], "out": i["out"], "cache": i["cache"], "efforts": i["efforts"]})
    return {
        "models": models,
        "effortLabels": EFFORT_LABELS,
        "roles": [{"key": k, "label": v["label"], "hint": v["hint"], "fallback": v["fallback"]} for k, v in ROLES.items()],
        "defaults": default_ai_models(),
    }


def resolve_role(settings, role: str, preset_model: str = "") -> dict:
    """{model, effort, role, managed}: модель и уровень роли.
    managed=False — роли выключены («всё на одну модель»): модель пресета и уровень не передаётся (поведение как раньше)."""
    cfg = normalize(settings)
    if not cfg.get("enabled"):
        return {"model": preset_model, "effort": "", "role": role, "managed": False}
    seen, key, model, effort = set(), role, "", ""
    # у задачи свой уровень, а пустую модель наследует от родительской роли
    own = cfg.get(role) or {}
    effort = own.get("effort") or ""
    while key and key not in seen:
        seen.add(key)
        entry = cfg.get(key) or {}
        if entry.get("model"):
            model = entry["model"]
            if not effort:
                effort = entry.get("effort") or ""
            break
        if not effort:
            effort = entry.get("effort") or ""
        key = (ROLES.get(key) or {}).get("fallback")
    model = model or preset_model
    return {"model": model, "effort": fit_effort(model, effort), "role": role, "managed": True}


# ── Цены ─────────────────────────────────────────────────────────────────────

def cost(model: str, inp: int = 0, cached: int = 0, out: int = 0, write: int = None) -> float:
    """Стоимость запроса, $. Для моделей с платной записью в кэш весь некэшированный вход (≥1024) считаем записью."""
    info = model_info(model)
    if not info:
        return 0.0
    inp, cached, out = int(inp or 0), int(cached or 0), int(out or 0)
    fresh = max(0, inp - cached)
    wprice = info.get("write")
    if wprice is None:
        w, plain = 0, fresh
    else:
        w = int(write) if write is not None else (fresh if inp >= 1024 else 0)
        w = min(w, fresh)
        plain = fresh - w
    total = plain * info["in"] + w * (wprice or 0) + cached * (info.get("cache") if info.get("cache") is not None else info["in"]) + out * info["out"]
    return round(total / 1e6, 6)


# ── Журнал ───────────────────────────────────────────────────────────────────

def _dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def usage_path() -> Path:
    return _dir() / USAGE_NAME


def turns_path() -> Path:
    return _dir() / TURNS_NAME


def _now() -> float:
    return time.time()


def _append(path: Path, row: dict, keep_days: int):
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if path.stat().st_size > 2_500_000:
                _trim(path, keep_days)
        except Exception as e:
            print(f"  ai_usage: {e.__class__.__name__}: {e}", flush=True)


def _trim(path: Path, keep_days: int):
    cutoff = _now() - keep_days * 86400
    rows = [r for r in _read(path) if r.get("ts", 0) >= cutoff]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def _read(path: Path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def usage_from_openai(u: dict) -> dict:
    u = u or {}
    det = u.get("prompt_tokens_details") or {}
    out_det = u.get("completion_tokens_details") or {}
    return {"in": int(u.get("prompt_tokens") or 0), "cached": int(det.get("cached_tokens") or 0),
            "write": det.get("cache_write_tokens"), "out": int(u.get("completion_tokens") or 0),
            "reasoning": int(out_det.get("reasoning_tokens") or 0)}


def usage_from_claude(u: dict) -> dict:
    u = u or {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    created = int(u.get("cache_creation_input_tokens") or 0)
    return {"in": int(u.get("input_tokens") or 0) + cached + created, "cached": cached, "write": created or None,
            "out": int(u.get("output_tokens") or 0), "reasoning": 0}


def record_call(*, provider: str, model: str, role: str = "", func: str = "", effort: str = "", usage: dict = None,
                step: int = 0, source: str = "server", **extra):
    """Одна запись на каждый запрос к модели. usage — результат usage_from_openai / usage_from_claude."""
    u = usage or {}
    row = {"kind": "call", "ts": round(_now(), 3), "provider": provider, "model": model, "role": role, "func": func,
           "effort": effort or "", "step": step, "in": u.get("in", 0), "cached": u.get("cached", 0), "out": u.get("out", 0),
           "reasoning": u.get("reasoning", 0), "src": source}
    row["usd"] = cost(model, u.get("in", 0), u.get("cached", 0), u.get("out", 0), u.get("write")) if provider == "openai" else 0.0
    row.update(extra)
    _append(usage_path(), row, USAGE_KEEP_DAYS)
    return row


def record_turn(*, func: str, role: str, model: str, steps: int = 0, ok: bool = True, tool_errors: int = 0, tools: list = None,
                escalated: str = "", groups: list = None, tools_more: int = 0, confirm: bool = False, chat=None, text: str = "",
                log_text: bool = True):
    """Итог хода: нужен для метрик качества и дневного лимита эскалаций."""
    now = _now()
    row = {"kind": "turn", "ts": round(now, 3), "func": func, "role": role, "model": model, "steps": steps, "ok": bool(ok),
           "tool_errors": tool_errors, "esc": escalated or "", "groups": sorted(groups) if groups else [],
           "tools_more": tools_more, "confirm": bool(confirm)}
    _append(usage_path(), row, USAGE_KEEP_DAYS)
    if log_text and text:
        _append(turns_path(), {"ts": round(now, 3), "func": func, "chat": chat, "text": text[:600], "tools": sorted(set(tools or [])),
                               "groups": row["groups"], "ok": bool(ok)}, TURNS_KEEP_DAYS)
    return row


def turns_log(days: int = TURNS_KEEP_DAYS) -> list:
    cutoff = _now() - days * 86400
    return [r for r in _read(turns_path()) if r.get("ts", 0) >= cutoff]


def _msk_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")


def escalations_today() -> int:
    day = _msk_day(_now())
    return sum(1 for r in _read(usage_path()) if r.get("kind") == "turn" and r.get("esc") and _msk_day(r.get("ts", 0)) == day)


def summary(days: int = 7, by: str = "day") -> dict:
    """Агрегат журнала за последние days суток: итоги и строки по day|model|func|role."""
    days = max(1, min(int(days or 7), 120))
    cutoff = _now() - days * 86400
    rows = [r for r in _read(usage_path()) if r.get("ts", 0) >= cutoff]
    calls = [r for r in rows if r.get("kind") == "call"]
    turns = [r for r in rows if r.get("kind") == "turn"]

    def key(r):
        if by == "day":
            return _msk_day(r.get("ts", 0))
        return str(r.get({"model": "model", "func": "func", "role": "role"}.get(by, "model")) or "—")

    groups = {}
    for r in calls:
        g = groups.setdefault(key(r), {"key": key(r), "calls": 0, "in": 0, "cached": 0, "out": 0, "usd": 0.0})
        g["calls"] += 1
        g["in"] += r.get("in", 0)
        g["cached"] += r.get("cached", 0)
        g["out"] += r.get("out", 0)
        g["usd"] += r.get("usd", 0.0)
    lines = sorted(groups.values(), key=lambda g: g["key"], reverse=(by == "day"))
    for g in lines:
        g["usd"] = round(g["usd"], 4)
        g["cachePct"] = round(100 * g["cached"] / g["in"]) if g["in"] else 0
    tin = sum(r.get("in", 0) for r in calls)
    tcached = sum(r.get("cached", 0) for r in calls)
    chat_turns = [t for t in turns if t.get("func") in ("chat", "plan_task", "study_brief", "voice")]
    quality = {
        "turns": len(chat_turns),
        "notFinished": sum(1 for t in chat_turns if not t.get("ok")),
        "toolErrors": sum(t.get("tool_errors", 0) for t in chat_turns),
        "escalated": sum(1 for t in chat_turns if t.get("esc")),
        "toolsMore": sum(t.get("tools_more", 0) for t in chat_turns),
        "avgSteps": round(sum(t.get("steps", 0) for t in chat_turns) / len(chat_turns), 1) if chat_turns else 0,
    }
    return {
        "days": days, "by": by, "lines": lines,
        "total": {"calls": len(calls), "in": tin, "cached": tcached, "out": sum(r.get("out", 0) for r in calls),
                  "usd": round(sum(r.get("usd", 0.0) for r in calls), 4), "cachePct": round(100 * tcached / tin) if tin else 0,
                  "avgInPerCall": round(tin / len(calls)) if calls else 0},
        "quality": quality,
    }
