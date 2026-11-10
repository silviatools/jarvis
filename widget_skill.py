"""Self-contained Telegram message widgets; no server actions from widget code."""
import zlib

SKILL_ID = "telegram_widgets"
SKILLS = [{
    "id": SKILL_ID, "title": "HTML-виджеты в сообщениях", "risk": "create", "default": True,
    "desc": "Формирует и отправляет интерактивный ответ прямо внутри сообщения: карточки, сравнения, "
            "диаграммы, калькуляторы и небольшие игры. По просьбе или по выбору ассистента. "
            "Только личный чат с ботом и Telegram с поддержкой виджетов. Нажатия и расчёты работают локально: "
            "виджет не выполняет действия в Джарвисе и не получает обновления с сервера.",
    "tools": ["tg_send_widget"],
}]

TOOLS = [{
    "name": "tg_send_widget",
    "description": "Сформировать и отправить готовый HTML-виджет прямо в сообщение текущего личного чата. "
                   "Используй по просьбе о виджете или сам, когда карточка, сравнение, диаграмма, калькулятор "
                   "или небольшая игра полезнее текста. Сначала собери полный HTML с inline CSS/JavaScript, "
                   "затем передай его в html; это не файл и не Mini App. Без внешних ресурсов и сети, "
                   "без TelegramWidget.request и действий на сервере. HTML до 32768 байт и до 4096 байт zlib-9. "
                   "caption — обязательное краткое текстовое содержание ответа для поиска и старых клиентов. "
                   "При ошибке не утверждай, что отправлено: исправь HTML или ответь текстом.",
    "input_schema": {"type": "object", "properties": {
        "html": {"type": "string", "description": "Полный самодостаточный HTML, без markdown-ограждений."},
        "caption": {"type": "string", "description": "Содержание ответа обычным текстом, 1–1024 UTF-16 единиц."},
        "width": {"type": "integer", "minimum": 200, "maximum": 600},
        "height": {"type": "integer", "minimum": 50, "maximum": 600},
    }, "required": ["html", "caption"], "additionalProperties": False},
}]
TOOL_NAMES = {t["name"] for t in TOOLS}


def skill_on(state=None):
    return (state or {}).get(SKILL_ID, True) is not False


def prompt_section(state=None):
    if not skill_on(state):
        return ""
    return (
        "\nHTML-ВИДЖЕТЫ: tg_send_widget отправляет ответ как интерактивный блок внутри сообщения Telegram. "
        "Если пользователь просит виджет — обязательно сформируй и отправь его, если задача укладывается в ограничения. "
        "Можешь выбрать виджет сам для наглядного сравнения, диаграммы, карточки, калькулятора или небольшой игры; "
        "для простого ответа достаточно текста. Явная просьба о виджете имеет приоритет над обычным оформлением "
        "списков и таблиц. Сначала получи нужные данные инструментами, затем создай законченный HTML/CSS/JS "
        "и вызови tg_send_widget. Код идёт только в аргумент html, не в текст ответа и не в HTML-файл. "
        "В отличие от обычных Telegram-ответов здесь допустим HTML с inline <style> и <script>. "
        "Telegram рисует ограниченный DOM в QuickJS, не браузере: никаких внешних библиотек, шрифтов, "
        "картинок по URL, fetch, WebSocket, iframe или Telegram.WebApp. Изображения — inline SVG/data:. "
        "Используй document.getElementById, textContent, onclick и кнопки; CSS flex/grid, "
        "var(--tg-theme-text-color), var(--tg-theme-bg-color), var(--tg-theme-button-color). "
        "Виджет не имеет клавиатуры, input/select/textarea, внутренней прокрутки и fullscreen; "
        "сделай компактный адаптивный макет для ширины 320px и высоты до 600px. "
        "Кнопки могут переключать представление, раскрывать детали, считать и играть локально. "
        "allow_queries выключен: не вызывай TelegramWidget.request, не изображай сохранение данных, "
        "создание задач, платежи или обновление с сервера. Это снимок данных; локальные изменения "
        "не синхронизируются с Джарвисом. HTML после отправки неизменяем. "
        "Не вставляй токены/секреты; пользовательские данные экранируй в HTML и JS. "
        "Лимиты: 32768 байт UTF-8, 4096 байт после zlib-9; при ошибке размера упрости код и повтори. "
        "caption — обычный текст с основными выводами, без HTML, до 1024 UTF-16 единиц. "
        "Сейчас поддерживается только личный чат с ботом. При отказе Telegram ответь обычным текстом; "
        "не подменяй виджет файлом без просьбы. После успеха не дублируй содержание, достаточно короткого подтверждения.\n\n"
    )


def execute(name, inp, state, ctx, bot):
    if not skill_on(state):
        return {"error": "Навык HTML-виджетов выключен в настройках."}
    if name not in TOOL_NAMES:
        return {"error": "Неизвестный инструмент виджета."}
    if ctx is None or bot is None:
        return {"error": "Виджет доступен только в личном Telegram-чате с ботом."}
    try:
        if int(ctx.chat_id) <= 0:
            return {"error": "Виджеты пока доступны только в личном чате с ботом, не в группах и каналах."}
        if not isinstance(inp, dict) or set(inp) - {"html", "caption", "width", "height"}:
            raise ValueError("допустимы только html, caption, width, height; адресат — текущий чат")
        source, caption = inp.get("html"), inp.get("caption")
        if not isinstance(source, str) or not source.strip() or "\x00" in source or source.lstrip().startswith("```"):
            raise ValueError("html должен содержать готовый HTML без NUL и markdown-ограждений")
        raw = source.encode("utf-8")
        packed = len(zlib.compress(raw, 9))
        if len(raw) > 32768 or packed > 4096:
            return {"error": "Упрости HTML/CSS/JS и повтори: превышен размер виджета.",
                    "html_bytes": len(raw), "compressed_bytes": packed,
                    "max_html_bytes": 32768, "max_compressed_bytes": 4096}
        if not isinstance(caption, str) or not caption.strip() or "\x00" in caption or len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("caption — непустой обычный текст до 1024 UTF-16 единиц без NUL")
        payload = {"chat_id": ctx.chat_id, "html": source, "caption": caption,
                   "allow_queries": False, "is_shared": False}
        for key, minimum in (("width", 200), ("height", 50)):
            if key in inp:
                value = inp[key]
                if type(value) is not int or not minimum <= value <= 600:
                    raise ValueError(f"{key} — целое число от {minimum} до 600, либо не указывай")
                payload[key] = value
        result = bot.api(ctx.token, "sendWidget", payload)
        if not isinstance(result, dict) or not result.get("message_id"):
            return {"error": "Telegram не подтвердил отправку виджета."}
        ctx.last_message_id = result["message_id"]
        ctx.sent.append("HTML-виджет: " + caption)
        return {"ok": True, "message_id": result["message_id"], "caption": caption}
    except bot.TgApiError as exc:
        return {"error": f"Telegram отклонил виджет: {exc}. Не утверждай, что отправлено; ответь текстом или исправь виджет."}
    except (TypeError, ValueError, UnicodeError) as exc:
        return {"error": f"Неверные аргументы виджета: {exc}"}
