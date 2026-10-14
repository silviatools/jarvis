"""
deck_skill.py — навык ассистента «Презентации».

Что здесь:
  • SKILLS — запись для каталога «Навыки ассистента» (Прочее → Настройки → Телеграм): название,
    описание, переключатель; состояние хранится в data.telegramSkills[<id>], как у остальных навыков;
  • TOOLS — инструменты для ИИ: presentation_build (собрать и отправить), presentation_get (вернуть
    сохранённое описание, чтобы поправить слайд);
  • PROMPT — методология: как ассистент ведёт себя как эксперт по презентациям;
  • хранилище колод (jarvis_decks.json): описание слайдов сохраняется, поэтому «поменяй слайд 3»
    работает и в следующих сообщениях;
  • run_build() — проверка качества → иллюстрации → сборка → предпросмотр → отправка в чат
    (и, если попросили, вложением в задание «Учёбы»).

Сам движок слайдов — deck_builder.py (без привязки к Telegram и приложению).
"""

import base64
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import deck_builder as db

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
STORE_FILE = DATA_DIR / "jarvis_decks.json"
_lock = threading.Lock()
MAX_DECKS = 20
MAX_IMAGES = 3

SKILL_ID = "presentations"

# Запись для каталога навыков (Настройки → Телеграм → «Навыки ассистента»).
SKILLS = [
    {"id": SKILL_ID, "title": "Презентации", "risk": "create", "default": True,
     "desc": ("Ассистент делает презентации как дизайнер: сам выбирает структуру, тему и макеты (титульный, цифры, "
              "карточки, диаграммы, сравнение, таймлайн, таблицы, цитаты, картинки), пишет заметки докладчика и "
              "присылает редактируемый PPTX в чат со слайдами-превью. Умеет править по просьбе («поменяй слайд 3»), "
              "сменить тему и положить файл в задание «Учёбы». Перед работой уточняет аудиторию и время выступления. "
              "Работает в Telegram-боте; превью-картинки требуют LibreOffice на сервере."),
     "tools": ["presentation_build", "presentation_get"]},
]


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


# ── инструменты ───────────────────────────────────────────────────────────────

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_ANY_LIST = {"type": "array", "items": {}}

_SLIDE = {"type": "object", "properties": {
    "layout": {"type": "string", "enum": db.LAYOUTS},
    "title": _STR, "subtitle": _STR, "meta": _STR, "number": _STR, "notes": _STR,
    "bullets": {"type": "array", "items": {}, "description": "строки или {text, sub:[строки]}"},
    "cards": {"type": "array", "items": {"type": "object", "properties": {"heading": _STR, "text": _STR, "icon": _STR}}},
    "stats": {"type": "array", "items": {"type": "object", "properties": {"value": _STR, "label": _STR}}},
    "caption": _STR, "value": _STR, "label": _STR, "takeaway": _STR,
    "left": {"type": "object", "properties": {"heading": _STR, "bullets": _STRS}},
    "right": {"type": "object", "properties": {"heading": _STR, "bullets": _STRS}},
    "quote": _STR, "author": _STR,
    "steps": {"type": "array", "items": {"type": "object", "properties": {"label": _STR, "text": _STR}}},
    "header": _STRS, "rows": {"type": "array", "items": {"type": "array", "items": {}}},
    "chart": {"type": "object", "properties": {
        "type": {"type": "string", "enum": ["column", "bar", "line", "pie", "doughnut"]},
        "categories": _STRS,
        "series": {"type": "array", "items": {"type": "object", "properties": {"name": _STR, "values": _ANY_LIST}}},
        "percent": {"type": "boolean"}}},
    "items": _STRS, "image_prompt": _STR}, "required": ["layout"]}

TOOLS = [
    {"name": "presentation_build",
     "description": (
         "Собрать презентацию (PPTX) и отправить пользователю в чат: слайды-превью картинками (если на сервере есть "
         "LibreOffice) и файл. Макеты и поля слайда: cover{title,subtitle,meta}; section{number,title,subtitle}; "
         "bullets{title,bullets[≤6]}; cards{title,cards[2–4]{heading,text,icon?}}; stats{title,stats[1–4]{value,label},caption}; "
         "compare{title,left{heading,bullets},right{heading,bullets}}; quote{quote,author}; timeline{title,steps[3–6]{label,text}}; "
         "table{title,header[],rows[][]}; chart{title,chart{type column|bar|line|pie,categories[],series[{name,values[]}]},takeaway,bullets?}; "
         "numbered{title,items[≤8]} (план/содержание); image{title,bullets,image_prompt} (иллюстрация генерируется); closing{title,subtitle,meta}. "
         "В любом слайде — notes (что говорить). Темы: minimal (светлая деловая), warm (тёплая, авторская), dark (тёмная, питч), "
         "academic (строгая, учёба). В тексте допустимы **жирный** и *курсив*. Чтобы ПОПРАВИТЬ уже сделанную презентацию — "
         "сначала presentation_get, затем снова presentation_build с тем же deck_id и изменённым списком slides (слайды целиком); "
         "только смена темы/акцента — deck_id без slides. Если вернулись fix_first — исправь замечания и вызови снова (повторный вызов "
         "отправит как есть). Файл в «Учёбу»: study{subject, task_title | task_id, create_if_missing}."),
     "input_schema": {"type": "object", "properties": {
         "title": _STR, "theme": {"type": "string", "enum": list(db.THEMES)}, "accent": {"type": "string", "description": "свой акцентный цвет #RRGGBB"},
         "author": _STR, "minutes": {"type": "number", "description": "длительность выступления, мин (для проверки числа слайдов)"},
         "slides": {"type": "array", "items": _SLIDE}, "deck_id": _STR,
         "preview": {"type": "boolean", "description": "прислать слайды картинками (по умолчанию да)"},
         "also_pdf": {"type": "boolean", "description": "заодно прислать PDF (нужен LibreOffice на сервере)"},
         "send_to_chat": {"type": "boolean", "description": "прислать файл в чат (по умолчанию да)"},
         "study": {"type": "object", "properties": {"subject": _STR, "task_title": _STR, "task_id": _STR, "create_if_missing": {"type": "boolean"}}}},
         "required": []}},
    {"name": "presentation_get",
     "description": "Сохранённое описание презентации (темa, слайды) — чтобы изменить слайд и пересобрать через presentation_build с тем же deck_id. Без deck_id — последняя презентация этого чата.",
     "input_schema": {"type": "object", "properties": {"deck_id": _STR}, "required": []}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


# ── методология (кусок системного промпта) ────────────────────────────────────

PROMPT = (
    "11. ПРЕЗЕНТАЦИИ — ты эксперт-дизайнер презентаций. Инструменты: presentation_build, presentation_get. Работай так:\n"
    "а) Прежде чем делать, выясни недостающее ОДНИМ коротким сообщением (не больше 3 вопросов): для кого (аудитория), сколько "
    "минут выступление, цель и тон, язык; есть ли материалы/данные. Если пользователь всё сказал или попросил «быстро, без "
    "вопросов» — сразу делай. Не выдумывай цифры и факты: нет данных — не пиши статистику, либо помечай «уточнить» в caption.\n"
    "б) Структура: титульный (cover) → план (numbered, если ≥ 6 слайдов) → содержательные слайды → итог/выводы → closing. "
    "Ориентир: ≈ 1–1,5 слайда на минуту выступления. Одна мысль на слайд; заголовок — это ВЫВОД слайда, а не тема "
    "(«Конфликты съедают 20% времени руководителя», а не «Статистика»). ≤ 6 пунктов, ≤ 12 слов в пункте; подробности — в notes.\n"
    "в) Чередуй макеты, не делай всё bullets: числа и доли — stats или chart; 2–4 понятия — cards; «до/после», «за/против» — compare; "
    "процесс и этапы — timeline; перечень признаков — table; сильная мысль — quote; смена раздела — section; "
    "иллюстрация — image (максимум 3 на колоду, это дорого и долго).\n"
    "г) Тема: учёба/защита — academic; бизнес и отчёты — minimal; личные истории, творчество — warm; питч и сцена — dark. "
    "Один акцентный цвет (accent) — если пользователь назвал бренд или цвет.\n"
    "д) К каждому слайду пиши notes: что сказать (30–60 секунд, живым языком), без пересказа заголовка.\n"
    "е) После отправки ответь коротко: тема, число слайдов, нумерованный список заголовков слайдов и предложение что можно "
    "поменять («сделать тему тёмной», «переписать слайд 4», «добавить диаграмму»). Не пересказывай содержимое слайдов. "
    "Если в результате preview сказано, что превью недоступно — просто упомяни, что слайды можно посмотреть в файле.\n"
    "ж) Правки: «поменяй слайд N» — presentation_get(deck_id), измени нужный слайд, пересобери с тем же deck_id (остальные слайды "
    "оставь без изменений). deck_id смотри в пометках истории («презентация … (deck_id …)»).\n\n"
)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


# ── хранилище ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"decks": {}}


def _save(st: dict):
    decks = st.setdefault("decks", {})
    if len(decks) > MAX_DECKS:
        for k in sorted(decks, key=lambda k: decks[k].get("updated", 0))[:len(decks) - MAX_DECKS]:
            del decks[k]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STORE_FILE)


def get_deck(chat_id, deck_id=None):
    with _lock:
        decks = _load().get("decks", {})
    if deck_id:
        d = decks.get(deck_id)
        return (deck_id, d) if d and d.get("chat_id") == chat_id else (None, None)
    mine = [(k, d) for k, d in decks.items() if d.get("chat_id") == chat_id]
    if not mine:
        return None, None
    k, d = max(mine, key=lambda kd: kd[1].get("updated", 0))
    return k, d


# ── сборка и доставка ─────────────────────────────────────────────────────────

def _safe_name(title: str) -> str:
    n = re.sub(r'[\\/:*?"<>|\r\n]+', "", title or "").strip(" .")[:60]
    return (n or "Презентация") + ".pptx"


def _gen_images(spec, ctx, tg_bot) -> tuple:
    """Иллюстрации для макета image — параллельно. → ({prompt: bytes}, предупреждения)."""
    prompts = []
    for s in spec["slides"]:
        p = s.get("image_prompt")
        if s["layout"] == "image" and p and p not in prompts:
            prompts.append(p)
    prompts = prompts[:MAX_IMAGES]
    warns, out = [], {}
    if not prompts:
        return out, warns
    if not getattr(ctx, "openai_key", ""):
        return out, ["иллюстрации не сгенерированы: нет ключа OpenAI (Настройки → Ассистент) — слайды с картинкой собраны без неё"]
    accent = spec.get("accent") or db.THEMES[spec["theme"]]["accent"]

    def work(p):
        try:
            out[p] = tg_bot.generate_image_bytes(
                ctx.openai_key, f"{p}. Style: minimal modern editorial illustration, flat shapes, soft light, generous negative space, "
                                f"no text, no letters, accent color #{accent}", "portrait")
        except Exception as e:
            warns.append(f"иллюстрация «{p[:40]}» не получилась: {e}")

    ths = [threading.Thread(target=work, args=(p,), daemon=True) for p in prompts]
    [t.start() for t in ths]
    [t.join(timeout=170) for t in ths]
    return out, warns


def run_build(inp: dict, ctx, tg_bot, study_attach=None) -> dict:
    inp = inp or {}
    deck_id = (inp.get("deck_id") or "").strip()
    stored = None
    if deck_id:
        _, stored = get_deck(ctx.chat_id, deck_id)
        if not stored:
            return {"error": "презентация с таким deck_id не найдена в этом чате — собери новую (без deck_id)"}
    spec_in = {k: inp[k] for k in ("title", "theme", "accent", "font", "author", "slides") if inp.get(k)}
    if stored:
        spec_in = {**stored["spec"], **spec_in}
    try:
        spec = db.normalize(spec_in)
    except db.DeckError as e:
        return {"error": str(e)}

    # Один круг «исправь замечания» перед отправкой: пользователь получает только готовый результат.
    warnings = db.lint(spec, inp.get("minutes") or 0)
    rounds = getattr(ctx, "deck_fix_rounds", 0)
    if warnings and rounds < 1:
        ctx.deck_fix_rounds = rounds + 1
        return {"ok": False, "sent": False, "fix_first": warnings,
                "hint": "Исправь замечания в slides и вызови presentation_build ещё раз (повторный вызов отправит презентацию как есть)."}

    fname = _safe_name(spec["title"])
    stages = [(0, "Продумываю структуру"), (5, "Раскладываю слайды"), (14, "Рисую иллюстрации"), (40, "Собираю файл и превью")]
    with tg_bot.ProgressMessage(ctx.token, ctx.chat_id, "Собираю презентацию", spec["title"], stages, expected=40.0):
        imgs, warns = _gen_images(spec, ctx, tg_bot)
        try:
            data = db.build(spec, imgs)
        except db.DeckError as e:
            return {"error": str(e)}
        warnings += warns
        want_preview = inp.get("preview") is not False
        pngs = pdf = None
        if (want_preview or inp.get("also_pdf")) and db.preview_available():
            pngs, pdf = db.render_png(data)
        # сохраняем описание (без картинок): по нему правятся слайды
        with _lock:
            st = _load()
            new_id = deck_id or uuid.uuid4().hex[:8]
            prev = st.setdefault("decks", {}).get(new_id) or {}
            ver = int(prev.get("version") or 0) + 1
            st["decks"][new_id] = {"chat_id": ctx.chat_id, "title": spec["title"], "spec": spec, "version": ver,
                                   "created": prev.get("created") or time.time(), "updated": time.time()}
            _save(st)
        out = {"ok": True, "deck_id": new_id, "version": ver, "slides": len(spec["slides"]), "theme": spec["theme"]}
        caption = f"<b>{spec['title']}</b> · {len(spec['slides'])} слайдов · тема «{spec['theme']}»"
        if inp.get("send_to_chat") is not False:
            if want_preview:
                if pngs:
                    try:
                        tg_bot.send_album(ctx, pngs[:10], f"<b>{spec['title']}</b> — предпросмотр"
                                          + (f" (первые 10 из {len(spec['slides'])})" if len(spec["slides"]) > 10 else ""))
                        out["preview"] = "отправлен"
                    except Exception as e:
                        out["preview"] = f"не отправился: {e}"
                else:
                    out["preview"] = "недоступен: на сервере нет LibreOffice (слайды можно посмотреть в файле)"
            sent = tg_bot.file_send(ctx, filename=fname, content_base64=base64.b64encode(data).decode(), caption=caption)
            out["file_sent"] = bool(sent.get("ok"))
            if not sent.get("ok"):
                out["file_error"] = sent.get("error")
            if inp.get("also_pdf"):
                if pdf:
                    tg_bot.file_send(ctx, filename=fname[:-5] + ".pdf", content_base64=base64.b64encode(pdf).decode())
                    out["pdf"] = "отправлен"
                else:
                    out["pdf"] = "недоступен: на сервере нет LibreOffice"
        study = inp.get("study")
        if isinstance(study, dict) and study_attach:
            out["study"] = study_attach({**study, "filename": fname, "content_base64": base64.b64encode(data).decode()})
        if warnings:
            out["warnings"] = warnings
        ctx.sent.append(f"презентация «{spec['title']}» (deck_id {new_id}, версия {ver}, {len(spec['slides'])} слайдов, тема {spec['theme']})")
        return out


def execute(name: str, inp: dict, ctx, tg_bot, state=None, study_attach=None) -> dict:
    if not skill_on(state):
        return {"error": "Навык «Презентации» выключен: Настройки → Телеграм → Навыки ассистента."}
    if ctx is None:
        return {"error": "Презентации доступны только в Telegram-чате."}
    try:
        if name == "presentation_build":
            return run_build(inp, ctx, tg_bot, study_attach)
        if name == "presentation_get":
            k, d = get_deck(ctx.chat_id, (inp or {}).get("deck_id"))
            if not d:
                return {"error": "сохранённых презентаций в этом чате нет"}
            return {"deck_id": k, "version": d.get("version"), "spec": d["spec"]}
    except Exception as e:
        return {"error": f"не удалось собрать презентацию: {type(e).__name__}: {e}"}
    return {"error": "unknown_tool"}
