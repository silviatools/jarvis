"""
tool_groups.py — группы инструментов ассистента по смыслу запроса.

Зачем: с каждым шагом цикла в запрос уходят ВСЕ инструменты (до ~94, ~18 тыс. токенов). Если включён переключатель
Настройки → Ассистент → «Инструменты» (settings.aiModels.toolGroups), модель получает только нужные группы:
выбор — по ключевым словам и контексту диалога, без лишнего вызова модели. Не хватило — модель вызывает tools_more
(или просто вызывает инструмент вне набора: если навык включён, он выполнится, а группа добавится).

Группы работают ПОВЕРХ фильтра навыков (skill_on): выключенный навык не появится ни через tools_more, ни иначе.
Новый навык ассистента → добавить его инструменты в GROUPS (тест test_tool_groups.py проверяет, что каждый инструмент в группе).
"""

import re
import threading
import time

ALWAYS = ("core", "plan")

# группа → (название, инструменты точными именами, префиксы имён)
GROUPS = {
    "core": {"title": "Данные и записи", "names": ["get_data", "navigate", "create_record", "update_record", "delete_record", "tools_more"], "prefixes": []},
    "plan": {"title": "Напоминания и плановые задачи", "names": [], "prefixes": ["plan_"]},
    "house": {"title": "Канбан «Задачи»", "names": [], "prefixes": ["kanban_"]},
    "food": {"title": "Питание и готовка", "names": ["log_diet_compliance", "log_checklist_answer"], "prefixes": ["cooking_plan_"]},
    "apple": {"title": "Apple Календарь и Напоминания", "names": [], "prefixes": ["calendar_", "reminders_"]},
    "weather": {"title": "Погода", "names": ["get_weather"], "prefixes": []},
    "study": {"title": "Учёба", "names": ["study_schedule", "study_attach_file", "study_get_attachment", "study_brief_start", "study_chats_scan_now"], "prefixes": []},
    "bot": {"title": "Файлы, чек-листы, картинки, опросы в чат", "names": [
        "tg_send_checklist", "tg_update_checklist", "tg_get_checklists", "tg_send_file", "tg_generate_image", "tg_send_poll",
        "tg_send_buttons", "tg_send_location", "tg_send_contact", "tg_send_dice", "tg_react", "tg_pin_message"], "prefixes": []},
    "deck": {"title": "Презентации", "names": [], "prefixes": ["presentation_"]},
    "tg_read": {"title": "Личный Telegram: чтение и поиск", "names": [
        "tg_me", "tg_sessions", "tg_list_folders", "tg_list_dialogs", "tg_unread_digest", "tg_chat_info", "tg_list_contacts",
        "tg_find_peer", "tg_list_members", "tg_read_messages", "tg_search_messages", "tg_transcribe", "tg_get_file", "tg_get_replies"], "prefixes": []},
    "tg_write": {"title": "Личный Telegram: отправка и правки", "names": ["tg_send_message", "tg_edit_message", "tg_forward_messages"], "prefixes": []},
    "tg_topics": {"title": "Личный Telegram: темы групп", "names": [], "prefixes": ["tg_topic_", "tg_list_topics", "tg_job_status"]},
    "tg_org": {"title": "Личный Telegram: порядок в чатах", "names": ["tg_mark_read", "tg_archive_chat", "tg_mute_chat", "tg_pin_chat", "tg_folder_edit"], "prefixes": []},
    "tg_danger": {"title": "Личный Telegram: удаление и блокировки", "names": ["tg_delete_messages", "tg_join_chat", "tg_leave_chat", "tg_block_user"], "prefixes": []},
    "tg_watch": {"title": "Слежение за чатами", "names": [], "prefixes": ["tg_watch_"]},
    "market": {"title": "Маркетплейсы", "names": [], "prefixes": ["market_"]},
    "mail": {"title": "Почта", "names": [], "prefixes": ["mail_"]},
}

# Ключевые слова — основы слов (без окончаний), нижний регистр.
KEYWORDS = {
    "house": r"канбан|задач|доск|колонк|подзадач|в работу|сделать до",
    "food": r"диет|ккал|калор|готовк|продукт|порци|рацион|чеклист.{0,10}(питани|день)|завтрак|обед|ужин",
    "apple": r"календар|событи|встреч|напомин.{0,12}(apple|айфон|iphone)|в напоминани|reminders|на айфон|будильник|созвон",
    "weather": r"погод|дожд|снег|температур|прогноз|зонт|жарк|холодн|ветер",
    "study": r"учеб|универ|пар[аыу]\b|расписан|предмет|задани|лекци|семинар|курсов|экзамен|зач[её]т|препод|аудитори|rea\.ru|рэу|вложени",
    "bot": r"файл|документ|отч[её]т|таблиц|список|чек.?лист|покуп|нарисуй|картин|изображени|фото|логотип|аватар|опрос|голосован|кнопк|локаци|контакт|кубик|pdf|docx|xlsx|pptx|excel|word|csv|сформируй|составь|сгенерируй|скинь|пришли",
    "deck": r"презентац|слайд|powerpoint|pptx|питч",
    "tg_read": r"телеграм|telegram|\btg\b|чат|переписк|сообщени|написал|писал|канал|групп|непрочитан|диалог|контакт|участник|голосов|кружк|найди в",
    "tg_write": r"напиши\s+(ему|ей|им|в\s|[@а-я]+\s+что)|отправь|ответь|перешли|перепиши|отредактируй|исправь сообщени|от моего имени",
    "tg_topics": r"тем[аыуе]\b|топик|форум|перенес|перенос|скопируй сообщени",
    "tg_org": r"архив|мьют|замьют|закреп.{0,6}чат|папк|прочитано|отметь прочитан",
    "tg_danger": r"удали сообщени|удалить сообщени|вступи|выйди из|покинь|заблокируй|блок",
    "tg_watch": r"следи|слежен|отслеживай|уведомляй.{0,12}чат|алиас|watch",
    "market": r"озон|ozon|вб\b|wildberries|вайлдберриз|маркетплейс|заказ|доставк|возврат|избранн|посылк|трек",
    "mail": r"почт|письм|ящик|inbox|mail|imap|вложен.{0,6}письм|от кого пришл",
}
_KW = {g: re.compile(p, re.I) for g, p in KEYWORDS.items()}
# Если сработали «чтение» или «запись» — приводим связанное: отправка тянет чтение (найти адресата).
IMPLIES = {"tg_write": ["tg_read"], "tg_topics": ["tg_read"], "tg_org": ["tg_read"], "tg_danger": ["tg_read"], "tg_watch": ["tg_read"]}

MORE_TOOL = {
    "name": "tools_more",
    "description": ("Подключить дополнительные группы инструментов, если нужного нет в текущем наборе. "
                    "Группы: house — канбан; food — питание/готовка; apple — календарь и напоминания Apple; weather — погода; "
                    "study — учёба, файлы заданий; bot — файлы, чек-листы, картинки, опросы в чат; deck — презентации; "
                    "tg_read/tg_write/tg_topics/tg_org/tg_danger — личный Telegram (чтение, отправка, темы, порядок, удаление); "
                    "tg_watch — слежение за чатами; market — маркетплейсы; mail — почта. После вызова новые инструменты доступны сразу."),
    "input_schema": {"type": "object",
                     "properties": {"groups": {"type": "array", "items": {"type": "string", "enum": [g for g in GROUPS if g not in ALWAYS]}},
                                    "reason": {"type": "string"}},
                     "required": ["groups"]},
}

_NAME2GROUP = {}
for _g, _d in GROUPS.items():
    for _n in _d["names"]:
        _NAME2GROUP.setdefault(_n, _g)


def group_of(name: str):
    if name in _NAME2GROUP:
        return _NAME2GROUP[name]
    for g, d in GROUPS.items():
        if any(name.startswith(p) for p in d["prefixes"]):
            return g
    return None


def unassigned(tools: list) -> list:
    return sorted({t["name"] for t in tools if group_of(t["name"]) is None})


def catalog_line(active: set) -> str:
    rest = [f"{g} — {GROUPS[g]['title']}" for g in GROUPS if g not in active and g not in ALWAYS]
    if not rest:
        return ""
    return ("Часть инструментов подключается по смыслу запроса. Если нужного нет — вызови tools_more(groups) или сразу нужный инструмент. "
            "Сейчас не подключены: " + "; ".join(rest) + ".\n\n")


# ── Липкие группы диалога ────────────────────────────────────────────────────

STICKY_TTL = 600
_sticky = {}
_lock = threading.Lock()


def sticky(chat_id) -> set:
    with _lock:
        ts, groups = _sticky.get(chat_id, (0, set()))
        if time.time() - ts > STICKY_TTL:
            _sticky.pop(chat_id, None)
            return set()
        return set(groups)


def remember(chat_id, groups: set):
    if chat_id is None:
        return
    with _lock:
        _sticky[chat_id] = (time.time(), set(groups) - set(ALWAYS))


def forget(chat_id):
    with _lock:
        _sticky.pop(chat_id, None)


def pick(text: str, *, chat_id=None, has_files: bool = False, anchor: str = "", extra: set = None) -> set:
    """Набор групп для хода. text — сообщение пользователя; anchor — подпись якоря ответа (письмо/отчёт «Учёбы»)."""
    t = (text or "").lower()
    found = {g for g, rx in _KW.items() if rx.search(t)}
    prev = sticky(chat_id)
    found |= prev                # «да», «ок», «а ещё…» и обычные реплики — продолжаем с прежним набором диалога
    if has_files:
        found |= {"bot", "study"}
    a = (anchor or "").lower()
    if a.startswith("почта"):
        found.add("mail")
    if "учебн" in a or "задача:" in a:
        found |= {"study", "tg_read"}
    for g in list(found):
        found |= set(IMPLIES.get(g, []))
    found |= set(extra or ())
    if not found:
        found = {"bot"}
    return found | set(ALWAYS)


def filter_tools(tools: list, groups: set) -> list:
    """Оставляет инструменты активных групп (по одному на имя), в стабильном порядке: группа, затем имя; без группы — всегда."""
    order = list(GROUPS)
    seen, out = set(), []
    for t in tools:
        n = t["name"]
        g = group_of(n)
        if n in seen or (g is not None and g not in groups):
            continue          # инструмент без группы (забыли добавить в GROUPS) не пропадает — тест его поймает
        seen.add(n)
        out.append(t)
    out.sort(key=lambda t: (order.index(group_of(t["name"])) if group_of(t["name"]) else len(order), t["name"]))
    return out
