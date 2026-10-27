"""
market_skill.py — навык «Маркетплейсы»: кабинеты покупателя Ozon и Wildberries.

Сессия браузера (cookies / storage state) и мобильный HTTP(S)-прокси лежат в
DATA_DIR/jarvis_market.json. Пароль прокси и куки в логи и в ответы API не попадают.
Вход — один Chromium на процесс, профиль Pixel 7 (Android, 412×915, тач, ru-RU,
Europe/Moscow, Client Hints). Без сохранённого прокси вход не начинается:
с IP сервера кабинет не открывается. Прокси один и тот же на все запросы —
ротацию IP не делаем, смена прокси сбрасывает сессии.

Чтение кабинета и редкая отмена заказа идут тем же браузером и той же сессией.
Публичные карточки товаров не открываем. Если сессия мертва, статус «слетела»,
инструмент прямо говорит, что нужен повторный вход: у Ozon код из СМС,
у Wildberries — уведомление в приложении.
"""

import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

try:
    import requests
except ImportError:
    requests = None

SKILL_ID = "marketplaces"
STORE_NAME = "jarvis_market.json"
SHOPS = ("ozon", "wb")
SHOP_TITLE = {"ozon": "Ozon", "wb": "Wildberries"}
STATUS_LABEL = {
    "none": "Нет сессии",
    "awaiting_code": "Ждём код",
    "active": "Активна",
    "expired": "Слетела",
    "blocked": "Заблокировано антиботом",
}

SKILLS = [
    {"id": SKILL_ID, "title": "Маркетплейсы", "risk": "write", "default": True,
     "desc": (
         "Личные кабинеты покупателя Ozon и Wildberries. У Wildberries это разные страницы: "
         "market_orders — покупки (уже полученные), market_delivery — что в пути, market_returns — возвраты. "
         "Ещё карточка заказа, избранное и краткий профиль (телефон маской). Не кабинет продавца и не цены с витрины. "
         "Нужен сохранённый мобильный HTTP(S)-прокси с липким IP (Настройки → Маркетплейсы) и живая сессия: "
         "без прокси кабинет с сервера не открывается, смена IP сессию сбрасывает. Если сессия слетела, "
         "инструменты так и скажут — нужен новый вход: у Ozon код из СМС, у Wildberries уведомление в приложении. Если антибот не пускает, статус "
         "«Заблокировано антиботом»: это не «сессия слетела». Отмена заказа, если кнопка есть на странице, "
         "только после вашего подтверждения."
     ),
     "tools": [
         "market_orders", "market_order", "market_delivery", "market_returns",
         "market_favorites", "market_profile", "market_cancel_order",
     ]},
]

_STR = {"type": "string"}
_SHOP = {"type": "string", "enum": ["ozon", "wb"], "description": "ozon или wb"}

TOOLS = [
    {"name": "market_orders",
     "description": "Покупки в кабинете покупателя. Для Wildberries это уже полученные заказы, не то, что сейчас в пути (для пути — market_delivery). Нужны прокси и живая сессия.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP}, "required": ["shop"]}},
    {"name": "market_order",
     "description": "Карточка одного заказа в кабинете покупателя. order_id — номер заказа со страницы заказов.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP, "order_id": _STR}, "required": ["shop", "order_id"]}},
    {"name": "market_delivery",
     "description": "Доставки в пути: статус, дата и ПВЗ. Для Wildberries это не список покупок и не возвраты. Можно сузить до order_id.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP, "order_id": _STR}, "required": ["shop"]}},
    {"name": "market_returns",
     "description": "Возвраты в личном кабинете покупателя. Для Wildberries отдельная страница, не доставки и не покупки.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP}, "required": ["shop"]}},
    {"name": "market_favorites",
     "description": "Избранное в личном кабинете покупателя. Не поиск по каталогу.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP}, "required": ["shop"]}},
    {"name": "market_profile",
     "description": "Краткий профиль кабинета покупателя. Телефон в ответе уже замаскирован.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP}, "required": ["shop"]}},
    {"name": "market_cancel_order",
     "description": (
         "Отменить заказ в кабинете покупателя, только если на странице заказа есть такая кнопка. "
         "Меняет кабинет: без confirmed:true ничего не нажимает и вернёт needs_confirmation. "
         "Повтор — только после явного согласия пользователя следующим сообщением."
     ),
     "input_schema": {"type": "object", "properties": {
         "shop": _SHOP, "order_id": _STR,
         "confirmed": {"type": "boolean", "description": "true только после явного согласия пользователя"},
     }, "required": ["shop", "order_id"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_missing = [t for s in SKILLS for t in s["tools"] if t not in TOOL_NAMES]
if _missing:
    raise RuntimeError("market skills catalog tools missing: " + ", ".join(_missing))

PROMPT = (
    "МАРКЕТПЛЕЙСЫ — личные кабинеты покупателя Ozon и Wildberries (не продавец и не карточки товаров). "
    "Инструменты: market_orders (покупки, у WB уже полученные), market_delivery (что в пути, статус и ПВЗ), "
    "market_returns (возвраты), market_order (один заказ), market_favorites, market_profile "
    "(чтение) и market_cancel_order (меняет кабинет). У WB это три разные страницы, не подменяй одну другой. "
    "shop — ozon или wb. Читать можно без спроса. "
    "Если в ответе есть строки «Заказ …», перескажи номер, товар, цену, статус, дату и ПВЗ оттуда. "
    "Если строк нет — так и скажи, заказы не выдумывай. "
    "Отмена заказа — только после явного «да»: первый вызов без confirmed вернёт needs_confirmation, опиши заказ и спроси, "
    "повтор с confirmed:true только следующим сообщением. Если на странице нет кнопки отмены или страница не подтвердила отмену — "
    "так и скажи, не говори, что заказ отменён. Если инструмент вернул, что нет прокси, нет сессии или сессия слетела — "
    "так и скажи, не выдумывай заказы. Если кабинет не открылся или сессия не активна — так и скажи "
    "и направь в Настройки → Маркетплейсы. Если ответ «заблокировано антиботом» — так и скажи, "
    "это не слетевшая сессия. Не советуй выключать VPN: это текст страницы магазина, не наш шаг. "
    "Новый вход — в Настройки → Маркетплейсы: у Ozon код из СМС, "
    "у Wildberries код в приложении (уведомление или колокольчик). "
    "Телефон в профиле уже замаскирован, не восстанавливай его.\n\n"
)

# Профиль телефона для Ozon: экран и вьюпорт 412×915. Модель Pixel 7 — в Client Hints, не в строке UA.
PIXEL7_PROFILE = {
    "viewport": {"width": 412, "height": 915},
    "screen": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
}
DESKTOP_PROFILE = {
    "viewport": {"width": 1366, "height": 900},
    "screen": {"width": 1366, "height": 900},
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
    "is_mobile": False,
    "has_touch": False,
}
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]
_WEBDRIVER_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});"
    "Object.defineProperty(navigator, 'language', {get: () => 'ru-RU'});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});"
)

_SECTION = {
    "market_orders": "orders",
    "market_order": "order",
    "market_delivery": "delivery",
    "market_returns": "returns",
    "market_favorites": "favorites",
    "market_profile": "profile",
}

_CABINET_URLS = {
    "ozon": {
        "login": "https://www.ozon.ru/my/orderlist",
        "orders": "https://www.ozon.ru/my/orderlist",
        "order": "https://www.ozon.ru/my/orderdetails/?order={id}",
        "delivery": "https://www.ozon.ru/my/orderlist",
        "returns": "https://www.ozon.ru/my/returns",
        "favorites": "https://www.ozon.ru/my/favorites",
        "profile": "https://www.ozon.ru/my/settings",
    },
    "wb": {
        "login": "https://www.wildberries.ru/security/login?returnUrl=https%3A%2F%2Fwww.wildberries.ru%2Flk%2Fmyorders%2Fdelivery",
        "orders": "https://www.wildberries.ru/lk/myorders/archive",
        "order": "https://www.wildberries.ru/lk/myorders/archive",
        "delivery": "https://www.wildberries.ru/lk/myorders/delivery",
        "returns": "https://www.wildberries.ru/lk/myrefunds",
        "favorites": "https://www.wildberries.ru/lk/favorites",
        "profile": "https://www.wildberries.ru/lk/details",
    },
}

_IP_URLS = ("https://api.ipify.org/?format=json", "https://ifconfig.me/ip")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s()]{8,}\d)")
_ORDER_RE = re.compile(r"^[A-Za-z0-9\-]{4,64}$")
_CANCELLED_RE = re.compile(r"заказ\s+отмен[её]н|отмена\s+оформлена|статус[:\s]+отмен", re.I)
_NOT_FOUND_RE = re.compile(r"номер\s+не\s+найден|телефон\s+не\s+найден|пользователь\s+не\s+найден", re.I)
_PLACEHOLDER_INTL = "79991234567"
_OFFLINE_MARKERS = (
    "нет соединения",
    "выключите vpn",
    "выключить vpn",
    "отключите vpn",
    "сменить сеть",
    "смените сеть",
    "проверьте соединение",
    "проверьте подключение",
    "нет подключения к интернету",
)
_CHALLENGE_MARKERS = (
    "antibot",
    "abt-challenge",
    "challenge page",
    "не робот",
    "variti",
    "captcha",
    "капча",
    "доступ ограничен",
    "проверка браузера",
    "подтвердите, что вы человек",
)
_CODE_MARKERS = (
    "введите код",
    "код из смс",
    "код из sms",
    "код из уведомления",
    "отправили пуш",
    "уведомление в колокольчик",
    "код подтверждения",
    "код из приложения",
    "код отправлен",
    "мы отправили код",
)
_LOGIN_TEXT = (
    "введите номер телефона",
    "войти или зарегистрироваться",
    "войдите, чтобы",
    "войдите с wb id",
    "войти в wb.id",
)
OFFLINE_ERROR = (
    "Кабинет не открылся, сессия не активна. Войти заново: Настройки → Маркетплейсы."
)
BLOCKED_ERROR = (
    "Заблокировано антиботом. Кабинет не открылся. Войти заново: Настройки → Маркетплейсы."
)
_JOURNAL_MAX = 40
_SHOT_MAX = 24
_SHOT_RE = re.compile(r"^m[0-9a-f]{16}\.jpg$")
# Прогрев Ozon: главная, перезагрузки на 10-й и 18-й секунде, до 25 с, затем 3 с на cookies.
OZON_HOME = "https://www.ozon.ru/"
OZON_MODES = ("android", "desktop", "patchright")
ABT_MODES = OZON_MODES + ("wb",)
OZON_WARM_LIMIT = 25
OZON_WARM_RELOADS = (10, 18)
OZON_WARM_SETTLE = 3
WB_WAIT_S = 40
WB_HOME = "https://www.wildberries.ru/"
# Оболочка главной со спиннером — отдельно от 40 с проверки на странице входа.
WB_SHELL_S = 25
WB_COOLDOWN_S = 300
WB_CHECK_STREAK = 2
# Чтение заказов WB: ждём список, не networkidle и не справочник ПВЗ.
# Оболочка со спиннером на мобильном прокси не укладывается в 12 с.
WB_READ_S = 40
HEAVY_TYPES = frozenset({"image", "media", "font"})
# Только картинки, видео и шрифты. script, xhr, document и css в перехват не попадают.
HEAVY_GLOBS = "**/*.{png,jpg,jpeg,gif,webp,avif,svg,ico,bmp,mp4,webm,mp3,ogg,woff,woff2,ttf,otf,eot}*"
_READ_TRACE = frozenset({"orders", "order", "delivery", "returns"})
# Страница кода WB остаётся открытой, пока человек не введёт код или не выйдут 5 минут.
CODE_WAIT_S = 300
# После ввода кода страница может уйти на согласие OAuth не сразу. Это не отказ кода.
CODE_AFTER_S = 20
CONSENT_REDIRECT_S = 25
WB_CODE_HINT = "Код в приложении Wildberries (уведомление или колокольчик)"
_AUTH_OK = ("code", "password", "manual")
# Ручной экран WB. Кадры в память, на диск не пишем. 10 минут без действий — сеанс закрыт.
MANUAL_IDLE_S = 600
_MANUAL_KEYS = {"enter": "Enter", "backspace": "Backspace"}
_BOOT = secrets.token_hex(8)
_TIMER_RE = re.compile(r"\d{1,2}:\d{2}")
_lock = threading.Lock()
_driver = None
_logs = []
_manual_pub = {"open": False, "shop": ""}


def skill_on(state) -> bool:
    v = (state or {}).get(SKILL_ID)
    return True if v is None else bool(v)


def prompt_section(state=None) -> str:
    return PROMPT if skill_on(state) else ""


def voice_payload(state) -> dict:
    """Те же инструменты и тот же текст, что у циклов ассистента. Пусто, если навык выключен."""
    if not skill_on(state):
        return {"tools": [], "prompt": ""}
    return {"tools": TOOLS, "prompt": prompt_section(state)}


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))


def store_path() -> Path:
    return data_dir() / STORE_NAME


def _log(msg: str):
    line = "market: " + str(msg)[:300]
    _logs.append(line)
    print(line, flush=True)


def redact(text, proxy=None, extra=()) -> str:
    out = str(text or "")
    secrets = [s for s in extra if s]
    if proxy:
        for key in ("password", "username"):
            val = proxy.get(key) or ""
            if val:
                secrets.append(val)
                secrets.append(quote(val, safe=""))
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out[:400]


def build_hints(version: str) -> dict:
    """Client Hints как у Chrome на Pixel 7. Версию берём у установленного Chromium, модель — Pixel 7."""
    version = (version or "").strip()
    if not re.match(r"^\d+\.\d+", version):
        version = "131.0.0.0"
    major = version.split(".")[0]
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not_A Brand", "version": "24"},
    ]
    full = [
        {"brand": "Chromium", "version": version},
        {"brand": "Google Chrome", "version": version},
        {"brand": "Not_A Brand", "version": "24.0.0.0"},
    ]
    sec = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brands)
    sec_full = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in full)
    ua = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Mobile Safari/537.36"
    )
    metadata = {
        "brands": brands,
        "fullVersionList": full,
        "platform": "Android",
        "platformVersion": "14.0.0",
        "architecture": "",
        "model": "Pixel 7",
        "mobile": True,
        "bitness": "",
        "wow64": False,
    }
    headers = {
        "sec-ch-ua": sec,
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua-platform-version": '"14.0.0"',
        "sec-ch-ua-model": '"Pixel 7"',
        "sec-ch-ua-full-version-list": sec_full,
        "accept-language": "ru-RU,ru;q=0.9",
    }
    return {"user_agent": ua, "metadata": metadata, "headers": headers, "version": version}


def pixel7_user_agent(version: str) -> str:
    return build_hints(version)["user_agent"]


def _chrome_version(version: str) -> str:
    version = (version or "").strip()
    if not re.match(r"^\d+\.\d+", version):
        return "140.0.0.0"
    return version


def ozon_hints(version: str, mode: str = "android") -> dict:
    """UA и Client Hints Ozon. В строке Chrome/{major}.0.0.0, в metadata — версия браузера."""
    version = _chrome_version(version)
    major = version.split(".")[0]
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Not_A Brand", "version": "8"},
    ]
    full = [
        {"brand": "Chromium", "version": version},
        {"brand": "Not_A Brand", "version": "8.0.0.0"},
    ]
    sec = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brands)
    sec_full = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in full)
    mobile = mode == "android"
    if mobile:
        ua = (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36"
        )
        metadata = {
            "brands": brands,
            "fullVersionList": full,
            "fullVersion": version,
            "platform": "Android",
            "platformVersion": "14.0.0",
            "architecture": "",
            "model": "Pixel 7",
            "mobile": True,
            "bitness": "",
            "wow64": False,
        }
        headers = {
            "sec-ch-ua": sec,
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-platform-version": '"14.0.0"',
            "sec-ch-ua-model": '"Pixel 7"',
            "sec-ch-ua-full-version-list": sec_full,
            "accept-language": "ru-RU,ru;q=0.9",
        }
    else:
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )
        metadata = {
            "brands": brands,
            "fullVersionList": full,
            "fullVersion": version,
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        }
        headers = {
            "sec-ch-ua": sec,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-ch-ua-full-version-list": sec_full,
            "accept-language": "ru-RU,ru;q=0.9",
        }
    return {"user_agent": ua, "metadata": metadata, "headers": headers, "version": version}


def ozon_cdp(version: str, mode: str = "android") -> dict:
    hints = ozon_hints(version, mode)
    platform = "Linux armv81" if mode == "android" else "Linux x86_64"
    return {
        "userAgent": hints["user_agent"],
        "acceptLanguage": "ru-RU,ru;q=0.9",
        "platform": platform,
        "userAgentMetadata": hints["metadata"],
    }


_OZON_UA_HEADERS = (
    "user-agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-model",
    "sec-ch-ua-full-version-list",
)
_OZON_FETCH_HEADERS = (
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
)


def _header_map(headers) -> dict:
    try:
        pairs = dict(headers or {}).items()
    except Exception:
        return {}
    return {str(key).lower(): str(value) for key, value in pairs if value}


def ozon_header_bits(headers, keys) -> str:
    data = _header_map(headers)
    return " ".join(key + "=" + data[key] for key in keys if data.get(key))[:400]


def ozon_ua_signature(headers) -> str:
    return ozon_header_bits(headers, _OZON_UA_HEADERS)


def ozon_fetch_signature(headers) -> str:
    return ozon_header_bits(headers, _OZON_FETCH_HEADERS)


def ozon_same_session_error(exc) -> bool:
    """Фрейм того же процесса: своей CDP-сессии нет, UA страницы менять нельзя."""
    text = str(exc or "").lower()
    return "separate cdp session" in text or "part of the parent" in text


def ozon_login_top(url) -> str:
    """Адрес входа без query. Фрейм /ozonid-lite открываем как страницу www.ozon.ru."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not host.endswith("ozon.ru"):
        return ""
    low = path.lower()
    if not low.startswith("/ozonid"):
        return ""
    if host == "www.ozon.ru" or low.startswith("/ozonid-lite"):
        return "https://www.ozon.ru" + path
    return "https://" + host + path


def hide_webdriver(shop: str, mode: str) -> bool:
    """Подмена navigator.webdriver для Ozon не используется. У WB на телефоне остаётся."""
    return shop != "ozon" and mode == "android"


def chromium_launch_kwargs(proxy=None, executable: str = "") -> dict:
    """Полный Chromium. Свой бинарник — без channel. Прокси на launch, не на контекст."""
    kwargs = {"headless": True, "args": list(LAUNCH_ARGS)}
    exe = (executable or "").strip()
    if exe:
        kwargs["executable_path"] = exe
    else:
        kwargs["channel"] = "chromium"
    if proxy:
        kwargs["proxy"] = _pw_proxy(proxy)
    return kwargs


def ozon_mode_order(remembered: str = "") -> tuple:
    order = []
    if remembered in OZON_MODES:
        order.append(remembered)
    for mode in OZON_MODES:
        if mode not in order:
            order.append(mode)
    return tuple(order)


def ozon_cookie_state(storage) -> dict:
    cookies = []
    raw = storage.get("cookies") if isinstance(storage, dict) else None
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("domain") or "").lstrip(".").lower()
        if host == "ozon.ru" or host.endswith(".ozon.ru"):
            cookies.append(item)
    return {"cookies": cookies, "origins": []}


def mask_proxy(proxy) -> str:
    if not proxy or not proxy.get("host"):
        return ""
    base = f"{proxy.get('scheme') or 'http'}://{proxy['host']}:{proxy['port']}"
    login = proxy.get("username") or ""
    if login:
        return f"{base} · {login[:3]}"
    return base


def mask_phone_value(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return ""
    return digits[:2] + "…" + digits[-2:]


def mask_phones(text: str) -> str:
    def repl(m):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 10 or len(digits) > 15:
            return m.group(0)
        return digits[:2] + "…" + digits[-2:]
    return _PHONE_RE.sub(repl, text or "")


def _phone_digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def is_placeholder_phone(raw: str) -> bool:
    digits = _phone_digits(raw)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits == _PLACEHOLDER_INTL


def prepare_phone(shop: str, raw: str) -> dict:
    """Номер для формы. Плейсхолдер +79991234567 не сохраняем и никуда не отправляем.

    WB рисует +7 отдельно и ждёт 10 цифр. Если вписать 11 цифр с семёркой,
    маска обрежет номер, и сайт ответит «не найден» на другой телефон.
    Ozon принимает +7 и те же 10 цифр одним полем.
    """
    if is_placeholder_phone(raw):
        return {"ok": False, "error": "Это пример из поля, не номер. Введите свой телефон."}
    digits = _phone_digits(raw)
    if len(digits) == 11 and digits[0] in "78":
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        return {"ok": False, "error": "Введите номер телефона"}
    if len(national) != 10 or not national.startswith("9"):
        return {"ok": False, "error": "Нужен мобильный номер: +7 и 10 цифр, начиная с 9."}
    intl = "7" + national
    form = national if shop == "wb" else "+" + intl
    return {"ok": True, "form": form, "intl": intl, "national": national}


def phone_landed(national: str, actual: str) -> bool:
    got = _phone_digits(actual)
    if not got or not national:
        return False
    return got == national or got.endswith(national) or got in ("7" + national, "8" + national)


def parse_proxy(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Введите прокси")
    if any(c in text for c in "\n\r\t "):
        raise ValueError("Прокси должен быть одной строкой без пробелов")
    if "://" not in text and "@" not in text:
        parts = text.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, user = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            text = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            text = "http://" + text
    elif "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Нужен HTTP или HTTPS прокси")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Укажите хост и порт прокси")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": int(parsed.port),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def parse_ip_body(raw: str) -> str:
    text = (raw or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("ip"):
            text = str(data["ip"]).strip()
    except Exception:
        pass
    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    if m:
        return m.group(0)
    m = re.search(r"\b(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}\b", text, re.I)
    return m.group(0) if m else ""


def fetch_ip_direct() -> str:
    """IP самого сервера, без прокси и без переменных HTTP_PROXY."""
    if requests is None:
        raise RuntimeError("нет библиотеки requests")
    sess = requests.Session()
    sess.trust_env = False
    last = "нет ответа"
    for url in _IP_URLS:
        try:
            resp = sess.get(url, timeout=15, proxies={"http": None, "https": None})
            if resp.ok:
                ip = parse_ip_body(resp.text)
                if ip:
                    return ip
            last = f"http {resp.status_code}"
        except Exception as e:
            last = type(e).__name__
    raise RuntimeError(last)


def _same_proxy(a, b) -> bool:
    if not a or not b:
        return False
    keys = ("scheme", "host", "port", "username", "password")
    return all(a.get(k) == b.get(k) for k in keys)


def _empty() -> dict:
    return {
        "proxy": None,
        "journal": [],
        "abt": {},
        "ozon_browser_mode": "",
        "wb_check": {"fails": 0, "until": 0},
        "shops": {sid: {"status": "none", "auth": "", "phone_mask": "", "updated": 0, "storage": None} for sid in SHOPS},
    }


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    base = _empty()
    proxy = data.get("proxy")
    if isinstance(proxy, dict) and proxy.get("host") and proxy.get("port"):
        base["proxy"] = {
            "scheme": proxy.get("scheme") if proxy.get("scheme") in ("http", "https") else "http",
            "host": str(proxy.get("host")),
            "port": int(proxy.get("port")),
            "username": str(proxy.get("username") or ""),
            "password": str(proxy.get("password") or ""),
        }
    shops = data.get("shops") if isinstance(data.get("shops"), dict) else {}
    for sid in SHOPS:
        raw = shops.get(sid) if isinstance(shops.get(sid), dict) else {}
        status = raw.get("status") if raw.get("status") in STATUS_LABEL else "none"
        storage = raw.get("storage") if isinstance(raw.get("storage"), dict) else None
        auth = raw.get("auth") if raw.get("auth") in _AUTH_OK else ""
        base["shops"][sid] = {
            "status": status,
            "auth": auth,
            "phone_mask": str(raw.get("phone_mask") or "")[:16],
            "updated": int(raw.get("updated") or 0),
            "storage": storage,
            "boot": str(raw.get("boot") or ""),
        }
    base["journal"] = _clean_journal(data.get("journal"))
    base["abt"] = _clean_abt(data.get("abt"))
    mode = data.get("ozon_browser_mode")
    base["ozon_browser_mode"] = mode if mode in OZON_MODES else ""
    base["wb_check"] = _clean_wb_check(data.get("wb_check"))
    return base


def _clean_abt(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, modes in raw.items():
        if not re.fullmatch(r"[0-9a-f]{16}", str(key)) or not isinstance(modes, dict):
            continue
        kept = {}
        for mode, storage in modes.items():
            if mode in ABT_MODES and isinstance(storage, dict):
                kept[mode] = storage
        if kept:
            out[str(key)] = kept
    return out


def _clean_wb_check(raw) -> dict:
    fails = 0
    until = 0
    if isinstance(raw, dict):
        try:
            fails = int(raw.get("fails") or 0)
        except (TypeError, ValueError):
            fails = 0
        try:
            until = int(raw.get("until") or 0)
        except (TypeError, ValueError):
            until = 0
    return {"fails": max(0, fails), "until": max(0, until)}


def load_abt(proxy, mode):
    if mode not in ABT_MODES:
        return None
    row = (_load().get("abt") or {}).get(proxy_hash(proxy)) or {}
    storage = row.get(mode)
    return storage if isinstance(storage, dict) else None


def save_abt(proxy, mode, storage):
    if mode not in ABT_MODES or not isinstance(storage, dict):
        return
    with _lock:
        data = _load()
        key = proxy_hash(proxy)
        data.setdefault("abt", {})
        data["abt"].setdefault(key, {})
        data["abt"][key][mode] = storage
        _write(data)


def _write(data: dict):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _shop_row(data, shop):
    return data["shops"][shop]


def _set_shop(shop, status, phone_mask=None, storage="__keep__", auth="__keep__", why=""):
    with _lock:
        data = _load()
        row = _shop_row(data, shop)
        prev = row.get("status") or "none"
        new_status = status if status in STATUS_LABEL else "none"
        new_auth = row.get("auth") if auth == "__keep__" else auth
        if new_auth not in _AUTH_OK:
            new_auth = ""
        if new_status == "active" and new_auth not in _AUTH_OK:
            new_status = "expired"
            storage = None
            why = why or "Активна без кода и пароля"
        if new_status != "active":
            new_auth = ""
        row["status"] = new_status
        row["auth"] = new_auth
        if phone_mask is not None:
            row["phone_mask"] = phone_mask
        if storage != "__keep__":
            row["storage"] = storage
        row["updated"] = int(time.time())
        row["boot"] = _BOOT if new_status == "awaiting_code" else ""
        if new_status != prev or why:
            _append_journal(data, shop, "status", "", why or new_status, new_status)
        _write(data)


def _proxy():
    return _load().get("proxy")


def _load_live() -> dict:
    """Читает файл, снимает «Активна» без кода и «ждём код» после рестарта процесса."""
    with _lock:
        data = _load()
        changed = _heal_lost_code(data)
        changed = _heal_unproved(data) or changed
        if changed:
            _write(data)
        return data


def _storage(shop):
    return (_load_live()["shops"][shop] or {}).get("storage")


def _status(shop) -> str:
    return (_load_live()["shops"][shop] or {}).get("status") or "none"


def storage_usable(storage) -> bool:
    if not isinstance(storage, dict):
        return False
    if storage.get("cookies"):
        return True
    for origin in storage.get("origins") or []:
        if isinstance(origin, dict) and origin.get("localStorage"):
            return True
    return False


def public_status() -> dict:
    data = _load_live()
    proxy = data.get("proxy")
    shops = {}
    for sid in SHOPS:
        row = data["shops"][sid]
        status = row.get("status") or "none"
        shops[sid] = {
            "id": sid,
            "title": SHOP_TITLE[sid],
            "status": status,
            "label": STATUS_LABEL.get(status, STATUS_LABEL["none"]),
            "phone_mask": row.get("phone_mask") or "",
        }
        if sid == "wb":
            left = wb_cooldown_remaining()
            if left > 0:
                shops[sid]["notice"] = wb_retry_message(left)
    journal = list(reversed(data.get("journal") or []))[:30]
    return {
        "ok": True,
        "has_proxy": bool(proxy and proxy.get("host")),
        "proxy": mask_proxy(proxy),
        "shops": shops,
        "journal": journal,
        "manual": {"open": manual_is_open(), "shop": manual_shop()},
    }


def _known_shop(shop) -> str:
    sid = (shop or "").strip().lower()
    return sid if sid in SHOPS else ""


def save_proxy(raw: str) -> dict:
    try:
        parsed = parse_proxy(raw)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    old = _proxy()
    changed = not _same_proxy(old, parsed)
    reset = False
    with _lock:
        data = _load()
        data["proxy"] = parsed
        if changed:
            data["ozon_browser_mode"] = ""
            for sid in SHOPS:
                if data["shops"][sid].get("status") != "none" or data["shops"][sid].get("storage"):
                    reset = True
                data["shops"][sid]["status"] = "none"
                data["shops"][sid]["auth"] = ""
                data["shops"][sid]["storage"] = None
                data["shops"][sid]["updated"] = int(time.time())
        _write(data)
    if changed:
        for sid in SHOPS:
            try:
                get_driver().close_login(sid)
            except Exception as e:
                _log("close login " + sid + " " + redact(e, parsed))
    _log("proxy saved " + mask_proxy(parsed) + (" reset" if reset else ""))
    message = "Прокси сохранён."
    if reset:
        message = "Прокси сохранён. Сессии сброшены: другой IP обрывает вход, войдите заново и введите код."
    return {"ok": True, "mask": mask_proxy(parsed), "message": message}


def proxy_connection(proxy) -> str:
    proxy = proxy or {}
    scheme = proxy.get("scheme") or "http"
    host = proxy.get("host") or ""
    port = proxy.get("port") or ""
    user = proxy.get("username") or ""
    password = proxy.get("password") or ""
    auth = ""
    if user or password:
        auth = quote(str(user), safe="") + ":" + quote(str(password), safe="") + "@"
    return f"{scheme}://{auth}{host}:{port}"


def proxy_hash(proxy) -> str:
    return hashlib.sha256(proxy_connection(proxy).encode()).hexdigest()[:16]


def load_ozon_mode() -> str:
    mode = (_load().get("ozon_browser_mode") or "")
    return mode if mode in OZON_MODES else ""


def remember_ozon_mode(mode: str):
    if mode not in OZON_MODES:
        return
    with _lock:
        data = _load()
        data["ozon_browser_mode"] = mode
        _write(data)


def forget_abt(proxy, mode):
    if mode not in ABT_MODES:
        return
    with _lock:
        data = _load()
        key = proxy_hash(proxy)
        row = (data.get("abt") or {}).get(key)
        if not isinstance(row, dict) or mode not in row:
            return
        row.pop(mode, None)
        if row:
            data["abt"][key] = row
        else:
            data.get("abt", {}).pop(key, None)
        _write(data)


def describe_exit(fields) -> dict:
    """Мобильный выход или нет. Broadband/Corbina важнее общей подписи маршрута «Mobile Region»."""
    fields = fields or {}
    org = str(fields.get("org") or "").strip()
    asn = str(fields.get("asn") or "").strip()
    netname = str(fields.get("netname") or "").strip()
    descr = str(fields.get("descr") or "").strip()
    host = str(fields.get("hostname") or "").strip()
    assign = _norm_text(" ".join((netname, descr, host)))
    if any(s in assign for s in ("broadband", "corbina")):
        mobile = False
    elif any(s in assign for s in ("mobile", "gprs", "apn")):
        mobile = True
    else:
        mobile = False
    who = " ".join(x for x in (org, asn) if x).strip() or "неизвестен"
    warning = ""
    if not mobile:
        where = host or netname or "сеть без признака мобильного оператора"
        warning = f"Оператор: {who}. Выход {where}, не мобильный. Ozon такой IP режет."
    return {
        "mobile": mobile, "org": org, "asn": asn, "hostname": host,
        "netname": netname, "descr": descr, "warning": warning,
    }


def lookup_exit(ip: str) -> dict:
    """ASN и тип сети выхода. Запрос с сервера, не через прокси пользователя."""
    fields = {"org": "", "asn": "", "hostname": "", "netname": "", "descr": ""}
    if requests is None or not parse_ip_body(ip or ""):
        return describe_exit(fields)
    sess = requests.Session()
    sess.trust_env = False
    direct = {"http": None, "https": None}
    try:
        resp = sess.get(f"https://ipinfo.io/{ip}/json", timeout=8, proxies=direct)
        if resp.ok:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") or resp.text.startswith("{") else {}
            if isinstance(data, dict):
                org = str(data.get("org") or "")
                match = re.search(r"\bAS\d+\b", org)
                if match:
                    fields["asn"] = match.group(0)
                    org = org.replace(match.group(0), "").strip(" -")
                fields["org"] = org
                fields["hostname"] = str(data.get("hostname") or "")
    except Exception as e:
        _log("ipinfo " + type(e).__name__)
    try:
        resp = sess.get(
            "https://stat.ripe.net/data/whois/data.json",
            params={"resource": ip}, timeout=8, proxies=direct,
        )
        if resp.ok:
            records = ((resp.json() or {}).get("data") or {}).get("records") or []
            for group in records:
                if not isinstance(group, list):
                    continue
                keys = {item.get("key"): item.get("value") for item in group if isinstance(item, dict)}
                if keys.get("netname") or keys.get("inetnum"):
                    fields["netname"] = str(keys.get("netname") or "")
                    fields["descr"] = str(keys.get("descr") or "")
                    break
    except Exception as e:
        _log("ripe " + type(e).__name__)
    return describe_exit(fields)


_OZON_TITLE_BLOCK = (
    "доступ ограничен",
    "нет соединения",
    "access denied",
    "antibot",
    "captcha",
    "робот",
)


def _ozon_title_placeholder(title: str) -> bool:
    """«Loading <url>» и сам URL в заголовке — это ещё не главная, даже если внутри есть ozon."""
    low = _norm_text(title).strip()
    if not low:
        return False
    if low.startswith("loading") or low.startswith("загрузка"):
        return True
    return "http://" in low or "https://" in low


def _ozon_url_pending(url: str) -> bool:
    """abt_att и __rr в адресе ставит антибот. Такой адрес не проход."""
    query = (urlparse(url or "").query or "").lower()
    return "abt_att" in query or "__rr" in query


def ozon_home_clear(info) -> bool:
    """document.title: непустой, ozon или озон, без стены и без заглушки «Loading <url>».
    Адрес с abt_att или __rr — ещё не проход, ждём дальше. Тело страницы не смотрим."""
    raw_title = (info or {}).get("title") or ""
    if _ozon_title_placeholder(raw_title):
        return False
    if _ozon_url_pending((info or {}).get("url") or ""):
        return False
    title = _norm_text(raw_title)
    if not title:
        return False
    if "ozon" not in title and "озон" not in title:
        return False
    if any(marker in title for marker in _OZON_TITLE_BLOCK):
        return False
    return True


def ozon_warm_tick(elapsed, done) -> str:
    if elapsed >= OZON_WARM_LIMIT:
        return "stop"
    for mark in OZON_WARM_RELOADS:
        if elapsed >= mark and mark not in done:
            return "reload"
    return "wait"


def ozon_warm_pass(snapshot, reload, sleep, now=None) -> dict:
    """Главная Ozon до 25 с. Перезагрузка на 10-й и 18-й секунде. После прохода пауза 3 с."""
    now = now or time.monotonic
    started = now()
    done = []
    events = []
    info = {}
    while True:
        elapsed = now() - started
        info = snapshot() or {}
        events.append({"elapsed": elapsed, "action": "see", "title": (info.get("title") or "")[:80]})
        if ozon_home_clear(info):
            sleep(OZON_WARM_SETTLE)
            events.append({"elapsed": now() - started, "action": "settle"})
            return {"ok": True, "events": events, "info": info}
        tick = ozon_warm_tick(elapsed, done)
        if tick == "stop":
            events.append({"elapsed": elapsed, "action": "stop"})
            return {"ok": False, "events": events, "info": info}
        if tick == "reload":
            mark = next(m for m in OZON_WARM_RELOADS if elapsed >= m and m not in done)
            done.append(mark)
            events.append({"elapsed": elapsed, "action": "reload", "at": mark})
            reload()
            continue
        sleep(1)


def wb_keep_waiting(info, elapsed, has_phone) -> bool:
    """Пока нет поля телефона и не вышли 40 с — ждём, в том числе «Проверяем браузер»."""
    if has_phone:
        return False
    if elapsed >= WB_WAIT_S:
        return False
    kind = classify_page(info or {})
    if kind in ("offline", "challenge"):
        return False
    return True


def wb_cooldown_remaining(now=None) -> int:
    now = time.time() if now is None else now
    until = int((_load().get("wb_check") or {}).get("until") or 0)
    left = int(until - now)
    return left if left > 0 else 0


def mark_wb_check(passed) -> dict:
    """Успех — поле телефона открылось. Два отказа подряд включают паузу."""
    with _lock:
        data = _load()
        row = _clean_wb_check(data.get("wb_check"))
        if passed:
            row = {"fails": 0, "until": 0}
        else:
            row["fails"] += 1
            if row["fails"] >= WB_CHECK_STREAK:
                row["until"] = int(time.time()) + WB_COOLDOWN_S
        data["wb_check"] = row
        _write(data)
        return dict(row)


def wb_shell(info) -> bool:
    """Главная-оболочка со спиннером и баннером. Это не страница «Проверяем браузер»."""
    text = _page_text(info)
    return "лови лучшие цены" in text and "проверяем браузер" not in text


def wb_on_id(info) -> bool:
    host = (urlparse((info or {}).get("url") or "").hostname or "").lower()
    return host == "id.wb.ru" or host.endswith(".id.wb.ru")


def _ru_minutes(n) -> str:
    n = max(0, int(n))
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        unit = "минуту"
    elif n10 in (2, 3, 4) and n100 not in (12, 13, 14):
        unit = "минуты"
    else:
        unit = "минут"
    return f"{n} {unit}"


def wb_retry_message(seconds) -> str:
    minutes = max(1, (int(seconds) + 59) // 60)
    return "WB проверяет браузер, попробуйте через " + _ru_minutes(minutes)


def wb_check_message(elapsed, streak) -> str:
    if int(streak or 0) >= WB_CHECK_STREAK:
        return wb_retry_message(WB_COOLDOWN_S)
    sec = max(0, int(round(elapsed or 0)))
    return f"WB проверяет браузер. Ждали {sec} с, страница входа не открылась."


def wb_wait_line(elapsed, info, has_phone, shell=False) -> str:
    """Секунды от открытия этой страницы, не от запуска браузера."""
    sec = max(0, int(round(elapsed or 0)))
    if has_phone:
        outcome = "поле телефона"
    else:
        kind = classify_page(info or {})
        if kind == "pending":
            outcome = "всё ещё проверка"
        elif shell or wb_shell(info):
            outcome = "оболочка со спиннером"
        elif kind in ("offline", "challenge"):
            outcome = "антибот"
        elif wb_on_id(info):
            outcome = "id.wb.ru без поля"
        else:
            outcome = page_face(info)
    return f"ждали {sec} с от страницы, итог: {outcome}"


def _set_manual_open(flag, shop=""):
    with _lock:
        _manual_pub["open"] = bool(flag)
        _manual_pub["shop"] = shop if flag and shop in SHOPS else ""


def manual_is_open() -> bool:
    with _lock:
        return bool(_manual_pub["open"])


def manual_shop() -> str:
    with _lock:
        return _manual_pub["shop"] if _manual_pub["open"] else ""


def manual_uses_touch(shop, mode) -> bool:
    """Телефон — касание. Desktop и patchright — мышь, не touch поверх has_touch: false."""
    if shop == "ozon" and mode in ("desktop", "patchright"):
        return False
    return True


def manual_point(x, y, view_w, view_h, page_w=None, page_h=None):
    """Точка на картинке в пикселях экрана → CSS-пиксели вьюпорта 412×915."""
    page_w = PIXEL7_PROFILE["viewport"]["width"] if page_w is None else page_w
    page_h = PIXEL7_PROFILE["viewport"]["height"] if page_h is None else page_h
    try:
        x, y, view_w, view_h = float(x), float(y), float(view_w), float(view_h)
        page_w, page_h = float(page_w), float(page_h)
    except (TypeError, ValueError):
        return None
    if view_w <= 0 or view_h <= 0 or page_w <= 0 or page_h <= 0:
        return None
    px = min(max(x * page_w / view_w, 0), page_w - 1)
    py = min(max(y * page_h / view_h, 0), page_h - 1)
    return (round(px, 2), round(py, 2))


def manual_delta(dy, view_h, page_h=None):
    page_h = PIXEL7_PROFILE["viewport"]["height"] if page_h is None else page_h
    try:
        dy, view_h, page_h = float(dy), float(view_h), float(page_h)
    except (TypeError, ValueError):
        return None
    if view_h <= 0 or page_h <= 0:
        return None
    return max(-2400.0, min(2400.0, dy * page_h / view_h))


def check_ip() -> dict:
    proxy = _proxy()
    if not proxy:
        return {"ok": False, "error": "Сначала сохраните прокси. Без него трафик пойдёт с IP сервера, кабинет так не открыть."}
    try:
        server_ip = fetch_ip_direct()
    except Exception as e:
        return {"ok": False, "error": "Не удалось узнать IP сервера: " + redact(e, proxy)}
    try:
        exit_ip = get_driver().exit_ip(proxy)
    except Exception as e:
        return {"ok": False, "error": "Прокси не ответил: " + redact(e, proxy)}
    server_ip = parse_ip_body(server_ip) or str(server_ip or "")
    exit_ip = parse_ip_body(exit_ip) or str(exit_ip or "")
    if not parse_ip_body(server_ip) or not parse_ip_body(exit_ip):
        return {"ok": False, "error": "Проверка IP не дала адреса. Успех не засчитываю."}
    _log("ip check server " + server_ip + " exit " + exit_ip)
    if server_ip == exit_ip:
        return {
            "ok": False,
            "error": "Трафик вышел с IP сервера. Прокси не сработал.",
            "server_ip": server_ip,
            "exit_ip": exit_ip,
        }
    kind = lookup_exit(exit_ip)
    message = f"Трафик выходит не с IP сервера ({exit_ip}). IP сервера: {server_ip}."
    if kind.get("warning"):
        message += " " + kind["warning"]
    elif kind.get("org"):
        message += " Оператор: " + kind["org"]
        if kind.get("asn"):
            message += ", " + kind["asn"]
        message += "."
    return {
        "ok": True,
        "server_ip": server_ip,
        "exit_ip": exit_ip,
        "org": kind.get("org") or "",
        "asn": kind.get("asn") or "",
        "mobile": bool(kind.get("mobile")),
        "warning": kind.get("warning") or "",
        "message": message,
    }


def _no_proxy_error():
    return {"ok": False, "error": "Без сохранённого прокси вход не начнётся: с IP сервера кабинет не открывается."}


def begin_login(shop: str, phone: str) -> dict:
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    prepared = prepare_phone(sid, phone)
    if not prepared.get("ok"):
        return prepared
    proxy = _proxy()
    if not proxy:
        return _no_proxy_error()
    if sid == "wb":
        left = wb_cooldown_remaining()
        if left > 0:
            msg = wb_retry_message(left)
            note_step(sid, "login", "", msg, "cooldown")
            return {"ok": False, "error": msg}
    if _status(sid) == "awaiting_code":
        opened = False
        try:
            opened = bool(get_driver().code_page_open(sid))
        except Exception as e:
            _log("code page " + sid + " " + type(e).__name__)
        if opened:
            note_step(sid, "login", "", "повторный вход не запущен, страница кода открыта", "awaiting_code")
            return {"ok": False, "status": "awaiting_code", "error": _code_wait_block(sid)}
    try:
        result = get_driver().begin_login(sid, prepared["form"], proxy)
    except Exception as e:
        _log("login failed " + sid + " " + type(e).__name__)
        return {"ok": False, "error": "Вход не выполнен: " + redact(e, proxy)}
    return _finish_login(sid, prepared["intl"], result, proxy)


def submit_code(shop: str, code: str) -> dict:
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{4,8}", code):
        if sid == "wb":
            return {"ok": False, "error": WB_CODE_HINT + ". Нужны 4–8 цифр."}
        return {"ok": False, "error": "Код из СМС — это 4–8 цифр"}
    if _status(sid) != "awaiting_code":
        return {"ok": False, "error": "Сейчас код не ожидается. Нажмите «Войти»."}
    proxy = _proxy()
    if not proxy:
        return _no_proxy_error()
    try:
        result = get_driver().submit_code(sid, code, proxy)
    except Exception as e:
        _log("code failed " + sid + " " + type(e).__name__)
        return {"ok": False, "error": "Код не отправлен: " + redact(e, proxy, [code])}
    if result.get("lost"):
        _set_shop(sid, "none", storage=None)
        return {"ok": False, "error": "Окно ввода кода закрыто — начните вход заново."}
    return _finish_login(sid, "", result, proxy, code=code)


def _proved_auth(result, code) -> str:
    """Второй шаг: код из СМС или пароль. Телефон сам по себе сюда не попадает."""
    if not (result or {}).get("authenticated"):
        return ""
    kind = (result or {}).get("auth") or ""
    if kind == "code" and code:
        return "code"
    if kind == "password":
        return "password"
    return ""


def _finish_login(sid, phone, result, proxy, code="") -> dict:
    result = result or {}
    status = result.get("status")
    step = "code" if code else "login"
    if status in ("offline", "blocked", "challenge"):
        _set_shop(sid, "blocked", phone_mask=mask_phone_value(phone) or None, storage=None,
                  why=result.get("face") or "антибот, кабинет не открыт")
        _log(sid + " blocked by antibot")
        _record_steps(sid, result, step, "blocked")
        return {"ok": False, "error": BLOCKED_ERROR, "status": "blocked"}
    proved = _proved_auth(result, code)
    if status == "active" and not proved:
        _set_shop(sid, "awaiting_code", phone_mask=mask_phone_value(phone) or None, storage=None, auth="",
                  why="введён только телефон, кода или пароля не было")
        _log(sid + " phone without code, not active")
        _record_steps(sid, result, step, "awaiting_code")
        return {
            "ok": True,
            "status": "awaiting_code",
            "message": (
                "Введён только телефон. " + WB_CODE_HINT + " — без него сессия не активна."
                if sid == "wb" else
                "Введён только телефон. Введите код из СМС — без него сессия не активна."
            ),
        }
    if result.get("kept"):
        _log(sid + " code page kept")
        note_step(sid, "login", result.get("url") or "", "повторный вход не запущен, страница кода открыта", "awaiting_code", title=result.get("title") or "")
        return {"ok": False, "status": "awaiting_code", "error": result.get("error") or _code_wait_block(sid)}
    if result.get("ok") is False or result.get("lost"):
        err = redact(result.get("error") or "Вход не выполнен.", proxy, [code])
        if "vpn" in err.lower() or "нет соединения" in err.lower():
            err = BLOCKED_ERROR
            _set_shop(sid, "blocked", phone_mask=mask_phone_value(phone) or None, storage=None, why="антибот в тексте страницы")
            status = "blocked"
        _log(sid + " login not finished")
        _record_steps(sid, result, step, "blocked" if status == "blocked" else err)
        return {"ok": False, "error": err, "status": status or _status(sid)}
    if status == "active" and proved and storage_usable(result.get("storage")):
        if proved == "password":
            why = "пароль принят, кабинет открыт"
        elif sid == "wb":
            why = "код из приложения принят, кабинет открыт"
        else:
            why = "код из СМС принят, кабинет открыт"
        _set_shop(sid, "active", phone_mask=mask_phone_value(phone) or None, storage=result.get("storage"), auth=proved, why=why)
        try:
            get_driver().close_login(sid)
        except Exception as e:
            _log("close after login " + redact(e, proxy, [code]))
        _log(sid + " session active")
        _record_steps(sid, result, step, "active")
        return {"ok": True, "status": "active", "message": result.get("message") or "Сессия сохранена."}
    if status == "active":
        _log(sid + " cabinet without storage")
        _record_steps(sid, result, step, "not active")
        return {"ok": False, "error": "Кабинет открылся, но сессия не сохранилась. Вход не засчитан."}
    if status == "awaiting_code":
        _set_shop(sid, "awaiting_code", phone_mask=mask_phone_value(phone) or None, storage=None, auth="",
                  why="открылась форма кода, сессия ещё не активна")
        _log(sid + " awaiting code")
        _record_steps(sid, result, step, "awaiting_code")
        message = result.get("message") or ""
        if sid == "wb":
            if "колокольчик" not in message.lower():
                message = WB_CODE_HINT
        elif not message:
            message = "Открылась форма кода. Введите код из СМС."
        return {"ok": True, "status": "awaiting_code", "message": message}
    _log(sid + " login not finished")
    err = redact(result.get("error") or "Вход не выполнен.", proxy, [code])
    _record_steps(sid, result, step, err)
    return {"ok": False, "error": err}


def logout(shop: str) -> dict:
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    try:
        get_driver().close_login(sid)
    except Exception as e:
        _log("logout " + sid + " " + redact(e, _proxy()))
    if manual_is_open() and manual_shop() == sid:
        try:
            get_driver().manual_cancel()
        except Exception as e:
            _log("manual logout " + type(e).__name__)
    _set_shop(sid, "none", phone_mask="", storage=None)
    _log(sid + " logout")
    return {"ok": True, "status": "none", "message": "Сессия удалена."}


def manual_start(shop="wb") -> dict:
    """Живой экран входа. Тот же Chromium, профиль Pixel 7 и прокси. Ozon — режим Android."""
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    proxy = _proxy()
    if not proxy:
        return _no_proxy_error()
    try:
        result = get_driver().manual_open(proxy, sid) or {}
    except Exception as e:
        _log("manual open " + type(e).__name__)
        return {"ok": False, "error": "Ручной вход не открылся."}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Ручной вход не открылся."}
    if not result.get("already"):
        note_step(sid, "manual", result.get("url") or "", "ручной вход начат", "ручной вход начат")
    vp = PIXEL7_PROFILE["viewport"]
    return {
        "ok": True,
        "already": bool(result.get("already")),
        "width": int(result.get("width") or vp["width"]),
        "height": int(result.get("height") or vp["height"]),
    }


def manual_frame():
    """JPEG текущего кадра. Пусто, если сеанса нет. На диск не пишется."""
    try:
        blob = get_driver().manual_frame()
    except Exception:
        return b""
    if isinstance(blob, (bytes, bytearray)) and bytes(blob).startswith(b"\xff\xd8"):
        return bytes(blob)
    return b""


def manual_input(payload) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Неизвестная команда."}
    action = str(payload.get("action") or "")
    if action not in ("click", "scroll", "type", "key", "back", "reload"):
        return {"ok": False, "error": "Неизвестная команда."}
    try:
        result = get_driver().manual_input(payload) or {}
    except Exception as e:
        _log("manual input " + type(e).__name__)
        return {"ok": False, "error": "Команда не выполнена."}
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "error": (result or {}).get("error") or "Команда не выполнена."}
    return {"ok": True}


def manual_finish() -> dict:
    proxy = _proxy()
    if not proxy:
        return _no_proxy_error()
    try:
        result = get_driver().manual_finish(proxy) or {}
    except Exception as e:
        _log("manual finish " + type(e).__name__)
        return {"ok": False, "error": "Проверка кабинета не выполнена."}
    if result.get("ok"):
        return {"ok": True, "status": "active", "message": result.get("message") or "Сессия сохранена."}
    return {"ok": False, "error": result.get("error") or "Кабинет не открылся. Сессия не активна."}


def manual_cancel() -> dict:
    try:
        result = get_driver().manual_cancel() or {}
    except Exception as e:
        _log("manual cancel " + type(e).__name__)
        _set_manual_open(False)
        return {"ok": False, "error": "Сеанс не закрылся."}
    if result.get("closed"):
        return {"ok": True, "message": "Ручной вход отменён."}
    return {"ok": True, "message": "Ручной вход не был открыт."}


def _require_session(shop) -> dict:
    proxy = _proxy()
    if not proxy:
        return {"error": "Нет сохранённого прокси. Кабинет с сервера без него не открывается."}
    status = _status(shop)
    if status == "awaiting_code":
        if shop == "wb":
            return {"error": "Вход не закончен. " + WB_CODE_HINT + ".", "status": status}
        return {"error": "Вход не закончен: нужен код из СМС.", "status": status}
    if status == "blocked":
        return {"error": BLOCKED_ERROR, "status": "blocked"}
    if status == "expired":
        if shop == "wb":
            return {"error": "Сессия слетела, нужен повторный вход. " + WB_CODE_HINT + ".", "status": "expired"}
        return {"error": "Сессия слетела, нужен повторный вход (код из СМС).", "status": "expired"}
    if status != "active" or not storage_usable(_storage(shop)):
        if shop == "wb":
            return {"error": "Нет сессии. Войдите в Настройки → Маркетплейсы. " + WB_CODE_HINT + ".", "status": status or "none"}
        return {"error": "Нет сессии. Войдите в Настройки → Маркетплейсы (код из СМС).", "status": status or "none"}
    return {}


def _drop_reason(result) -> str:
    """Пусто, если страницу можно читать как кабинет. Иначе blocked или closed."""
    result = result or {}
    info = {
        "url": result.get("url") or "",
        "title": result.get("title") or "",
        "text": result.get("text") or "",
    }
    kind = classify_page(info) if any(info.values()) else ""
    if kind == "pending" or result.get("session") == "pending":
        return "pending"
    if kind in ("offline", "challenge") or result.get("session") in ("blocked", "challenge"):
        return "blocked"
    if cabinet_open(info):
        return ""
    # Стираем cookies только когда сайт явно вернул на вход. Чужой текст страницы — не повод.
    if login_redirect(info):
        return "closed"
    if result.get("session") == "active" or result.get("ok"):
        return ""
    return "keep"


def _apply_read_session(shop, result) -> dict:
    reason = _drop_reason(result)
    if not reason:
        return {}
    if reason == "pending":
        note_step(
            shop, "read", (result or {}).get("url") or "",
            (result or {}).get("text") or (result or {}).get("title") or "проверка браузера",
            "pending",
            title=(result or {}).get("title") or "",
            shot=(result or {}).get("shot") or "",
        )
        return {"error": "Сайт проверяет браузер. Кабинет ещё не открылся.", "shop": shop, "status": _status(shop)}
    if reason == "blocked":
        why = (result or {}).get("face") or page_face({
            "url": (result or {}).get("url") or "",
            "title": (result or {}).get("title") or "",
            "text": (result or {}).get("text") or "",
        })
        _set_shop(shop, "blocked", storage=None, why=why or "антибот, кабинет не открыт")
        _log(shop + " blocked by antibot")
        note_step(
            shop, "read", (result or {}).get("url") or "",
            ((result or {}).get("text") or "") + " · " + (why or ""),
            "blocked",
            title=(result or {}).get("title") or "",
            shot=(result or {}).get("shot") or "",
        )
        return {"error": BLOCKED_ERROR, "shop": shop, "status": "blocked"}
    if reason == "keep":
        note_step(
            shop, "read", (result or {}).get("url") or "",
            (result or {}).get("text") or (result or {}).get("title") or "кабинет не открылся",
            "kept",
            title=(result or {}).get("title") or "",
            shot=(result or {}).get("shot") or "",
        )
        return {
            "error": "Кабинет не открылся. Сессия сохранена.",
            "shop": shop,
            "status": _status(shop),
        }
    why = "редирект на вход"
    _set_shop(shop, "expired", storage=None, why=why)
    _log(shop + " session expired closed")
    note_step(
        shop, "read", (result or {}).get("url") or "",
        (result or {}).get("text") or (result or {}).get("title") or why,
        "expired",
        title=(result or {}).get("title") or "",
        shot=(result or {}).get("shot") or "",
    )
    return {"error": OFFLINE_ERROR, "shop": shop, "status": "expired"}


def _read(shop, section, order_id, state) -> dict:
    if not skill_on(state):
        return {"error": "Навык «Маркетплейсы» выключен: Настройки → Ассистент → Навыки ассистента."}
    sid = _known_shop(shop)
    if not sid:
        return {"error": "Неизвестный магазин: укажите ozon или wb."}
    if section == "order" and not _ORDER_RE.fullmatch(order_id or ""):
        return {"error": "Нужен номер заказа."}
    if order_id and not _ORDER_RE.fullmatch(order_id):
        return {"error": "Некорректный номер заказа."}
    blocked = _require_session(sid)
    if blocked:
        return blocked
    try:
        result = get_driver().read(sid, section, _proxy(), _storage(sid), order_id or "")
    except Exception as e:
        _log("read " + sid + " " + section + " " + type(e).__name__)
        return {"error": "Не удалось открыть кабинет: " + redact(e, _proxy())}
    if section in _READ_TRACE:
        length = result.get("text_len")
        if length is None:
            length = len(result.get("text") or "")
        note_step(
            sid, "read", result.get("url") or "",
            f"текст {int(length)} символов",
            "ok" if result.get("ok") else (result.get("session") or "read"),
            title=result.get("title") or "",
            shot=result.get("shot") or "",
            apis=result.get("apis") or "",
        )
    failed = _apply_read_session(sid, result)
    if failed:
        return failed
    if section not in _READ_TRACE:
        note_step(sid, "read", result.get("url") or "", result.get("text") or result.get("title") or "", "ok")
    text = mask_phones(result.get("text") or "")
    if section == "profile":
        text = text[:1800]
    return {
        "ok": True,
        "shop": sid,
        "section": section,
        "title": (result.get("title") or "")[:200],
        "url": _safe_url(result.get("url") or ""),
        "text": text[:8000],
    }


def _safe_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _norm_text(text: str) -> str:
    return re.sub(r"[\u00a0\u2007\u202f]+", " ", text or "").lower()


def _append_journal(data, shop, step, url, summary, result, title="", shot="", apis=""):
    data.setdefault("journal", [])
    data["journal"].append({
        "t": int(time.time()),
        "shop": shop if shop in SHOPS else "",
        "step": str(step or "")[:24],
        "url": _safe_url(url)[:200],
        "title": _journal_text(title)[:120],
        "summary": _journal_text(summary),
        "result": _journal_text(result),
        "shot": shot if _SHOT_RE.fullmatch(shot or "") else "",
        "apis": scrub_secrets(apis)[:3500],
    })
    data["journal"] = data["journal"][-_JOURNAL_MAX:]


def _journal_text(text) -> str:
    return page_snippet({"text": text}, 180)[:180]


def _clean_journal(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw[-_JOURNAL_MAX:]:
        if not isinstance(item, dict):
            continue
        shot = str(item.get("shot") or "")
        out.append({
            "t": int(item.get("t") or 0),
            "shop": item.get("shop") if item.get("shop") in SHOPS else "",
            "step": str(item.get("step") or "")[:24],
            "url": _safe_url(str(item.get("url") or "")),
            "title": _journal_text(item.get("title") or "")[:120],
            "summary": _journal_text(item.get("summary") or ""),
            "result": _journal_text(item.get("result") or ""),
            "shot": shot if _SHOT_RE.fullmatch(shot) else "",
            "apis": scrub_secrets(item.get("apis") or "")[:3500],
        })
    return out


def _code_wait_block(shop) -> str:
    if shop == "wb":
        return "Код уже запрошен, страница ввода открыта. " + WB_CODE_HINT + ". Новый код не отправляем."
    return "Код уже запрошен, страница ввода открыта. Введите код из СМС. Новый код не отправляем."


def _heal_lost_code(data) -> bool:
    """«Ждём код» живёт только в памяти браузера. После рестарта процесса страницы уже нет."""
    changed = False
    for sid in SHOPS:
        row = data["shops"][sid]
        if row.get("status") != "awaiting_code" or row.get("boot") == _BOOT:
            continue
        row["status"] = "none"
        row["auth"] = ""
        row["storage"] = None
        row["boot"] = ""
        row["updated"] = int(time.time())
        _append_journal(
            data, sid, "status", "",
            "Процесс перезапустился, окно ввода кода закрыто. Сессии нет.",
            "none",
        )
        changed = True
    return changed


def _heal_unproved(data) -> bool:
    changed = False
    for sid in SHOPS:
        row = data["shops"][sid]
        if row.get("status") == "active" and row.get("auth") not in _AUTH_OK:
            row["status"] = "expired"
            row["auth"] = ""
            row["storage"] = None
            row["updated"] = int(time.time())
            _append_journal(data, sid, "status", "", "Активна без кода и пароля", "expired")
            changed = True
    return changed


def note_step(shop, step, url, summary, result, title="", shot="", apis=""):
    proxy = _proxy()
    summary = redact(_journal_text(summary), proxy)
    result = redact(_journal_text(result), proxy)
    title = redact(_journal_text(title), proxy)
    apis = redact(scrub_secrets(apis), proxy)[:3500]
    with _lock:
        data = _load()
        _append_journal(data, shop, step, url, summary, result, title=title, shot=shot, apis=apis)
        _write(data)


def shots_dir() -> Path:
    path = data_dir() / "market_shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_jpeg(blob: bytes) -> str:
    """Маленький jpeg шага входа. Не jpeg и слишком большой кадр не храним."""
    if not blob or len(blob) > 400_000 or not blob.startswith(b"\xff\xd8"):
        return ""
    name = "m" + secrets.token_hex(8) + ".jpg"
    (shots_dir() / name).write_bytes(blob)
    _prune_shots()
    return name


def shot_path(name: str):
    if not _SHOT_RE.fullmatch(name or ""):
        return None
    path = shots_dir() / name
    return path if path.is_file() else None


def _prune_shots():
    files = sorted(shots_dir().glob("m*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[_SHOT_MAX:]:
        try:
            old.unlink()
        except OSError:
            pass


def _step_summary(item) -> str:
    summary = (item or {}).get("summary") or ""
    face = (item or {}).get("face") or ""
    if face and face not in summary:
        summary = (summary + " · " + face).strip(" ·")
    return summary


def _record_steps(sid, result, fallback_step, fallback_result):
    steps = (result or {}).get("steps")
    if isinstance(steps, list) and steps:
        for item in steps:
            if not isinstance(item, dict):
                continue
            note_step(
                sid,
                item.get("step") or fallback_step,
                item.get("url") or "",
                _step_summary(item),
                item.get("result") or fallback_result,
                title=item.get("title") or "",
                shot=item.get("shot") or "",
            )
        return
    note_step(
        sid,
        fallback_step,
        (result or {}).get("url") or "",
        _step_summary(result or {"summary": (result or {}).get("summary") or (result or {}).get("message") or (result or {}).get("error") or "", "face": (result or {}).get("face") or ""}),
        fallback_result,
        title=(result or {}).get("title") or "",
        shot=(result or {}).get("shot") or "",
    )


def scrub_secrets(text) -> str:
    """Убирает токены, cookie и телефоны. Query в журнал всё равно не пишем."""
    out = mask_phones(str(text or ""))
    out = re.sub(
        r'(?i)("(?:token|cookie|authorization|access_token|refresh_token|jwt|password|set-cookie)"\s*:\s*")[^"]*"',
        r'\1***"',
        out,
    )
    out = re.sub(r"(?i)\b(bearer\s+)\S+", r"\1***", out)
    out = re.sub(r"(?i)([?&](?:token|access_token|cookie|authorization)=)[^&\s]+", r"\1***", out)
    return out


def redact_api_snippet(text) -> str:
    return scrub_secrets(text)[:300]


def is_heavy(resource_type) -> bool:
    return (resource_type or "") in HEAVY_TYPES


def wb_skip_body(url) -> bool:
    """Справочник ПВЗ и статика. Это не список заказов, тело не читаем."""
    blob = (url or "").lower()
    if "wbbasket" in blob or "static-basket" in blob:
        return True
    path = urlparse(blob).path or ""
    return "/delivery/offices" in path


def wb_orders_api(url) -> bool:
    """Список покупок или возвратов. Справочник ПВЗ и srids сюда не входят."""
    if wb_skip_body(url):
        return False
    blob = (url or "").lower()
    path = (urlparse(blob).path or "").rstrip("/")
    if path.endswith("/srids"):
        return False
    needles = (
        "/myorders/archive/get",
        "/archive/getbyrids",
        "/shopper/api/v2/purchases/list",
        "/webapi/lk/refunds/get",
        "/webapi/lk/refunds/list",
        "/webapi/lk/claims/get",
    )
    return any(part in blob for part in needles)


def wb_list_settled(captures) -> bool:
    """Список ответил 200 без error. Пустой список тоже ответ."""
    for item in captures or []:
        if not wb_orders_api((item or {}).get("url") or ""):
            continue
        if int((item or {}).get("status") or 0) != 200:
            continue
        body = ((item or {}).get("body") or "").lstrip()
        if not body or body[0] not in "{[":
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("error"):
            continue
        return True
    return False


def _flat_key(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _scalar(value) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _pick(flat, keys) -> str:
    for key in keys:
        value = flat.get(key)
        if _scalar(value) and str(value).strip():
            return str(value).strip()
    return ""


def _price_text(value) -> str:
    if not _scalar(value):
        return ""
    if isinstance(value, str):
        return value[:40]
    number = float(value)
    if number >= 10000 and number.is_integer():
        number = number / 100
    if number.is_integer():
        return f"{int(number)} ₽"
    return f"{number:.2f} ₽"


def _date_text(value) -> str:
    if not _scalar(value):
        return ""
    raw = str(value).strip()
    if re.fullmatch(r"\d{13}", raw):
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(int(raw) / 1000))
        except (OverflowError, OSError, ValueError):
            return raw[:40]
    return raw[:40]


def _as_order(flat, inherited_id) -> dict:
    oid = _pick(flat, ("orderid", "rid", "srid", "claimid", "refundid")) or inherited_id
    name = _pick(flat, ("goodsname", "imtname", "name", "title"))
    status = _pick(flat, ("statusname", "trackingstatus"))
    if not status:
        raw_status = flat.get("status")
        if isinstance(raw_status, str) and len(raw_status.strip()) > 2:
            status = raw_status.strip()
    price = ""
    for key in ("price", "saleprice", "totalprice", "finalprice"):
        if key in flat:
            price = _price_text(flat.get(key))
            if price:
                break
    date = ""
    for key in ("orderdate", "datetime", "createdat", "deliverydate", "date", "createdt", "dt"):
        if key in flat:
            date = _date_text(flat.get(key))
            if date:
                break
    pvz = _pick(flat, ("pooaddress", "deliveryaddress", "officeaddress", "pvzaddress", "address"))
    if not oid or not (name or status or price):
        return {}
    if name.lower() in ("true", "false", "null"):
        name = ""
        if not (status or price):
            return {}
    return {"id": oid[:40], "name": name[:120], "price": price[:40], "status": status[:80], "date": date[:40], "pvz": pvz[:160]}


def _walk_orders(node, found, inherited_id="", depth=0):
    if depth > 7 or len(found) >= 40:
        return
    if isinstance(node, list):
        for item in node[:100]:
            _walk_orders(item, found, inherited_id, depth + 1)
        return
    if not isinstance(node, dict):
        return
    flat = {_flat_key(key): value for key, value in node.items()}
    oid = _pick(flat, ("orderid", "rid", "srid", "claimid", "refundid")) or inherited_id
    row = _as_order(flat, inherited_id)
    if row:
        found.append(row)
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_orders(value, found, oid, depth + 1)


def parse_wb_orders(data) -> list:
    """Номер, товар, цена, статус, дата и ПВЗ из JSON кабинета WB."""
    found = []
    _walk_orders(data, found)
    merged = []
    index = {}
    for row in found:
        key = row["id"] + "\n" + row.get("name", "")
        if key in index:
            current = merged[index[key]]
            for field, value in row.items():
                if value and not current.get(field):
                    current[field] = value
            continue
        index[key] = len(merged)
        merged.append(dict(row))
    named = {row["id"] for row in merged if row.get("name")}
    return [row for row in merged if row.get("name") or row["id"] not in named]


def format_wb_rows(rows) -> str:
    blocks = []
    for row in rows[:30]:
        lines = ["Заказ " + row.get("id", "")]
        if row.get("name"):
            lines.append("Товар: " + row["name"])
        if row.get("price"):
            lines.append("Цена: " + row["price"])
        if row.get("status"):
            lines.append("Статус: " + row["status"])
        if row.get("date"):
            lines.append("Дата: " + row["date"])
        if row.get("pvz"):
            lines.append("ПВЗ: " + row["pvz"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _api_rank(url) -> int:
    blob = (url or "").lower()
    if any(part in blob for part in ("order", "deliver", "refund", "return", "purchase", "archive", "myorders")):
        return 0
    return 1


def api_journal(captures) -> str:
    ranked = sorted(captures or [], key=lambda item: _api_rank((item or {}).get("url") or ""))
    lines = []
    for item in ranked[:8]:
        url = _safe_url((item or {}).get("url") or "")
        status = (item or {}).get("status") or 0
        body = redact_api_snippet((item or {}).get("body") or "")
        line = f"{status} {url}".strip()
        if body:
            line += "\n" + body
        if url or body:
            lines.append(line)
    return "\n".join(lines)[:3500]


def rows_from_captures(captures) -> list:
    found = []
    seen = set()
    for item in captures or []:
        body = (item or {}).get("body") or ""
        stripped = body.lstrip()
        if not stripped or stripped[0] not in "{[":
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        for row in parse_wb_orders(data):
            key = (row.get("id"), row.get("name"), row.get("status"))
            if key in seen:
                continue
            seen.add(key)
            found.append(row)
            if len(found) >= 30:
                return found
    return found


_MANUAL_SKIP_TYPES = frozenset({"image", "stylesheet", "font", "media"})
_MANUAL_NET_MAX = 30
MANUAL_TAP_LOOK = 0.6


def _frame_host(url) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _manual_frame_host(host: str) -> bool:
    host = (host or "").lower()
    return host.endswith("ozon.ru") or host.endswith("wb.ru") or host.endswith("wildberries.ru")


class ManualTrace:
    """Переходы ручного окна и ответы ≥400. Картинки не пишем, query в журнал не попадает."""

    def __init__(self, shop):
        self.shop = shop if shop in SHOPS else ""
        self.seen = set()
        self.n = 0
        self.main_url = ""

    def _add(self, url, summary, result):
        if not self.shop or self.n >= _MANUAL_NET_MAX:
            return
        safe = _safe_url(url)
        key = (result, safe)
        if key in self.seen:
            return
        self.seen.add(key)
        self.n += 1
        note_step(self.shop, "manual", url or "", summary, result)

    def on_frame(self, frame):
        try:
            url = getattr(frame, "url", "") or ""
        except Exception:
            return
        if not str(url).startswith("http"):
            return
        main = getattr(frame, "parent_frame", None) is None
        if not main and not _manual_frame_host(_frame_host(url)):
            return
        safe = _safe_url(url)
        if main:
            if safe == self.main_url:
                return
            self.main_url = safe
            self._add(url, "страница", "переход")
            return
        self._add(url, "фрейм", "фрейм")

    def on_response(self, response):
        try:
            status = int(getattr(response, "status", 0) or 0)
            if status < 400:
                return
            request = getattr(response, "request", None)
            kind = str(getattr(request, "resource_type", "") or "")
            if kind in _MANUAL_SKIP_TYPES:
                return
            method = str(getattr(request, "method", "") or "")
            url = getattr(response, "url", "") or ""
        except Exception:
            return
        result = " ".join(bit for bit in (str(status), method, kind) if bit)
        self._add(url, result, result)


class ApiSink:
    """XHR и fetch. Тела JSON держим для разбора, в журнал уходит урезанный кусок."""

    def __init__(self):
        self.rows = []

    def add(self, response):
        if len(self.rows) >= 24:
            return
        try:
            request = getattr(response, "request", None)
            kind = getattr(request, "resource_type", "") or ""
            if kind and kind not in ("xhr", "fetch"):
                return
            url = getattr(response, "url", "") or ""
            status = int(getattr(response, "status", 0) or 0)
            headers = {}
            ctype = ""
            body = ""
            # Тело ПВЗ и static-basket не читаем: оно не список и держит поток, пока висят route.
            if not wb_skip_body(url):
                try:
                    headers = response.headers or {}
                except Exception:
                    headers = {}
                if isinstance(headers, dict):
                    ctype = str(headers.get("content-type") or headers.get("Content-Type") or "")
                if "json" in ctype.lower() or kind in ("xhr", "fetch"):
                    try:
                        body = response.text() or ""
                    except Exception:
                        body = ""
            if len(body) > 200_000:
                body = body[:200_000]
            self.rows.append({"url": url, "status": status, "body": body, "type": kind})
        except Exception:
            return


def page_snippet(info, limit=160) -> str:
    text = mask_phones((info or {}).get("text") or "")
    text = re.sub(r"(?i)\b(?:cookie|token|session)[=:][^\s]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def login_fail_message(shop, info) -> str:
    raw = (info or {}).get("text") or ""
    if _NOT_FOUND_RE.search(raw):
        return f"{SHOP_TITLE.get(shop, 'Сайт')}: номер не найден."
    url = _safe_url((info or {}).get("url") or "")
    snippet = page_snippet(info) or ((info or {}).get("title") or "").strip()[:80]
    msg = "Вход остановился"
    if url:
        msg += f" на {url}"
    if snippet:
        msg += f": {snippet}"
    return msg + "."


def _log_page(sid, info, phone_mask="", form_mask=""):
    url = _safe_url((info or {}).get("url") or "")
    bits = f"{sid} page {url}"
    if phone_mask:
        bits += " phone " + phone_mask
    if form_mask:
        bits += " form " + form_mask
    snippet = page_snippet(info)
    if snippet:
        bits += " " + snippet
    _log(bits)


def page_sig(info) -> tuple:
    """Подпись страницы для журнала. Обратный отсчёт «0:59» сам по себе страницу не меняет."""
    info = info or {}
    text = _TIMER_RE.sub("", page_snippet(info, 180))
    title = _TIMER_RE.sub("", info.get("title") or "")
    text = re.sub(r"\s+", " ", text).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return (_safe_url(info.get("url") or ""), title[:120], text)


def code_wait_action(elapsed) -> str:
    """Пока не вышли 5 минут — держим страницу. Вход заново отсюда не начинается."""
    if elapsed >= CODE_WAIT_S:
        return "expire"
    return "hold"


def is_resend_code(label: str) -> bool:
    name = re.sub(r"\s+", " ", (label or "")).strip().lower()
    return "получить новый код" in name


def is_fresh_code_request(label: str) -> bool:
    """Первая кнопка «Получить код». «Получить новый код» во время ожидания не нажимаем."""
    if is_resend_code(label):
        return False
    name = re.sub(r"\s+", " ", (label or "")).strip()
    return bool(re.search(r"получить код|выслать код|отправить код|продолжить|далее", name, re.I))


def decide_after_phone(shop, info, code_field=False) -> dict:
    """После ввода телефона сессия не бывает активной: нужен код или пароль и живой кабинет."""
    kind = classify_page(info)
    if kind == "pending":
        return {"ok": False, "error": "Сайт ещё проверяет браузер. Вход не закончен.", "url": (info or {}).get("url") or "", "title": (info or {}).get("title") or "", "summary": page_snippet(info), "face": page_face(info)}
    if kind in ("offline", "challenge"):
        return {
            "ok": False, "status": "blocked", "error": BLOCKED_ERROR,
            "url": (info or {}).get("url") or "",
            "title": (info or {}).get("title") or "",
            "summary": page_snippet(info),
            "face": page_face(info),
        }
    if _NOT_FOUND_RE.search((info or {}).get("text") or ""):
        return {"ok": False, "error": login_fail_message(shop, info)}
    if kind == "code" or (code_field and kind not in ("login", "password")):
        if shop == "wb":
            message = WB_CODE_HINT
        else:
            text = ((info or {}).get("text") or "").lower()
            sent = any(s in text for s in ("отправлен", "выслал", "смс", "sms"))
            message = "Введите код из СМС." if sent else "Открылась форма кода. Если СМС пришла — введите код."
        return {"ok": True, "status": "awaiting_code", "message": message}
    if kind == "password":
        return {"ok": False, "error": "Сайт просит пароль. Телефон без кода или пароля сессию не активирует."}
    if kind == "cabinet":
        if shop == "wb":
            message = "Введён только телефон. " + WB_CODE_HINT + "."
        else:
            message = "Введён только телефон. Сессия станет активной после кода из СМС."
        return {"ok": True, "status": "awaiting_code", "message": message}
    return {"ok": False, "error": login_fail_message(shop, info)}


def session_from_page(info) -> dict:
    kind = classify_page(info or {})
    url = (info or {}).get("url") or ""
    if kind == "pending":
        return {"ok": False, "session": "pending", "url": url}
    if kind in ("offline", "challenge"):
        return {"ok": False, "session": "blocked", "error": BLOCKED_ERROR, "url": url, "face": page_face(info)}
    if login_redirect(info):
        return {"ok": False, "session": "expired", "url": url}
    if cabinet_open(info):
        return {"ok": True, "session": "active", "url": url}
    return {"ok": False, "session": "public", "url": url}


def execute(name: str, inp: dict, state=None) -> dict:
    """Исполнитель навыка. Выключенный навык сюда до браузера не доходит."""
    if name not in TOOL_NAMES:
        return {"error": "unknown_tool"}
    if not skill_on(state):
        return {"error": "Навык «Маркетплейсы» выключен: Настройки → Ассистент → Навыки ассистента."}
    inp = inp or {}
    if name == "market_cancel_order":
        return _cancel(inp.get("shop"), (inp.get("order_id") or "").strip(), bool(inp.get("confirmed")), state)
    section = _SECTION.get(name)
    if not section:
        return {"error": "unknown_tool"}
    return _read(inp.get("shop"), section, (inp.get("order_id") or "").strip(), state)


def _cancel(shop, order_id, confirmed, state) -> dict:
    if not skill_on(state):
        return {"error": "Навык «Маркетплейсы» выключен: Настройки → Ассистент → Навыки ассистента."}
    sid = _known_shop(shop)
    if not sid:
        return {"error": "Неизвестный магазин: укажите ozon или wb."}
    if not _ORDER_RE.fullmatch(order_id or ""):
        return {"error": "Некорректный номер заказа."}
    if not confirmed:
        return {
            "needs_confirmation": True,
            "message": f"Нужно подтверждение пользователя, чтобы отменить заказ {order_id} на {SHOP_TITLE[sid]}.",
        }
    blocked = _require_session(sid)
    if blocked:
        return blocked
    try:
        result = get_driver().cancel_order(sid, order_id, _proxy(), _storage(sid))
    except Exception as e:
        _log("cancel " + sid + " " + type(e).__name__)
        return {"error": "Не удалось открыть заказ: " + redact(e, _proxy())}
    failed = _apply_read_session(sid, result)
    if failed:
        return failed
    if not result.get("cancelled"):
        return {"ok": False, "shop": sid, "error": result.get("error") or "Заказ не отменён."}
    _log(sid + " cancel confirmed by page")
    return {"ok": True, "shop": sid, "order_id": order_id, "cancelled": True, "text": mask_phones(result.get("text") or "")[:2000]}


def set_driver(driver):
    """Подмена браузера в тестах. None — следующий вызов снова возьмёт Chromium."""
    global _driver
    _driver = driver
    _set_manual_open(False)


def get_driver():
    global _driver
    if _driver is None:
        _driver = PlaywrightCabinet()
    return _driver


class SerialWorker:
    """Очередь на один поток: sync Playwright нельзя звать с потоков HTTP-сервера."""

    def __init__(self, on_idle=None):
        self._q = queue.Queue()
        self._on_idle = on_idle
        self._thread = threading.Thread(target=self._loop, name="market-browser", daemon=True)
        self._started = False
        self._start_lock = threading.Lock()

    def _loop(self):
        while True:
            try:
                item = self._q.get(timeout=1 if self._on_idle else None)
            except queue.Empty:
                try:
                    if self._on_idle:
                        self._on_idle()
                except Exception as e:
                    _log("watch " + type(e).__name__)
                continue
            if item is None:
                return
            fn, args, kwargs, slot = item
            try:
                slot["value"] = fn(*args, **kwargs)
            except Exception as e:
                slot["error"] = e
            finally:
                slot["ev"].set()

    def start(self):
        with self._start_lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def call(self, fn, *args, timeout=75, **kwargs):
        self.start()
        slot = {"ev": threading.Event()}
        self._q.put((fn, args, kwargs, slot))
        if not slot["ev"].wait(timeout):
            raise TimeoutError("браузер не ответил вовремя")
        if "error" in slot:
            raise slot["error"]
        return slot.get("value")


def _pw_proxy(proxy: dict) -> dict:
    out = {"server": f"{proxy['scheme']}://{proxy['host']}:{proxy['port']}"}
    if proxy.get("username"):
        out["username"] = proxy["username"]
        out["password"] = proxy.get("password") or ""
    return out


def _frames(page):
    try:
        return list(page.frames)
    except Exception:
        return []


def _first_locator(page, selector):
    for frame in _frames(page):
        try:
            loc = frame.locator(selector)
            if loc.count():
                return loc.first
        except Exception:
            continue
    return None


def _click_label(page, patterns) -> bool:
    compiled = [re.compile(pat, re.I) for pat in patterns]
    for frame in _frames(page):
        for rx in compiled:
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role, name=rx)
                    if loc.count():
                        loc.first.click(timeout=4000)
                        return True
                except Exception:
                    continue
    return False


def _fill_first(page, selectors, value) -> bool:
    for sel in selectors:
        loc = _first_locator(page, sel)
        if loc is None:
            continue
        try:
            loc.click(timeout=3000)
            loc.fill(value)
            return True
        except Exception:
            continue
    return False


def _type_phone(page, value) -> str:
    """Печатает номер по символам: маска WB не принимает fill() целиком. Возвращает текст поля."""
    for sel in _PHONE_INPUTS:
        loc = _first_locator(page, sel)
        if loc is None:
            continue
        try:
            loc.click(timeout=3000)
            loc.fill("")
            loc.press_sequentially(value, delay=20)
            try:
                return loc.input_value() or ""
            except Exception:
                return value
        except Exception:
            continue
    return ""


_PHONE_INPUTS = (
    "input#wb-phone-number",
    "input[data-test-id='field_phone_input']",
    "input[placeholder='000 000-00-00']",
    "input.FieldInput.phone",
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phoneNumber']",
    "input[autocomplete='tel']",
    "input[inputmode='tel']",
)
# numeric на WB ID — это телефон (#wb-phone-number), не код из СМС.
_CODE_INPUTS = (
    "input[autocomplete='one-time-code']",
    "input[name='code']",
    "input[name='otp']",
    "input[inputmode='numeric']:not(#wb-phone-number):not([data-test-id='field_phone_input'])",
)
# «Код не принят» — только если сайт прямо так пишет и форма кода ещё на экране.
_CODE_ERROR_MARKERS = (
    "неверный код",
    "неправильный код",
    "код неверный",
    "код не подходит",
    "код истёк",
    "код истек",
    "ошибка кода",
    "введён неверный код",
    "введен неверный код",
)


def _page_text(info) -> str:
    info = info or {}
    return _norm_text((info.get("title") or "") + "\n" + (info.get("text") or ""))


def is_wb_consent(info) -> bool:
    """Экран «Принять / Отклонить» после кода WB. Это не ошибка кода и не форма входа."""
    url = ((info or {}).get("url") or "")
    path = (urlparse(url).path or "").lower()
    if "/oauth2/authorize" in path:
        return True
    text = _page_text(info)
    return "принять" in text and "отклонить" in text


def code_was_rejected(info) -> bool:
    """Форма кода осталась и WB прямо пишет, что код неверный."""
    if is_wb_consent(info) or classify_page(info or {}) != "code":
        return False
    text = _page_text(info)
    return any(marker in text for marker in _CODE_ERROR_MARKERS)


def on_wildberries(url) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host.endswith("wildberries.ru") and "id.wb.ru" not in host


_WB_CABINET_PREFIXES = ("/lk/myorders", "/lk/details", "/lk/myrefunds", "/lk/favorites")
_OZON_CABINET_PREFIXES = ("/my/order", "/my/returns", "/my/favorites", "/my/settings")
# Одна планка и для «Готово, я вошёл», и для чтения. «Заказы» и «Доставки» тоже кабинет.
_CABINET_MARKERS = ("заказ", "доставк", "профиль", "личн", "покупк", "возврат", "избран")


def on_ozon(url) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host.endswith("ozon.ru") and not host.startswith("id.")


def buyer_cabinet_path(url) -> bool:
    path = (urlparse(url or "").path or "").lower()
    if on_wildberries(url):
        return any(path.startswith(prefix) for prefix in _WB_CABINET_PREFIXES)
    if on_ozon(url):
        return any(path.startswith(prefix) for prefix in _OZON_CABINET_PREFIXES)
    return False


def login_redirect(info) -> bool:
    """Явный вход: форма телефона, кода или пароля. Проверка браузера сюда не входит."""
    info = info or {}
    if is_wb_consent(info):
        return False
    return classify_page(info) in ("login", "code", "password")


def cabinet_open(info) -> bool:
    """Кабинет покупателя. Та же функция при «Готово» и при чтении заказов."""
    if is_wb_consent(info):
        return False
    kind = classify_page(info or {})
    if kind in ("offline", "challenge", "pending", "login", "code", "password", "public"):
        return False
    if kind == "cabinet":
        return True
    if not buyer_cabinet_path((info or {}).get("url") or ""):
        return False
    text = _page_text(info)
    return any(marker in text for marker in _CABINET_MARKERS)


def interpret_after_code(shop, info) -> str:
    """consent, cabinet, rejected, wait или other. rejected — только текст ошибки на форме кода."""
    info = info or {}
    if shop == "wb":
        if is_wb_consent(info):
            return "consent"
        if on_wildberries(info.get("url") or "") and cabinet_open(info):
            return "cabinet"
        if code_was_rejected(info):
            return "rejected"
        if classify_page(info) == "code":
            return "wait"
        return "other"
    if classify_page(info) == "cabinet":
        return "cabinet"
    if classify_page(info) == "code":
        return "rejected"
    return "other"


def is_accept_button(label: str) -> bool:
    name = re.sub(r"\s+", " ", (label or "")).strip().casefold()
    return name == "принять"


def click_accept(page) -> bool:
    """Только кнопка «Принять». «Отклонить» не нажимаем."""
    for frame in _frames(page):
        for role in ("button", "link"):
            try:
                loc = frame.get_by_role(role)
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 40)):
                item = loc.nth(i)
                try:
                    name = item.inner_text(timeout=1000) or ""
                except Exception:
                    continue
                if not is_accept_button(name):
                    continue
                try:
                    item.click(timeout=4000)
                    return True
                except Exception:
                    continue
    return False


def classify_page(info: dict) -> str:
    url = (info.get("url") or "")
    path = (urlparse(url).path or "").lower()
    host = (urlparse(url).hostname or "").lower()
    text = _norm_text((info.get("title") or "") + "\n" + (info.get("text") or ""))
    if "/product/" in path or path.endswith("detail.aspx") or ("/catalog/" in path and not path.startswith("/lk") and not path.startswith("/my")):
        return "public"
    # «Нет соединения» часто остаётся на /my/orderlist. Это антибот, не кабинет.
    if any(s in text for s in _OFFLINE_MARKERS):
        return "offline"
    # «Проверяем браузер» на WB — ещё идёт проверка, это не отказ.
    if "проверяем браузер" in text:
        return "pending"
    if any(s in text for s in _CHALLENGE_MARKERS):
        return "challenge"
    if any(s in text for s in _CODE_MARKERS):
        return "code"
    if any(s in text for s in ("введите пароль", "пароль от аккаунта")):
        return "password"
    login_host = "id.ozon.ru" in host or "id.wb.ru" in host
    login_url = any(s in url.lower() for s in ("/security/login", "/signin")) or login_host
    login_text = any(s in text for s in _LOGIN_TEXT)
    cabinet = path.startswith("/my") or path.startswith("/lk")
    proof = any(s in text for s in ("мои заказы", "покупки", "номер заказа", "профиль", "личные данные", "мои данные"))
    # «Получить код» на форме телефона WB — ещё не ввод кода. Голый «код» форму кода не делает.
    if login_host or login_text:
        return "login"
    if login_url and ("телефон" in text or "получить код" in text):
        return "login"
    if cabinet and proof:
        return "cabinet"
    return "unknown"


def page_face(info) -> str:
    """Короткая пометка для журнала: антибот это или форма входа."""
    if is_wb_consent(info):
        return "согласие"
    kind = classify_page(info or {})
    text = _norm_text(((info or {}).get("title") or "") + "\n" + ((info or {}).get("text") or ""))
    if kind == "pending":
        return "проверка браузера"
    if kind == "offline":
        return "антибот: нет соединения"
    if kind == "challenge":
        if "доступ ограничен" in text:
            return "антибот: доступ ограничен"
        return "антибот"
    return {
        "login": "форма входа",
        "code": "форма кода",
        "password": "форма пароля",
        "cabinet": "кабинет",
        "public": "не кабинет",
    }.get(kind, "не форма входа")


class PlaywrightCabinet:
    """Один браузер на процесс. Контексты — с одним и тем же прокси, без ротации."""

    def __init__(self):
        self._worker = SerialWorker(self._watch_code_pages)
        self._pw = None
        self._browser = None
        self._live = {}
        self._watch = {}
        self._manual = None
        self._warm_read = None
        self._hints = build_hints("131.0.0.0")

    def exit_ip(self, proxy) -> str:
        return self._worker.call(self._exit_ip, proxy)

    def begin_login(self, shop, phone, proxy) -> dict:
        return self._worker.call(self._begin_login, shop, phone, proxy, timeout=180)

    def code_page_open(self, shop) -> bool:
        return bool(self._worker.call(lambda: shop in self._live, timeout=10))

    def submit_code(self, shop, code, proxy) -> dict:
        return self._worker.call(self._submit_code, shop, code, proxy, timeout=90)

    def close_login(self, shop):
        return self._worker.call(self._close_login, shop, timeout=20)

    def read(self, shop, section, proxy, storage, order_id="") -> dict:
        return self._worker.call(self._read, shop, section, proxy, storage, order_id, timeout=180)

    def cancel_order(self, shop, order_id, proxy, storage) -> dict:
        return self._worker.call(self._cancel, shop, order_id, proxy, storage, timeout=70)

    def manual_open(self, proxy, shop="wb") -> dict:
        timeout = 300 if shop == "ozon" else 60
        return self._worker.call(self._manual_open, proxy, shop, timeout=timeout)

    def manual_frame(self):
        return self._worker.call(self._manual_frame, timeout=15)

    def manual_input(self, payload) -> dict:
        return self._worker.call(self._manual_input, payload, timeout=20)

    def manual_finish(self, proxy) -> dict:
        return self._worker.call(self._manual_finish, proxy, timeout=90)

    def manual_cancel(self) -> dict:
        return self._worker.call(self._manual_cancel, timeout=20)

    def _custom_chromium(self) -> str:
        for key in ("CHROMIUM_EXECUTABLE", "CHROMIUM_PATH"):
            env = (os.environ.get(key) or "").strip()
            if env and os.path.isfile(env):
                return env
        return ""

    def _close_main_browser(self):
        self._drop_warm()
        browser = self._browser
        self._browser = None
        self._browser_proxy = None
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    def _close_patchright(self):
        browser = getattr(self, "_prt_browser", None)
        self._prt_browser = None
        self._prt_proxy = None
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    def _ensure_browser(self, proxy=None):
        sig = proxy_hash(proxy) if proxy else ""
        if self._browser is not None:
            try:
                if self._browser.is_connected() and getattr(self, "_browser_proxy", "") == sig:
                    return self._browser
            except Exception:
                self._browser = None
        self._close_patchright()
        self._close_main_browser()
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**chromium_launch_kwargs(proxy, self._custom_chromium()))
        self._browser_proxy = sig
        self._hints = build_hints(getattr(self._browser, "version", "") or "")
        _log("browser started " + self._hints["version"])
        return self._browser

    def _context_kwargs(self, proxy, storage=None, mode="android", shop="wb") -> dict:
        if shop == "ozon":
            hints = ozon_hints((self._hints or {}).get("version") or "", mode)
            kw = dict(PIXEL7_PROFILE if mode == "android" else DESKTOP_PROFILE)
            kw["user_agent"] = hints["user_agent"]
            kw["extra_http_headers"] = dict(hints["headers"])
        elif mode == "android":
            kw = dict(PIXEL7_PROFILE)
            kw["user_agent"] = self._hints["user_agent"]
            kw["extra_http_headers"] = dict(self._hints["headers"])
        else:
            kw = {}
        if storage:
            kw["storage_state"] = storage
        return kw

    def _apply_hints(self, context, page):
        meta = self._hints["metadata"]
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": self._hints["user_agent"],
                "acceptLanguage": "ru-RU,ru",
                "platform": "Linux armv8l",
                "userAgentMetadata": meta,
            })
            cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        except Exception as e:
            _log("client hints " + type(e).__name__)

    def _hint_fail(self, page, exc):
        msg = str(exc or "").replace("\n", " ")[:200]
        line = "ozon hints " + type(exc).__name__ + ": " + msg
        if ozon_same_session_error(exc):
            if getattr(page, "_jarvis_same_session_logged", False):
                return
            try:
                page._jarvis_same_session_logged = True
            except Exception:
                pass
            _log(line + " — тот же процесс, UA страницы не меняем")
            return
        _log(line)

    def _note_doc_headers(self, page, request):
        """Сравнивает UA и sec-ch-ua документа входа с главной. Cookie не пишет."""
        try:
            if str(getattr(request, "resource_type", "") or "") != "document":
                return
            url = getattr(request, "url", "") or ""
            headers = getattr(request, "headers", None) or {}
        except Exception:
            return
        ua = ozon_ua_signature(headers)
        fetch = ozon_fetch_signature(headers)
        frame = getattr(request, "frame", None)
        main = frame is None or getattr(frame, "parent_frame", None) is None
        login = bool(ozon_login_top(url))
        if main and not login:
            try:
                page._jarvis_doc_ua = ua
                page._jarvis_doc_fetch = fetch
            except Exception:
                pass
            return
        if not login:
            return
        base = getattr(page, "_jarvis_doc_ua", "") or ""
        if base and ua == base:
            verdict = "UA и sec-ch-ua как у главной"
        elif base:
            verdict = "UA или sec-ch-ua отличаются от главной"
        else:
            verdict = "заголовки главной ещё не сняты"
        where = "главный документ" if main else "фрейм"
        summary = verdict + " · " + where
        if fetch:
            summary += " · " + fetch
        shop = getattr(getattr(page, "context", None), "_jarvis_shop", "") or ""
        detail = ""
        if base or ua:
            detail = "главная: " + (base or "—") + "\nвход: " + (ua or "—")
            home_fetch = getattr(page, "_jarvis_doc_fetch", "") or ""
            if home_fetch or fetch:
                detail += "\nsec-fetch главная: " + (home_fetch or "—") + "\nsec-fetch вход: " + (fetch or "—")
        self._manual_note(shop, url, summary, "заголовки", apis=detail)

    def _apply_ozon_hints(self, context, page, mode):
        """UA страницы. Фрейм того же процесса не трогаем: отдельной сессии у него нет."""
        payload = ozon_cdp((self._hints or {}).get("version") or "", mode)
        bound = set()
        same = set()

        def apply_target(target):
            if target is None:
                try:
                    cdp = context.new_cdp_session(target)
                    cdp.send("Emulation.setUserAgentOverride", payload)
                except Exception as e:
                    self._hint_fail(page, e)
                return
            marker = id(target)
            if marker in bound or marker in same:
                return
            try:
                cdp = context.new_cdp_session(target)
            except Exception as e:
                if ozon_same_session_error(e):
                    same.add(marker)
                self._hint_fail(page, e)
                return
            bound.add(marker)
            try:
                cdp.send("Emulation.setUserAgentOverride", payload)
            except Exception as e:
                self._hint_fail(page, e)

        apply_target(page)
        if page is None:
            return
        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            frames = []
        main = getattr(page, "main_frame", None)
        for frame in frames:
            if frame is page or frame is main or getattr(frame, "parent_frame", None) is None:
                continue
            apply_target(frame)
        if not getattr(page, "_jarvis_hint_watch", False):
            try:
                page._jarvis_hint_watch = True
            except Exception:
                pass
            if hasattr(page, "on"):
                def on_request(request, page=page):
                    self._note_doc_headers(page, request)

                def on_frame(frame):
                    if getattr(frame, "parent_frame", None) is None:
                        return
                    apply_target(frame)

                try:
                    page.on("request", on_request)
                    page.on("frameattached", on_frame)
                except Exception as e:
                    _log("ozon frame " + type(e).__name__ + ": " + str(e)[:160])

    def _prepare_context(self, context, shop, mode):
        try:
            context._jarvis_shop = shop
            context._jarvis_mode = mode
        except Exception:
            pass
        if hide_webdriver(shop, mode):
            context.add_init_script(_WEBDRIVER_SCRIPT)
        if shop == "ozon":
            def on_page(page, mode=mode):
                self._apply_ozon_hints(context, page, mode)
            try:
                context.on("page", on_page)
            except Exception:
                pass
        try:
            context.set_default_navigation_timeout(35000)
        except Exception:
            pass

    def _new_context(self, proxy, storage=None, mode="android", shop="wb"):
        if mode == "patchright":
            return self._patchright_context(proxy, storage)
        browser = self._ensure_browser(proxy)
        context = browser.new_context(**self._context_kwargs(proxy, storage, mode, shop))
        self._prepare_context(context, shop, mode)
        return context

    def _patchright_context(self, proxy, storage):
        from patchright.sync_api import sync_playwright
        sig = proxy_hash(proxy) if proxy else ""
        self._close_main_browser()
        if getattr(self, "_prt", None) is None:
            self._prt = sync_playwright().start()
        alive = False
        browser = getattr(self, "_prt_browser", None)
        if browser is not None and getattr(self, "_prt_proxy", "") == sig:
            try:
                alive = browser.is_connected()
            except Exception:
                alive = False
        if not alive:
            self._close_patchright()
            self._prt_browser = self._prt.chromium.launch(**chromium_launch_kwargs(proxy, self._custom_chromium()))
            self._prt_proxy = sig
            version = getattr(self._prt_browser, "version", "") or ""
            if version:
                self._hints = build_hints(version)
        context = self._prt_browser.new_context(**self._context_kwargs(proxy, storage, "patchright", shop="ozon"))
        self._prepare_context(context, "ozon", "patchright")
        return context

    def _open(self, context, mode="android"):
        page = context.new_page()
        shop = getattr(context, "_jarvis_shop", "wb")
        mode = getattr(context, "_jarvis_mode", mode)
        if shop == "ozon":
            self._apply_ozon_hints(context, page, mode)
        elif mode == "android":
            self._apply_hints(context, page)
        return page

    def _settle(self, page):
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass

    def _snapshot(self, page) -> dict:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        try:
            text = page.evaluate(
                """() => {
                  const main = document.querySelector('main');
                  const mainText = main && main.innerText ? main.innerText.trim() : '';
                  const body = document.body;
                  const bodyText = body && body.innerText ? body.innerText.trim() : '';
                  const t = mainText || bodyText;
                  return t.replace(/\\n{3,}/g, '\\n\\n').slice(0, 8000);
                }"""
            )
        except Exception:
            text = ""
        return {"url": getattr(page, "url", "") or "", "title": (title or "")[:200], "text": text or ""}

    def _drop(self, context):
        try:
            if context:
                context.close()
        except Exception:
            pass

    def _close_login(self, shop):
        self._watch.pop(shop, None)
        context = self._live.pop(shop, None)
        if shop == "wb":
            self._save_wb_abt(_proxy(), context)
        self._drop(context)
        warm = self._warm_read or {}
        if warm.get("shop") == shop:
            self._drop_warm()

    def _save_wb_abt(self, proxy, context):
        """Cookies антибота WB между попытками. Сессия кабинета сюда не пишется."""
        if not proxy or context is None:
            return
        state = getattr(context, "storage_state", None)
        if not callable(state):
            return
        try:
            save_abt(proxy, "wb", state())
        except Exception as e:
            _log("abt save wb " + type(e).__name__)

    def _release_login(self, shop, proxy, context, keep=False):
        if shop == "wb":
            self._save_wb_abt(proxy, context)
        if not keep:
            self._drop(context)

    def _wb_preloader(self, page) -> bool:
        try:
            return bool(page.evaluate(
                """() => {
                  const el = document.querySelector('.general-preloader, .j-initial-preloader');
                  if (!el) return false;
                  const st = window.getComputedStyle(el);
                  return st.display !== 'none' && st.visibility !== 'hidden';
                }"""
            ))
        except Exception:
            return False

    def _await_wb_shell(self, page):
        """До 25 с на оболочку главной. Часы проверки входа здесь не идут."""
        started = time.time()
        info = self._snapshot(page)
        while time.time() - started < WB_SHELL_S:
            if self._phone_ready(page) or wb_on_id(info):
                break
            # Баннер «Лови лучшие цены» остаётся и на уже открытой главной. Ждём сам спиннер.
            if not self._wb_preloader(page):
                break
            try:
                page.wait_for_timeout(400)
            except Exception:
                time.sleep(0.4)
            info = self._snapshot(page)
        return info, time.time() - started

    def _click_fresh_code(self, page) -> bool:
        """Жмёт первую отправку кода. «Получить новый код» на странице ожидания не трогает."""
        for frame in _frames(page):
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role)
                    n = loc.count()
                except Exception:
                    continue
                for i in range(min(n, 40)):
                    try:
                        name = loc.nth(i).inner_text(timeout=1000) or ""
                    except Exception:
                        continue
                    if not is_fresh_code_request(name):
                        continue
                    try:
                        loc.nth(i).click(timeout=4000)
                        return True
                    except Exception:
                        continue
        return False

    def _watch_code_pages(self):
        """Между задачами смотрит открытую страницу кода. Сам на /security/login не уходит и код заново не шлёт."""
        self._manual_session()
        now = time.time()
        for shop in list(self._live):
            meta = self._watch.get(shop) or {}
            until = meta.get("until") or 0
            started = until - CODE_WAIT_S if until else 0
            if not until or code_wait_action(now - started) == "expire":
                self._close_login(shop)
                _set_shop(
                    shop, "none", storage=None,
                    why="Истекло ожидание кода (5 минут), страница закрыта. Сессии нет.",
                )
                continue
            context = self._live.get(shop)
            pages = getattr(context, "pages", None) or []
            if not pages:
                continue
            info = self._snapshot(pages[0])
            sig = page_sig(info)
            if sig == meta.get("sig"):
                continue
            meta["sig"] = sig
            self._watch[shop] = meta
            note_step(
                shop, "page", info.get("url") or "", page_snippet(info),
                "страница изменилась", title=info.get("title") or "", shot=self._shot(pages[0]),
            )

    def _exit_ip(self, proxy) -> str:
        context = self._new_context(proxy)
        page = self._open(context)
        last = "нет ответа"
        try:
            for url in _IP_URLS:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    ip = parse_ip_body(page.inner_text("body"))
                    if ip:
                        return ip
                    last = "пустой ответ"
                except Exception as e:
                    last = type(e).__name__
            raise RuntimeError("сервис IP не ответил через прокси (" + last + ")")
        finally:
            self._drop(context)

    def _shot(self, page) -> str:
        try:
            blob = page.screenshot(type="jpeg", quality=32, full_page=False)
        except Exception:
            return ""
        return save_jpeg(blob)

    def _page_step(self, page, step, result, info=None) -> dict:
        info = info or self._snapshot(page)
        return {
            "step": step,
            "url": info.get("url") or "",
            "title": (info.get("title") or "")[:120],
            "summary": page_snippet(info, 160),
            "face": page_face(info),
            "result": result,
            "shot": self._shot(page),
        }

    def _phone_ready(self, page) -> bool:
        return _first_locator(page, ", ".join(_PHONE_INPUTS)) is not None

    def _await_surface(self, page, seconds) -> dict:
        """Ждём поле телефона или стену антибота. «Проверяем браузер» сюда не входит."""
        deadline = time.time() + seconds
        info = self._snapshot(page)
        while time.time() < deadline:
            if classify_page(info) in ("offline", "challenge") or self._phone_ready(page):
                return info
            try:
                page.wait_for_timeout(400)
            except Exception:
                time.sleep(0.4)
            info = self._snapshot(page)
        return info

    def _await_wb(self, page):
        """До 40 с с момента вызова: проверка браузера и оболочка не отказ."""
        started = time.time()
        info = self._snapshot(page)
        while wb_keep_waiting(info, time.time() - started, self._phone_ready(page)):
            try:
                page.wait_for_timeout(400)
            except Exception:
                time.sleep(0.4)
            info = self._snapshot(page)
        return info, time.time() - started

    def _goto_logged(self, page, url, shop, label):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            info = self._snapshot(page)
            if not str(info.get("url") or "").startswith("http"):
                raise
            _log(shop + " " + label + " " + type(e).__name__)

    def _wb_login_round(self, page, steps, profile, phone):
        """Путь, который в 19:23 дошёл до id.wb.ru: /security/login, затем 40 с от этой страницы."""
        self._goto_logged(page, _CABINET_URLS["wb"]["login"], "wb", "login")
        self._settle(page)
        opened = self._snapshot(page)
        steps.append(self._page_step(page, "opened", profile, opened))
        _log_page("wb", opened, mask_phone_value(phone))
        info, elapsed = self._await_wb(page)
        ready = self._phone_ready(page)
        spinning = (not ready) and (wb_shell(info) or self._wb_preloader(page))
        steps.append(self._page_step(page, "check", wb_wait_line(elapsed, info, ready, shell=spinning), info))
        return info, elapsed, ready

    def _warm_summary(self, mode, result) -> str:
        marks = [str(ev.get("at")) for ev in result.get("events") or [] if ev.get("action") == "reload"]
        bits = [mode]
        if marks:
            bits.append("перезагрузка " + " и ".join(marks) + " с")
        bits.append("главная открылась" if result.get("ok") else "главная не открылась")
        if result.get("ok"):
            bits.append("пауза 3 с")
        return " · ".join(bits)

    def _warm_ozon(self, proxy):
        """Главная тем же режимом, что прошёл в прошлый раз, иначе android → desktop → patchright."""
        steps = []
        last_info = {"url": OZON_HOME, "title": "", "text": ""}
        for mode in ozon_mode_order(load_ozon_mode()):
            context = None
            try:
                context = self._new_context(proxy, load_abt(proxy, mode), mode, shop="ozon")
                page = self._open(context, mode)
                try:
                    page.goto(OZON_HOME, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    _log("ozon home " + mode + " " + type(e).__name__)
                result = ozon_warm_pass(
                    lambda: self._snapshot(page),
                    lambda: page.reload(wait_until="domcontentloaded", timeout=30000),
                    time.sleep,
                )
                info = result.get("info") or self._snapshot(page)
                last_info = info
                steps.append(self._page_step(page, "warm", self._warm_summary(mode, result), info))
                if result.get("ok"):
                    try:
                        save_abt(proxy, mode, ozon_cookie_state(context.storage_state()))
                    except Exception as e:
                        _log("abt save " + mode + " " + type(e).__name__)
                    remember_ozon_mode(mode)
                    return context, page, steps
                forget_abt(proxy, mode)
            except Exception as e:
                _log("ozon warm " + mode + " " + type(e).__name__)
                forget_abt(proxy, mode)
                steps.append({
                    "step": "warm", "result": mode + " · не запустился",
                    "url": OZON_HOME, "title": "", "summary": mode + " · " + type(e).__name__,
                    "face": "", "shot": "",
                })
            self._drop(context)
        return None, None, steps

    def _with_page(self, decision, info, steps) -> dict:
        decision = dict(decision)
        decision.setdefault("url", (info or {}).get("url") or "")
        decision.setdefault("title", (info or {}).get("title") or "")
        decision.setdefault("summary", page_snippet(info))
        decision.setdefault("face", page_face(info))
        decision["steps"] = steps
        return decision

    def _blocked_result(self, info, steps) -> dict:
        return self._with_page({"ok": False, "status": "blocked", "error": BLOCKED_ERROR}, info, steps)

    def _begin_login(self, shop, phone, proxy) -> dict:
        if shop in self._live:
            info = {}
            ctx = self._live.get(shop)
            pages = getattr(ctx, "pages", None) or []
            if pages:
                info = self._snapshot(pages[0])
            return self._with_page({
                "ok": False,
                "kept": True,
                "status": "awaiting_code",
                "error": _code_wait_block(shop),
            }, info, [])
        self._close_login(shop)
        context = None
        steps = []
        try:
            if shop == "ozon":
                context, page, warm_steps = self._warm_ozon(proxy)
                steps.extend(warm_steps)
                if context is None:
                    info = steps[-1] if steps else {"url": OZON_HOME, "title": "", "text": ""}
                    modes = ", ".join(ozon_mode_order(load_ozon_mode()))
                    decision = self._blocked_result(info if isinstance(info, dict) else {"url": OZON_HOME}, steps)
                    decision["error"] = "Главная Ozon не открылась. Режимы: " + modes + ". Если стена осталась, смените IP прокси и войдите ещё раз."
                    return decision
                try:
                    page.goto(_CABINET_URLS[shop]["login"], wait_until="domcontentloaded", timeout=40000)
                except Exception as e:
                    info = self._snapshot(page)
                    if not str(info.get("url") or "").startswith("http"):
                        raise
                    _log(shop + " goto " + type(e).__name__)
                self._settle(page)
                info = self._snapshot(page)
                steps.append(self._page_step(page, "opened", "opened", info))
                _log_page(shop, info, mask_phone_value(phone))
                if classify_page(info) in ("offline", "challenge"):
                    self._drop(context)
                    return self._blocked_result(info, steps)
                if classify_page(info) == "cabinet":
                    self._drop(context)
                    return self._with_page({
                        "ok": True,
                        "status": "awaiting_code",
                        "message": "Кабинет на экране, но код из СМС не вводился. Сессия не активна.",
                    }, info, steps)
                info = self._await_surface(page, 20)
                if classify_page(info) in ("offline", "challenge"):
                    steps.append(self._page_step(page, "opened", "blocked", info))
                    self._drop(context)
                    return self._blocked_result(info, steps)
                if self._phone_ready(page):
                    steps.append(self._page_step(page, "button", "поле телефона", info))
                else:
                    clicked = _click_label(page, [r"^войти$", r"войти", r"^вход$"])
                    steps.append(self._page_step(page, "button", "нажали вход" if clicked else "кнопки входа нет", info))
                    info = self._await_surface(page, 12)
                    if classify_page(info) in ("offline", "challenge"):
                        self._drop(context)
                        return self._blocked_result(info, steps)
                if not self._phone_ready(page):
                    steps.append(self._page_step(page, "phone", "поле не найдено", info))
                    self._drop(context)
                    return self._with_page(
                        {"ok": False, "error": "Поле телефона не найдено. Вход не выполнен."},
                        info, steps,
                    )
            else:
                saved = load_abt(proxy, "wb")
                profile = "cookies антибота: сохранённые" if saved else "cookies антибота: новый профиль"
                context = self._new_context(proxy, saved)
                page = self._open(context)
                self._goto_logged(page, WB_HOME, "wb", "home")
                home, shell_elapsed = self._await_wb_shell(page)
                spinning = wb_shell(home) or self._wb_preloader(page)
                steps.append(self._page_step(
                    page, "home",
                    wb_wait_line(shell_elapsed, home, self._phone_ready(page), shell=spinning),
                    home,
                ))
                if self._phone_ready(page) and wb_on_id(home):
                    info, elapsed, ready = home, 0, True
                    steps.append(self._page_step(page, "check", wb_wait_line(0, home, True), home))
                else:
                    info, elapsed, ready = self._wb_login_round(page, steps, profile, phone)
                    # Вторую проверку на 40 с не запускаем, если всё ещё «Проверяем браузер».
                    if (
                        not ready and not wb_on_id(info) and classify_page(info) != "pending"
                        and self._wb_preloader(page)
                    ):
                        home, shell_elapsed = self._await_wb_shell(page)
                        spinning = wb_shell(home) or self._wb_preloader(page)
                        steps.append(self._page_step(
                            page, "home",
                            wb_wait_line(shell_elapsed, home, self._phone_ready(page), shell=spinning),
                            home,
                        ))
                        info, elapsed, ready = self._wb_login_round(page, steps, profile, phone)
                if classify_page(info) in ("offline", "challenge"):
                    steps.append(self._page_step(page, "opened", "blocked", info))
                    self._save_wb_abt(proxy, context)
                    self._drop(context)
                    return self._blocked_result(info, steps)
                if ready:
                    mark_wb_check(True)
                    steps.append(self._page_step(page, "button", "поле телефона", info))
                elif not wb_on_id(info):
                    row = mark_wb_check(False)
                    err = wb_check_message(elapsed, row.get("fails") or 0)
                    steps.append(self._page_step(page, "phone", err, info))
                    self._save_wb_abt(proxy, context)
                    self._drop(context)
                    return self._with_page({"ok": False, "error": err}, info, steps)
                else:
                    steps.append(self._page_step(page, "phone", "поле не найдено", info))
                    self._save_wb_abt(proxy, context)
                    self._drop(context)
                    return self._with_page(
                        {"ok": False, "error": "Поле телефона не найдено. Вход не выполнен."},
                        info, steps,
                    )
            got = _type_phone(page, phone)
            info = self._snapshot(page)
            steps.append(self._page_step(page, "phone", "введён " + (mask_phone_value(got) or "пусто"), info))
            national = _phone_digits(phone)[-10:]
            if not got or not phone_landed(national, got):
                self._release_login(shop, proxy, context)
                return self._with_page({
                    "ok": False,
                    "error": (
                        f"В форму ушёл номер {mask_phone_value(got) or 'пусто'}, "
                        f"ждали {mask_phone_value(national)}. "
                        + login_fail_message(shop, info)
                    ),
                }, info, steps)
            self._click_fresh_code(page)
            self._settle(page)
            try:
                page.wait_for_selector(", ".join(_CODE_INPUTS), timeout=8000)
            except Exception:
                pass
            info = self._snapshot(page)
            if classify_page(info) in ("offline", "challenge"):
                steps.append(self._page_step(page, "wait_code", "blocked", info))
                self._release_login(shop, proxy, context)
                return self._blocked_result(info, steps)
            code_field = _first_locator(page, ", ".join(_CODE_INPUTS)) is not None
            decision = decide_after_phone(shop, info, code_field)
            steps.append(self._page_step(page, "wait_code", decision.get("status") or "stopped", info))
            _log_page(shop, info, mask_phone_value(phone), mask_phone_value(got))
            if decision.get("ok") and decision.get("status") == "awaiting_code":
                self._live[shop] = context
                self._watch[shop] = {"until": time.time() + CODE_WAIT_S, "sig": page_sig(info)}
                self._release_login(shop, proxy, context, keep=True)
                return self._with_page(decision, info, steps)
            self._release_login(shop, proxy, context)
            return self._with_page(decision, info, steps)
        except Exception:
            self._release_login(shop, proxy, context)
            raise

    def _manual_session(self):
        session = self._manual
        if not session:
            return None
        if time.time() - float(session.get("touched") or 0) >= MANUAL_IDLE_S:
            self._close_manual("timeout")
            return None
        return session

    def _close_manual_context(self):
        session = self._manual
        self._manual = None
        _set_manual_open(False)
        if session:
            self._drop(session.get("context"))

    def _close_manual(self, reason):
        session = self._manual
        if not session:
            _set_manual_open(False)
            return False
        shop = session.get("shop") if session.get("shop") in SHOPS else "wb"
        self._close_manual_context()
        if reason == "timeout":
            note_step(shop, "manual", "", "10 минут без действий", "ручной вход отменён")
        elif reason == "cancel":
            note_step(shop, "manual", "", "ручной вход отменён", "ручной вход отменён")
        return True

    def _manual_open(self, proxy, shop="wb") -> dict:
        sid = shop if shop in SHOPS else ""
        if not sid:
            return {"ok": False, "error": "Неизвестный магазин"}
        current = self._manual_session()
        if current is not None:
            if current.get("shop") not in (None, "", sid):
                return {"ok": False, "error": "Уже открыт ручной вход в другой магазин."}
            current["shop"] = sid
            current["touched"] = time.time()
            _set_manual_open(True, sid)
            vp = PIXEL7_PROFILE["viewport"]
            return {"ok": True, "already": True, "width": vp["width"], "height": vp["height"]}
        context = None
        try:
            if sid == "ozon":
                context, page, warm_steps = self._warm_ozon(proxy)
                for item in warm_steps:
                    note_step(
                        sid, item.get("step") or "warm", item.get("url") or "",
                        _step_summary(item), item.get("result") or "",
                        title=item.get("title") or "", shot=item.get("shot") or "",
                    )
                if context is None or page is None:
                    self._drop(context)
                    self._manual = None
                    _set_manual_open(False)
                    modes = ", ".join(ozon_mode_order(load_ozon_mode()))
                    return {
                        "ok": False,
                        "error": "Главная Ozon не открылась. Режимы: " + modes + ". Если стена осталась, смените IP прокси и откройте ещё раз.",
                    }
                mode = getattr(context, "_jarvis_mode", "android")
                profile = PIXEL7_PROFILE if mode == "android" else DESKTOP_PROFILE
                start = getattr(page, "url", "") or OZON_HOME
            else:
                context = self._new_context(proxy, load_abt(proxy, "wb"))
                page = self._open(context)
                start = _CABINET_URLS["wb"]["login"]
                self._goto_logged(page, start, sid, "manual")
                profile = PIXEL7_PROFILE
            self._manual = {"context": context, "page": page, "touched": time.time(), "shop": sid}
            self._watch_manual(page, sid)
            if hasattr(context, "on"):
                try:
                    context.on("page", lambda popup, sid=sid: self._watch_manual(popup, sid))
                except Exception as e:
                    _log("manual watch page " + type(e).__name__)
            _set_manual_open(True, sid)
            vp = profile["viewport"]
            return {
                "ok": True,
                "width": vp["width"],
                "height": vp["height"],
                "url": getattr(page, "url", "") or start,
            }
        except Exception as e:
            _log("manual open " + type(e).__name__)
            self._drop(context)
            self._manual = None
            _set_manual_open(False)
            return {"ok": False, "error": "Ручной вход не открылся."}

    def _manual_frame(self):
        session = self._manual_session()
        if session is None:
            return b""
        try:
            blob = session["page"].screenshot(type="jpeg", quality=40, full_page=False)
        except Exception:
            return b""
        if isinstance(blob, (bytes, bytearray)) and bytes(blob).startswith(b"\xff\xd8"):
            return bytes(blob)
        return b""

    def _watch_manual(self, page, shop):
        if page is None or getattr(page, "_jarvis_manual_watch", False):
            return
        try:
            page._jarvis_manual_watch = True
        except Exception:
            pass
        if not hasattr(page, "on"):
            return
        trace = ManualTrace(shop)
        try:
            page.on("framenavigated", trace.on_frame)
            page.on("response", trace.on_response)
        except Exception as e:
            _log("manual watch " + type(e).__name__)

    def _touch_tap(self, page, pt):
        """Касание по очереди: пауза и радиус пальца. Playwright шлёт start и end одновременно."""
        x, y = float(pt[0]), float(pt[1])
        point = {"x": x, "y": y, "radiusX": 8, "radiusY": 8, "force": 0.5, "id": 1}
        context = getattr(page, "context", None)
        if context is not None and hasattr(context, "new_cdp_session"):
            try:
                cdp = context.new_cdp_session(page)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point], "modifiers": 0})
                time.sleep(0.07)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": [], "modifiers": 0})
                return "touch"
            except Exception as e:
                _log("touch tap " + type(e).__name__)
        touch = getattr(page, "touchscreen", None)
        if touch is not None and hasattr(touch, "tap"):
            touch.tap(x, y)
            return "touch"
        page.mouse.click(x, y)
        return "mouse"

    def _mouse_click(self, page, pt):
        page.mouse.move(pt[0], pt[1])
        page.mouse.down()
        time.sleep(0.05)
        page.mouse.up()
        return "mouse"

    def _manual_tap(self, page, pt, touch=True):
        if touch:
            return self._touch_tap(page, pt)
        return self._mouse_click(page, pt)

    def _manual_note(self, shop, url, summary, result, title="", apis=""):
        if shop not in SHOPS:
            return
        try:
            note_step(shop, "manual", url or "", summary, result, title=title, apis=apis)
        except Exception as e:
            _log("manual note " + type(e).__name__)

    def _promote_ozon_login(self, page) -> str:
        """Фрейм /ozonid-lite того же сайта. Открываем его переходом вкладки, не iframe."""
        main = getattr(page, "main_frame", None)
        for url in (getattr(page, "url", "") or "", getattr(main, "url", "") or ""):
            if ozon_login_top(url):
                return ""
        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            return ""
        top = ""
        for frame in frames:
            if frame is page or frame is main or getattr(frame, "parent_frame", None) is None:
                continue
            top = ozon_login_top(getattr(frame, "url", "") or "")
            if top:
                break
        if not top:
            return ""
        try:
            page.goto(top, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            _log("ozon login top " + type(e).__name__ + ": " + str(e)[:160])
            return ""
        return top

    def _manual_glance(self, page, shop):
        self._glance_wait(page)
        if shop == "ozon":
            top = self._promote_ozon_login(page)
            if not top and self._blank_child_frame(page):
                self._glance_wait(page, 0.4)
                top = self._promote_ozon_login(page)
            if top:
                self._manual_note(shop, top, "вместо фрейма", "вход")
        try:
            info = self._snapshot(page)
        except Exception:
            return
        kind = classify_page(info)
        if kind not in ("offline", "challenge", "login"):
            return
        self._manual_note(shop, info.get("url") or "", page_face(info), "экран", title=info.get("title") or "")

    def _glance_wait(self, page, seconds=None):
        delay = MANUAL_TAP_LOOK if seconds is None else seconds
        try:
            page.wait_for_timeout(int(delay * 1000))
        except Exception:
            time.sleep(delay)

    def _blank_child_frame(self, page) -> bool:
        main = getattr(page, "main_frame", None)
        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            return False
        for frame in frames:
            if frame is page or frame is main or getattr(frame, "parent_frame", None) is None:
                continue
            url = str(getattr(frame, "url", "") or "")
            if not url or url == "about:blank" or url.startswith("about:"):
                return True
        return False

    def _manual_input(self, payload) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        session = self._manual_session()
        if session is None:
            return {"ok": False, "error": "Ручной вход не открыт."}
        action = str(payload.get("action") or "")
        page = session["page"]
        shop = session.get("shop") if session.get("shop") in SHOPS else ""
        mode = getattr(session.get("context"), "_jarvis_mode", "") or ""
        try:
            if action == "click":
                pt = manual_point(payload.get("x"), payload.get("y"), payload.get("viewW"), payload.get("viewH"))
                if pt is None:
                    return {"ok": False, "error": "Нет координат."}
                how = self._manual_tap(page, pt, manual_uses_touch(shop, mode))
                self._manual_note(shop, getattr(page, "url", "") or "", how or "", "тап")
                self._manual_glance(page, shop)
            elif action == "scroll":
                pt = manual_point(payload.get("x"), payload.get("y"), payload.get("viewW"), payload.get("viewH"))
                dy = manual_delta(payload.get("dy"), payload.get("viewH"))
                if pt is None or dy is None:
                    return {"ok": False, "error": "Нет координат."}
                page.mouse.move(pt[0], pt[1])
                page.mouse.wheel(0, dy)
            elif action == "type":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 120:
                    return {"ok": False, "error": "Нечего вводить."}
                page.keyboard.type(text)
            elif action == "key":
                key = _MANUAL_KEYS.get(str(payload.get("key") or "").strip().casefold())
                if not key:
                    return {"ok": False, "error": "Неизвестная клавиша."}
                page.keyboard.press(key)
            elif action == "back":
                page.go_back(wait_until="domcontentloaded", timeout=20000)
            elif action == "reload":
                page.reload(wait_until="domcontentloaded", timeout=20000)
            else:
                return {"ok": False, "error": "Неизвестная команда."}
        except Exception as e:
            _log("manual " + action + " " + type(e).__name__)
            return {"ok": False, "error": "Команда не выполнена."}
        session["touched"] = time.time()
        return {"ok": True}

    def _manual_prove(self, page, shop):
        info = self._snapshot(page)
        if cabinet_open(info):
            return info
        for section in ("orders", "profile"):
            try:
                page.goto(_CABINET_URLS[shop][section], wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                _log("manual cabinet " + type(e).__name__)
            self._settle(page)
            info = self._snapshot(page)
            if cabinet_open(info):
                return info
        return info

    def _manual_finish(self, proxy) -> dict:
        session = self._manual_session()
        if session is None:
            return {"ok": False, "error": "Ручной вход не открыт."}
        shop = session.get("shop") if session.get("shop") in SHOPS else "wb"
        page = session["page"]
        context = session["context"]
        info = self._manual_prove(page, shop)
        url = (info or {}).get("url") or ""
        if not cabinet_open(info):
            note_step(shop, "manual", url, "кабинет не открылся", "кабинет не открылся")
            session["touched"] = time.time()
            return {"ok": False, "error": "Кабинет не открылся. Сессия не активна. Экран ещё открыт."}
        try:
            storage = context.storage_state()
        except Exception:
            storage = None
        if not storage_usable(storage):
            note_step(shop, "manual", url, "кабинет открылся, сессия не сохранилась", "кабинет не открылся")
            return {"ok": False, "error": "Кабинет открылся, но сессия не сохранилась."}
        self._close_manual_context()
        self._close_login(shop)
        if shop == "ozon":
            mode = getattr(context, "_jarvis_mode", "") or load_ozon_mode() or "android"
            if mode not in OZON_MODES:
                mode = "android"
            save_abt(proxy, mode, ozon_cookie_state(storage))
        else:
            save_abt(proxy, "wb", storage)
        _set_shop(shop, "active", storage=storage, auth="manual", why="ручной вход завершён")
        note_step(shop, "manual", url, "ручной вход завершён", "кабинет открылся")
        return {"ok": True, "status": "active", "message": "Сессия сохранена."}

    def _manual_cancel(self) -> dict:
        if not self._manual:
            _set_manual_open(False)
            return {"ok": True, "closed": False}
        self._close_manual("cancel")
        return {"ok": True, "closed": True}

    def _fill_code(self, page, code) -> bool:
        for frame in _frames(page):
            try:
                loc = frame.locator(", ".join(_CODE_INPUTS))
                n = loc.count()
            except Exception:
                continue
            if n <= 0:
                continue
            try:
                if n == 1:
                    loc.first.fill(code)
                    return True
                digits = re.sub(r"\D", "", code)
                for i, ch in enumerate(digits[:n]):
                    loc.nth(i).fill(ch)
                return True
            except Exception:
                continue
        return False

    def _pause(self, page, ms=400):
        try:
            page.wait_for_timeout(ms)
        except Exception:
            time.sleep(ms / 1000)

    def _watch_after_code(self, page, shop, seconds, steps) -> dict:
        """Ждёт уход с формы кода. Пока форма на месте и ошибки нет — это не «код не принят»."""
        deadline = time.time() + seconds
        info = self._snapshot(page)
        last = None
        while time.time() < deadline:
            action = interpret_after_code(shop, info)
            sig = page_sig(info)
            if sig != last:
                last = sig
                if action == "wait":
                    if not steps or steps[-1].get("result") != "форма кода на месте":
                        steps.append(self._page_step(page, "code", "форма кода на месте", info))
                elif action not in ("consent", "cabinet", "rejected"):
                    steps.append(self._page_step(page, "code", "страница изменилась", info))
            if action in ("consent", "cabinet", "rejected"):
                return info
            self._pause(page)
            info = self._snapshot(page)
        return info

    def _decide_after_code(self, shop, context, page, steps, info) -> dict:
        if shop == "wb" and is_wb_consent(info):
            return self._enter_after_consent(shop, context, page, steps, info)
        kind = classify_page(info)
        if kind in ("offline", "challenge"):
            _log_page(shop, info)
            steps.append(self._page_step(page, "code", "blocked", info))
            return self._blocked_result(info, steps)
        action = interpret_after_code(shop, info)
        if action == "cabinet":
            try:
                storage = context.storage_state()
            except Exception:
                storage = None
            steps.append(self._page_step(page, "cabinet", "active", info))
            self._close_login(shop)
            return self._with_page({
                "ok": True, "status": "active", "authenticated": True, "auth": "code",
                "storage": storage, "message": "Сессия сохранена.",
            }, info, steps)
        if action == "rejected":
            steps.append(self._page_step(page, "code", "код не принят", info))
            return self._with_page({
                "ok": False, "status": "awaiting_code",
                "error": "Код не принят. Можно ввести его ещё раз или начать вход заново.",
            }, info, steps)
        if action == "wait":
            if not steps or steps[-1].get("result") != "форма кода на месте":
                steps.append(self._page_step(page, "code", "форма кода на месте", info))
            return self._with_page({
                "ok": False, "status": "awaiting_code",
                "error": "Форма кода ещё открыта. Если код неверный, на странице будет текст ошибки.",
            }, info, steps)
        steps.append(self._page_step(page, "code", "кабинет не открылся", info))
        return self._with_page(
            {"ok": False, "error": "После кода кабинет не открылся. Вход не выполнен."},
            info, steps,
        )

    def _enter_after_consent(self, shop, context, page, steps, info) -> dict:
        """На экране согласия жмём только «Принять» и проверяем кабинет на wildberries.ru."""
        steps.append(self._page_step(page, "consent", "экран согласия", info))
        clicked = click_accept(page)
        info = self._snapshot(page)
        steps.append(self._page_step(page, "consent", "нажали принять" if clicked else "кнопки «Принять» нет", info))
        if not clicked:
            return self._with_page({
                "ok": False, "status": "awaiting_code",
                "error": "На экране согласия нет кнопки «Принять». Код уже отправлен, новый не запрашиваем.",
            }, info, steps)
        deadline = time.time() + CONSENT_REDIRECT_S
        last = page_sig(info)
        while time.time() < deadline:
            info = self._snapshot(page)
            sig = page_sig(info)
            if sig != last:
                last = sig
                steps.append(self._page_step(page, "redirect", "страница изменилась", info))
            if on_wildberries(info.get("url") or "") or cabinet_open(info):
                break
            self._pause(page)
        else:
            info = self._snapshot(page)
        if not on_wildberries(info.get("url") or ""):
            steps.append(self._page_step(page, "redirect", "редиректа на wildberries.ru нет", info))
            return self._with_page({
                "ok": False, "status": "awaiting_code",
                "error": "После «Принять» редиректа на wildberries.ru не было. Сессия не активна.",
            }, info, steps)
        if not cabinet_open(info):
            for section in ("orders", "profile"):
                try:
                    page.goto(_CABINET_URLS[shop][section], wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    _log(shop + " cabinet " + section + " " + type(e).__name__)
                self._settle(page)
                info = self._snapshot(page)
                opened = cabinet_open(info)
                steps.append(self._page_step(
                    page, section, "кабинет открылся" if opened else "кабинет не открылся", info,
                ))
                if opened:
                    break
        if not cabinet_open(info):
            return self._with_page({
                "ok": False, "status": "awaiting_code",
                "error": "После согласия кабинет не открылся. Сессия не активна.",
            }, info, steps)
        try:
            storage = context.storage_state()
        except Exception:
            storage = None
        steps.append(self._page_step(page, "cabinet", "active", info))
        self._close_login(shop)
        return self._with_page({
            "ok": True, "status": "active", "authenticated": True, "auth": "code",
            "storage": storage, "message": "Сессия сохранена.",
        }, info, steps)

    def _submit_code(self, shop, code, proxy) -> dict:
        context = self._live.get(shop)
        if context is None:
            return {"ok": False, "lost": True, "error": "Окно ввода кода закрыто — начните вход заново."}
        page = context.pages[0] if context.pages else self._open(context)
        steps = []
        info = self._snapshot(page)
        if shop == "wb" and is_wb_consent(info):
            return self._enter_after_consent(shop, context, page, steps, info)
        if not self._fill_code(page, code):
            info = self._snapshot(page)
            if shop == "wb" and is_wb_consent(info):
                return self._enter_after_consent(shop, context, page, steps, info)
            if classify_page(info) != "code":
                steps.append(self._page_step(page, "code", "форма кода уже закрыта", info))
                info = self._watch_after_code(page, shop, CODE_AFTER_S, steps)
                return self._decide_after_code(shop, context, page, steps, info)
            steps.append(self._page_step(page, "code", "поле кода не найдено", info))
            return self._with_page(
                {"ok": False, "status": "awaiting_code", "error": "Поле кода не найдено. Вход не выполнен."},
                info, steps,
            )
        if not (shop == "wb" and is_wb_consent(self._snapshot(page))):
            _click_label(page, [r"^подтвердить$", r"подтвердить", r"^войти$", r"готово", r"продолжить"])
        self._settle(page)
        info = self._snapshot(page)
        if interpret_after_code(shop, info) not in ("consent", "cabinet", "rejected"):
            info = self._watch_after_code(page, shop, CODE_AFTER_S, steps)
        return self._decide_after_code(shop, context, page, steps, info)

    def _drop_warm(self):
        warm = self._warm_read
        self._warm_read = None
        if warm:
            self._drop(warm.get("context"))

    def _block_heavy(self, context):
        """Картинки и видео обрываем, шрифт закрываем пустым ответом. script и xhr не трогаем."""
        def handle(route):
            try:
                kind = getattr(getattr(route, "request", None), "resource_type", "") or ""
                if kind == "font":
                    route.fulfill(status=200, content_type="font/woff2", body=b"")
                    return
                if is_heavy(kind):
                    route.abort("blockedbyclient")
                    return
                route.fallback()
            except Exception:
                try:
                    route.fallback()
                except Exception:
                    pass
        try:
            context.route(HEAVY_GLOBS, handle)
        except Exception as e:
            _log("route " + type(e).__name__)

    def _storage_sig(self, storage) -> str:
        cookies = (storage or {}).get("cookies") if isinstance(storage, dict) else None
        parts = []
        for item in cookies or []:
            if isinstance(item, dict):
                parts.append(str(item.get("name") or "") + ":" + str(item.get("domain") or "") + ":" + str(item.get("value") or ""))
        raw = "|".join(sorted(parts))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _borrow_wb_page(self, proxy, storage):
        """Один контекст WB между вопросами, пока те же прокси и cookies. Картинки не грузим."""
        sig = ("wb", proxy_hash(proxy), self._storage_sig(storage))
        warm = self._warm_read or {}
        page = warm.get("page")
        alive = False
        if warm.get("sig") == sig and page is not None and time.time() - float(warm.get("used") or 0) < 600:
            try:
                alive = not page.is_closed()
            except Exception:
                alive = False
        if alive:
            warm["used"] = time.time()
            return page, warm.get("tap")
        self._drop_warm()
        context = self._new_context(proxy, storage)
        self._block_heavy(context)
        page = self._open(context)
        tap = ApiSink()
        try:
            page.on("response", tap.add)
        except Exception as e:
            _log("listen " + type(e).__name__)
        self._warm_read = {
            "sig": sig, "shop": "wb", "context": context, "page": page,
            "tap": tap, "used": time.time(),
        }
        return page, tap

    def _wait_wb_read(self, page, tap):
        """Пока нет списка заказов — ждём. Справочник ПВЗ и спиннер оболочки чтением не считаются."""
        started = time.time()
        info = self._snapshot(page)
        while time.time() - started < WB_READ_S:
            captures = list(getattr(tap, "rows", None) or [])
            parsed = rows_from_captures(captures)
            info = self._snapshot(page)
            kind = classify_page(info)
            if kind in ("offline", "challenge", "login", "code", "password"):
                return parsed, info
            if parsed or wb_list_settled(captures) or cabinet_open(info):
                return parsed, info
            try:
                page.wait_for_timeout(400)
            except Exception:
                time.sleep(0.4)
        captures = list(getattr(tap, "rows", None) or [])
        return rows_from_captures(captures), self._snapshot(page)

    def _read_wb(self, section, proxy, storage, order_id) -> dict:
        key = "orders" if section == "order" else section
        url = _CABINET_URLS["wb"][key]
        try:
            page, tap = self._borrow_wb_page(proxy, storage)
        except Exception as e:
            _log("wb read open " + type(e).__name__)
            return {"ok": False, "session": "public", "url": url, "title": "", "text": "", "text_len": 0, "apis": "", "shot": ""}
        if tap is not None:
            tap.rows.clear()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            _log("wb read goto " + type(e).__name__)
        if order_id:
            self._focus_order(page, order_id)
        parsed, info = self._wait_wb_read(page, tap)
        if order_id:
            own = [row for row in parsed if order_id in (row.get("id") or "")]
            if own:
                parsed = own
        captures = list(getattr(tap, "rows", None) or [])
        page_text = info.get("text") or ""
        structured = format_wb_rows(parsed)
        if not structured and wb_list_settled(captures):
            if key in ("orders", "order"):
                structured = "Заказов нет."
            elif key == "delivery":
                structured = "Доставок нет."
            else:
                structured = "Возвратов нет."
        text = structured or page_text
        shot = ""
        try:
            shot = self._shot(page)
        except Exception:
            shot = ""
        base = session_from_page(info)
        if (parsed or structured) and base.get("session") not in ("blocked", "expired"):
            base["ok"] = True
            base["session"] = "active"
        base["title"] = info.get("title") or ""
        base["text"] = text
        base["text_len"] = len(page_text)
        base["apis"] = api_journal(captures)
        base["shot"] = shot
        if not base.get("ok"):
            if base.get("session") in ("blocked", "expired") and base.get("error"):
                _log_page("wb", info)
            if base.get("session") in ("blocked", "expired", "login"):
                self._drop_warm()
            return base
        if order_id and section == "order" and order_id not in (text + (info.get("url") or "")):
            return {
                "ok": False, "session": "active", "error": "Карточка этого заказа не открылась.",
                "url": info.get("url") or "", "title": info.get("title") or "", "text": text,
                "text_len": len(page_text), "apis": base["apis"], "shot": shot,
            }
        _log("wb read " + key + " lines " + str(len(parsed)) + " api " + str(len(captures)))
        return {
            "ok": True, "session": "active", "url": info.get("url") or url,
            "title": info.get("title") or "", "text": text,
            "text_len": len(page_text), "apis": base["apis"], "shot": shot,
        }

    def _goto_section(self, page, shop, section, order_id):
        url = _CABINET_URLS[shop][section]
        if section == "order" and shop == "ozon":
            url = url.format(id=order_id)
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        self._settle(page)
        if order_id and not (shop == "ozon" and section == "order"):
            self._focus_order(page, order_id)

    def _focus_order(self, page, order_id):
        for frame in _frames(page):
            try:
                loc = frame.get_by_text(order_id, exact=False)
                if loc.count():
                    loc.first.click(timeout=3000)
                    self._settle(page)
                    return
            except Exception:
                continue

    def _read(self, shop, section, proxy, storage, order_id) -> dict:
        if shop == "wb" and section in _READ_TRACE:
            return self._read_wb(section, proxy, storage, order_id)
        context = None
        page = None
        if shop == "ozon":
            context, page, warm_steps = self._warm_ozon(proxy)
            for item in warm_steps:
                note_step(
                    shop, item.get("step") or "warm", item.get("url") or "",
                    _step_summary(item), item.get("result") or "",
                    title=item.get("title") or "", shot=item.get("shot") or "",
                )
            if context is None:
                modes = ", ".join(ozon_mode_order(load_ozon_mode()))
                return {
                    "ok": False, "session": "blocked", "url": OZON_HOME,
                    "title": "", "text": "главная не открылась", "text_len": 0, "apis": "", "shot": "",
                    "error": "Главная Ozon не открылась. Режимы: " + modes + ". Если стена осталась, смените IP прокси и войдите ещё раз.",
                }
        else:
            context = self._new_context(proxy, storage)
            page = self._open(context)
        tap = None
        if section in _READ_TRACE:
            tap = ApiSink()
            try:
                page.on("response", tap.add)
            except Exception as e:
                _log("listen " + type(e).__name__)
        try:
            self._goto_section(page, shop, section, order_id)
            info = self._snapshot(page)
            base = session_from_page(info)
            page_text = info.get("text") or ""
            base["title"] = info.get("title") or ""
            base["text"] = page_text
            base["text_len"] = len(page_text)
            base["apis"] = api_journal(getattr(tap, "rows", None) or [])
            if not base.get("ok"):
                base["shot"] = self._shot(page)
                if base.get("session") in ("blocked", "expired") and base.get("error"):
                    _log_page(shop, info)
                return base
            if order_id and section == "order" and order_id not in (page_text + (info.get("url") or "")):
                return {
                    "ok": False, "session": "active", "error": "Карточка этого заказа не открылась.",
                    "url": info.get("url") or "", "title": info.get("title") or "", "text": page_text,
                    "text_len": len(page_text), "apis": base["apis"], "shot": self._shot(page),
                }
            out = {"ok": True, "session": "active", "url": info["url"], "title": info["title"], "text": page_text, "text_len": len(page_text), "apis": base["apis"]}
            if section in _READ_TRACE:
                out["shot"] = self._shot(page)
            return out
        finally:
            self._drop(context)

    def _cancel(self, shop, order_id, proxy, storage) -> dict:
        context = None
        if shop == "ozon":
            context, page, warm_steps = self._warm_ozon(proxy)
            for item in warm_steps:
                note_step(
                    shop, item.get("step") or "warm", item.get("url") or "",
                    _step_summary(item), item.get("result") or "",
                    title=item.get("title") or "", shot=item.get("shot") or "",
                )
            if context is None:
                modes = ", ".join(ozon_mode_order(load_ozon_mode()))
                return {
                    "ok": False, "cancelled": False, "session": "blocked",
                    "error": "Главная Ozon не открылась. Режимы: " + modes + ". Если стена осталась, смените IP прокси и войдите ещё раз.",
                    "url": OZON_HOME, "text": "главная не открылась",
                }
        else:
            context = self._new_context(proxy, storage)
            page = self._open(context)
        try:
            self._goto_section(page, shop, "order", order_id)
            info = self._snapshot(page)
            base = session_from_page(info)
            if not base.get("ok"):
                out = {"ok": False, "cancelled": False, "session": base.get("session")}
                if base.get("error"):
                    out["error"] = base["error"]
                return out
            blob = (info.get("text") or "") + (info.get("url") or "")
            if order_id not in blob:
                return {"ok": False, "cancelled": False, "session": "active", "error": "Этот заказ на странице не найден."}
            before = _CANCELLED_RE.search(info.get("text") or "")
            if not _click_label(page, [r"отменить\s+заказ"]):
                return {"ok": False, "cancelled": False, "session": "active", "error": "На странице нет кнопки «Отменить заказ»."}
            self._settle(page)
            _click_label(page, [r"да,?\s*отменить", r"подтвердить отмену", r"отменить заказ"])
            self._settle(page)
            after = self._snapshot(page)
            base = session_from_page(after)
            if not base.get("ok") and base.get("session") in ("expired", "blocked"):
                out = {"ok": False, "cancelled": False, "session": "expired"}
                if base.get("error"):
                    out["error"] = base["error"]
                return out
            if _CANCELLED_RE.search(after.get("text") or "") and not before:
                return {"ok": True, "cancelled": True, "session": "active", "text": after.get("text") or "", "url": after.get("url") or ""}
            return {
                "ok": False, "cancelled": False, "session": "active",
                "error": "Кнопка отмены нажата, но страница не подтвердила, что заказ отменён.",
            }
        finally:
            self._drop(context)
