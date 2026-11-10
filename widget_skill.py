"""Self-contained Telegram message widgets; no server actions from widget code."""
import time
import uuid
import zlib
from html.parser import HTMLParser

SKILL_ID = "telegram_widgets"
SKILLS = [{
    "id": SKILL_ID, "title": "HTML-виджеты в сообщениях", "risk": "create", "default": True,
    "desc": "Формирует и отправляет интерактивный ответ прямо внутри сообщения: карточки, сравнения, "
            "диаграммы, калькуляторы и небольшие игры. По просьбе или по выбору ассистента. "
            "Только личный чат с ботом и Telegram с поддержкой виджетов. Нажатия и расчёты работают локально: "
            "виджет не выполняет действия в Джарвисе и не получает обновления с сервера. "
            "Если клиент не показывает виджет, кнопка под ним выдаёт HTML-копию для браузера.",
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
                   "Начальный HTML обязан содержать видимый текст или кнопку до запуска JS. "
                   "Успех означает приём Telegram, не подтверждение отрисовки клиентом. "
                   "При ошибке не утверждай, что отправлено: исправь HTML или ответь текстом.",
    "input_schema": {"type": "object", "properties": {
        "html": {"type": "string", "description": "Полный самодостаточный HTML, без markdown-ограждений."},
        "caption": {"type": "string", "description": "Содержание ответа обычным текстом, 1–1024 UTF-16 единиц."},
        "width": {"type": "integer", "minimum": 200, "maximum": 600},
        "height": {"type": "integer", "minimum": 50, "maximum": 600},
    }, "required": ["html", "caption"], "additionalProperties": False},
}]
TOOL_NAMES = {t["name"] for t in TOOLS}


class _InitialContent(HTMLParser):
    """Reject script-only/empty markup; client rendering still cannot be verified."""
    def __init__(self):
        super().__init__()
        self.ignored = []
        self.visible = False

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "title", "script", "style", "template"}:
            self.ignored.append(tag)
        elif not self.ignored and tag in {"button", "svg", "canvas", "img"}:
            self.visible = True

    def handle_endtag(self, tag):
        if tag in self.ignored:
            del self.ignored[self.ignored.index(tag):]

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.visible = True


def _remember_fallback(bot, key, ctx, source, caption, message_id):
    # Use the existing private bot state, already ignored by Git and backed up.
    with bot._state_lock:
        state = bot._load_state()
        widgets = state.setdefault("widgets", {})
        widgets[key] = {"chat_id": ctx.chat_id, "message_id": message_id,
                        "html": source, "caption": caption, "created": time.time()}
        for old in sorted(widgets, key=lambda k: widgets[k]["created"])[:-60]:
            del widgets[old]
        bot._save_state(state)


def handle_fallback(token, cq, state, bot):
    """An explicit click offers a local HTML copy for clients without widgets."""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    key = (cq.get("data") or "").removeprefix("twf:")
    with bot._state_lock:
        saved = bot._load_state().get("widgets", {}).get(key)
    allowed = (skill_on(state) and saved and chat_id and int(chat_id) > 0
               and str(saved["chat_id"]) == str(chat_id)
               and saved["message_id"] == msg.get("message_id")
               and str((cq.get("from") or {}).get("id")) == str(chat_id))
    bot.api(token, "answerCallbackQuery", {"callback_query_id": cq.get("id"),
            "text": "Отправляю HTML-копию" if allowed else "Виджет недоступен или навык выключен"})
    if not allowed:
        return
    # The source is never executed by Jarvis or served on its authenticated origin.
    theme = '<style>:root{--tg-theme-bg-color:#fff;--tg-theme-text-color:#111;--tg-theme-button-color:#2481cc;--tg-theme-button-text-color:#fff;--tg-theme-hint-color:#707579;--tg-theme-secondary-bg-color:#f1f1f4}body{font-family:system-ui;color:#111;background:#fff}</style>'
    doc = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + theme + saved["html"]
    bot.api(token, "sendDocument", {"chat_id": chat_id, "caption":
            "Если в сообщении видна только подпись, обновите Telegram до версии с поддержкой HTML-виджетов. "
            "Бот не может проверить версию или отрисовку клиента. "
            "Это HTML-копия: скачайте файл и откройте в браузере; локальные кнопки работают без Джарвиса."},
            files={"document": ("widget.html", doc.encode("utf-8"), "text/html")})


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
        "Начальный HTML должен содержать видимые текст и элементы управления, не пустой контейнер "
        "до выполнения JS. Не прячь body/display/opacity на время загрузки; не используй vh или "
        "абсолютное позиционирование для всего содержимого. Скрипт ставь после разметки. "
        "Кнопки могут переключать представление, раскрывать детали, считать и играть локально. "
        "allow_queries выключен: не вызывай TelegramWidget.request, не изображай сохранение данных, "
        "создание задач, платежи или обновление с сервера. Это снимок данных; локальные изменения "
        "не синхронизируются с Джарвисом. HTML после отправки неизменяем. "
        "Не вставляй токены/секреты; пользовательские данные экранируй в HTML и JS. "
        "Лимиты: 32768 байт UTF-8, 4096 байт после zlib-9; при ошибке размера упрости код и повтори. "
        "caption — обычный текст с основными выводами, без HTML, до 1024 UTF-16 единиц. "
        "Сейчас поддерживается только личный чат с ботом. При отказе Telegram ответь обычным текстом; "
        "не подменяй виджет файлом без просьбы. После успеха скажи «Виджет отправлен. Если видна только подпись, "
        "нажми „Не вижу виджет“ под сообщением». Не утверждай, что кнопка отображается или работает у пользователя: "
        "сервер не получает подтверждение отрисовки. Если пользователь сообщает о пропавшем виджете, "
        "объясни ограничение версии Telegram и предложи эту кнопку для HTML-копии в браузере, "
        "не обещай, что повторная отправка исправит клиент.\n\n"
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
        initial = _InitialContent()
        initial.feed(source)
        if not initial.visible:
            raise ValueError("добавь видимый текст и элементы управления в начальный HTML до запуска JavaScript")
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
        key = uuid.uuid4().hex[:16]
        payload["reply_markup"] = {"inline_keyboard": [[{
            "text": "Не вижу виджет — HTML-копия", "callback_data": "twf:" + key}]]}
        result = bot.api(ctx.token, "sendWidget", payload)
        if not isinstance(result, dict) or not result.get("message_id"):
            return {"error": "Telegram не подтвердил отправку виджета."}
        if str((result.get("chat") or {}).get("id")) != str(ctx.chat_id):
            return {"error": "Telegram не подтвердил отправку в текущий чат.", "retryable": False}
        fallback_ready = False
        try:
            _remember_fallback(bot, key, ctx, source, caption, result["message_id"])
            fallback_ready = True
        except OSError:
            # The message has already been sent; do not invite duplicate sends.
            print("  widget: unable to persist HTML fallback", flush=True)
        media = result.get("widget")
        if not isinstance(media, dict) or not isinstance(media.get("html"), str) or not media["html"].strip():
            print("  widget: sendWidget returned a message without widget media", flush=True)
            return {"error": "Telegram вернул сообщение без HTML-виджета. Не сообщай об успешной отправке "
                             "и не повторяй запрос автоматически: сообщение уже могло быть создано. "
                             + ("Для HTML-копии нажмите «Не вижу виджет» под сообщением." if fallback_ready else ""),
                    "message_id": result["message_id"], "retryable": False}
        ctx.last_message_id = result["message_id"]
        ctx.sent.append("HTML-виджет: " + caption)
        return {"ok": True, "message_id": result["message_id"], "caption": caption,
                "delivery": "telegram_accepted", "client_rendering": "unknown", "html_copy_available": fallback_ready,
                "note": "Telegram принял HTML-виджет; отображение клиентом не подтверждено. "
                        "Если видна только подпись, обновите Telegram или нажмите «Не вижу виджет» для HTML-копии."}
    except bot.TgApiError as exc:
        return {"error": f"Telegram отклонил виджет: {exc}. Не утверждай, что отправлено; ответь текстом или исправь виджет."}
    except (TypeError, ValueError, UnicodeError) as exc:
        return {"error": f"Неверные аргументы виджета: {exc}"}
