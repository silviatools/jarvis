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
         "Личные кабинеты покупателя Ozon и Wildberries: заказы, карточка заказа, доставка, возвраты, "
         "избранное и краткий профиль (телефон маской). Это не кабинет продавца и не цены с витрины. "
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
     "description": "Заказы в личном кабинете покупателя Ozon или Wildberries. Не витрина и не кабинет продавца. Нужны прокси и живая сессия.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP}, "required": ["shop"]}},
    {"name": "market_order",
     "description": "Карточка одного заказа в кабинете покупателя. order_id — номер заказа со страницы заказов.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP, "order_id": _STR}, "required": ["shop", "order_id"]}},
    {"name": "market_delivery",
     "description": "Доставка в кабинете покупателя: что в пути и статусы. Можно сузить до order_id.",
     "input_schema": {"type": "object", "properties": {"shop": _SHOP, "order_id": _STR}, "required": ["shop"]}},
    {"name": "market_returns",
     "description": "Возвраты в личном кабинете покупателя Ozon или Wildberries.",
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
    "Инструменты: market_orders, market_order, market_delivery, market_returns, market_favorites, market_profile "
    "(чтение) и market_cancel_order (меняет кабинет). shop — ozon или wb. Читать можно без спроса. "
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

# Профиль, который проходил антибот Ozon (Variti) на мобильном прокси: Pixel 7.
PIXEL7_PROFILE = {
    "viewport": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
}

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
        "orders": "https://www.wildberries.ru/lk/myorders/delivery",
        "order": "https://www.wildberries.ru/lk/myorders/delivery",
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
# Страница кода WB остаётся открытой, пока человек не введёт код или не выйдут 5 минут.
CODE_WAIT_S = 300
# После ввода кода страница может уйти на согласие OAuth не сразу. Это не отказ кода.
CODE_AFTER_S = 20
CONSENT_REDIRECT_S = 25
WB_CODE_HINT = "Код в приложении Wildberries (уведомление или колокольчик)"
_AUTH_OK = ("code", "password")
_BOOT = secrets.token_hex(8)
_TIMER_RE = re.compile(r"\d{1,2}:\d{2}")
_lock = threading.Lock()
_driver = None
_logs = []


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
    if "://" not in text:
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


def proxy_hash(proxy) -> str:
    raw = "|".join(str((proxy or {}).get(k) or "") for k in ("scheme", "host", "port", "username", "password"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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


def ozon_home_clear(info) -> bool:
    """Заголовок с ozon и без стены антибота. Тело тоже смотрим на метки блокировки."""
    title = _norm_text((info or {}).get("title") or "")
    blob = title + "\n" + _norm_text((info or {}).get("text") or "")
    if "ozon" not in title:
        return False
    if any(s in blob for s in _OFFLINE_MARKERS) or any(s in blob for s in _CHALLENGE_MARKERS):
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
        sleep(0.4)


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
    _set_shop(sid, "none", phone_mask="", storage=None)
    _log(sid + " logout")
    return {"ok": True, "status": "none", "message": "Сессия удалена."}


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
    if result.get("session") in ("expired", "public") or kind in ("login", "code", "password", "public"):
        return "closed"
    if not result.get("ok") and result.get("session") != "active":
        return "closed"
    return ""


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
    why = "открылась не страница кабинета"
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
    failed = _apply_read_session(sid, result)
    if failed:
        return failed
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


def _append_journal(data, shop, step, url, summary, result, title="", shot=""):
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


def note_step(shop, step, url, summary, result, title="", shot=""):
    proxy = _proxy()
    summary = redact(_journal_text(summary), proxy)
    result = redact(_journal_text(result), proxy)
    title = redact(_journal_text(title), proxy)
    with _lock:
        data = _load()
        _append_journal(data, shop, step, url, summary, result, title=title, shot=shot)
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
    if kind in ("login", "code", "password"):
        return {"ok": False, "session": "expired", "url": url}
    if kind != "cabinet":
        return {"ok": False, "session": "public", "url": url}
    return {"ok": True, "session": "active", "url": url}


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


def cabinet_open(info) -> bool:
    """Кабинет покупателя: страница заказов или профиля, не вход и не согласие."""
    if is_wb_consent(info):
        return False
    kind = classify_page(info or {})
    if kind == "cabinet":
        return True
    if kind in ("offline", "challenge", "pending", "login", "code", "password", "public"):
        return False
    url = (info or {}).get("url") or ""
    path = (urlparse(url).path or "").lower()
    if not on_wildberries(url):
        return False
    if not (path.startswith("/lk/myorders") or path.startswith("/lk/details")):
        return False
    text = _page_text(info)
    return any(marker in text for marker in ("заказ", "доставк", "профиль", "личн", "покупк"))


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

    def _ensure_browser(self):
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:
                self._browser = None
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kwargs = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        exe = (os.environ.get("CHROMIUM_PATH") or "").strip()
        if exe and os.path.isfile(exe):
            kwargs["executable_path"] = exe
        self._browser = self._pw.chromium.launch(**kwargs)
        self._hints = build_hints(getattr(self._browser, "version", "") or "")
        _log("browser started " + self._hints["version"])
        return self._browser

    def _context_kwargs(self, proxy, storage=None, mode="android") -> dict:
        if mode == "android":
            kw = dict(PIXEL7_PROFILE)
            kw["user_agent"] = self._hints["user_agent"]
            kw["extra_http_headers"] = dict(self._hints["headers"])
        else:
            # Desktop и Patchright — без подмены UA, вьюпорта и Client Hints.
            kw = {}
        kw["proxy"] = _pw_proxy(proxy)
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

    def _chromium_exe(self) -> str:
        env = (os.environ.get("CHROMIUM_PATH") or "").strip()
        if env and os.path.isfile(env):
            return env
        root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
        if root and os.path.isdir(root):
            found = sorted(Path(root).glob("chromium-*/chrome-linux*/chrome"))
            if found:
                return str(found[-1])
        if os.path.isfile("/opt/google/chrome/chrome"):
            return "/opt/google/chrome/chrome"
        return ""

    def _new_context(self, proxy, storage=None, mode="android"):
        if mode == "patchright":
            return self._patchright_context(proxy, storage)
        browser = self._ensure_browser()
        context = browser.new_context(**self._context_kwargs(proxy, storage, mode))
        if mode == "android":
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});"
                "Object.defineProperty(navigator, 'language', {get: () => 'ru-RU'});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});"
            )
        context.set_default_navigation_timeout(35000)
        return context

    def _patchright_context(self, proxy, storage):
        from patchright.sync_api import sync_playwright
        if getattr(self, "_prt", None) is None:
            self._prt = sync_playwright().start()
        if getattr(self, "_prt_browser", None) is not None:
            try:
                if not self._prt_browser.is_connected():
                    self._prt_browser = None
            except Exception:
                self._prt_browser = None
        if getattr(self, "_prt_browser", None) is None:
            kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            exe = self._chromium_exe()
            if exe:
                kwargs["executable_path"] = exe
            self._prt_browser = self._prt.chromium.launch(**kwargs)
        context = self._prt_browser.new_context(**self._context_kwargs(proxy, storage, "patchright"))
        context.set_default_navigation_timeout(35000)
        return context

    def _open(self, context, mode="android"):
        page = context.new_page()
        if mode == "android":
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
                  const root = document.querySelector('main') || document.body;
                  const t = (root && root.innerText) ? root.innerText : '';
                  return t.replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 8000);
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
        """Сначала главная. Android, при отказе desktop без подмен, затем Patchright."""
        steps = []
        last_info = {"url": OZON_HOME, "title": "", "text": ""}
        for mode in OZON_MODES:
            context = None
            try:
                context = self._new_context(proxy, load_abt(proxy, mode), mode)
                page = self._open(context, mode)
                try:
                    page.goto(OZON_HOME, wait_until="domcontentloaded", timeout=40000)
                except Exception as e:
                    _log("ozon home " + mode + " " + type(e).__name__)
                result = ozon_warm_pass(
                    lambda: self._snapshot(page),
                    lambda: page.reload(wait_until="domcontentloaded", timeout=20000),
                    time.sleep,
                )
                info = result.get("info") or self._snapshot(page)
                last_info = info
                steps.append(self._page_step(page, "warm", self._warm_summary(mode, result), info))
                if result.get("ok"):
                    try:
                        save_abt(proxy, mode, context.storage_state())
                    except Exception as e:
                        _log("abt save " + mode + " " + type(e).__name__)
                    return context, page, steps
            except Exception as e:
                _log("ozon warm " + mode + " " + type(e).__name__)
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
                    return self._blocked_result(info if isinstance(info, dict) else {"url": OZON_HOME}, steps)
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
                return {
                    "ok": False, "session": "blocked", "url": OZON_HOME,
                    "title": "", "text": "главная не открылась", "error": BLOCKED_ERROR,
                }
        else:
            context = self._new_context(proxy, storage)
            page = self._open(context)
        try:
            self._goto_section(page, shop, section, order_id)
            info = self._snapshot(page)
            base = session_from_page(info)
            base["title"] = info.get("title") or ""
            base["text"] = info.get("text") or ""
            if not base.get("ok"):
                base["shot"] = self._shot(page)
                if base.get("session") in ("blocked", "expired") and base.get("error"):
                    _log_page(shop, info)
                return base
            if order_id and section == "order" and order_id not in ((info.get("text") or "") + (info.get("url") or "")):
                return {"ok": False, "session": "active", "error": "Карточка этого заказа не открылась.", "url": info.get("url") or "", "title": info.get("title") or "", "text": info.get("text") or ""}
            return {"ok": True, "session": "active", "url": info["url"], "title": info["title"], "text": info["text"]}
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
                return {
                    "ok": False, "cancelled": False, "session": "blocked",
                    "error": BLOCKED_ERROR, "url": OZON_HOME, "text": "главная не открылась",
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
