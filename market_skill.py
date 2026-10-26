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
инструмент прямо говорит, что нужен повторный вход и код из СМС.
"""

import json
import os
import queue
import re
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
}

SKILLS = [
    {"id": SKILL_ID, "title": "Маркетплейсы", "risk": "write", "default": True,
     "desc": (
         "Личные кабинеты покупателя Ozon и Wildberries: заказы, карточка заказа, доставка, возвраты, "
         "избранное и краткий профиль (телефон маской). Это не кабинет продавца и не цены с витрины. "
         "Нужен сохранённый мобильный HTTP(S)-прокси с липким IP (Настройки → Маркетплейсы) и живая сессия: "
         "без прокси кабинет с сервера не открывается, смена IP сессию сбрасывает. Если сессия слетела, "
         "инструменты так и скажут — нужен новый вход и код из СМС. Отмена заказа, если кнопка есть на странице, "
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
    "так и скажи, не выдумывай заказы. Новый вход — код из СМС в Настройки → Маркетплейсы. "
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
        "shops": {sid: {"status": "none", "phone_mask": "", "updated": 0, "storage": None} for sid in SHOPS},
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
        base["shops"][sid] = {
            "status": status,
            "phone_mask": str(raw.get("phone_mask") or "")[:16],
            "updated": int(raw.get("updated") or 0),
            "storage": storage,
        }
    return base


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


def _set_shop(shop, status, phone_mask=None, storage="__keep__"):
    with _lock:
        data = _load()
        row = _shop_row(data, shop)
        row["status"] = status if status in STATUS_LABEL else "none"
        if phone_mask is not None:
            row["phone_mask"] = phone_mask
        if storage != "__keep__":
            row["storage"] = storage
        row["updated"] = int(time.time())
        _write(data)


def _proxy():
    return _load().get("proxy")


def _storage(shop):
    return (_load()["shops"][shop] or {}).get("storage")


def _status(shop) -> str:
    return (_load()["shops"][shop] or {}).get("status") or "none"


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
    data = _load()
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
    return {
        "ok": True,
        "has_proxy": bool(proxy and proxy.get("host")),
        "proxy": mask_proxy(proxy),
        "shops": shops,
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
        message = "Прокси сохранён. Сессии сброшены: другой IP обрывает вход, войдите заново и введите код из СМС."
    return {"ok": True, "mask": mask_proxy(parsed), "message": message}


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
    return {
        "ok": True,
        "server_ip": server_ip,
        "exit_ip": exit_ip,
        "message": "Трафик выходит не с IP сервера.",
    }


def _no_proxy_error():
    return {"ok": False, "error": "Без сохранённого прокси вход не начнётся: с IP сервера кабинет не открывается."}


def begin_login(shop: str, phone: str) -> dict:
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 10:
        return {"ok": False, "error": "Введите номер телефона"}
    proxy = _proxy()
    if not proxy:
        return _no_proxy_error()
    try:
        result = get_driver().begin_login(sid, digits, proxy)
    except Exception as e:
        _log("login failed " + sid + " " + type(e).__name__)
        return {"ok": False, "error": "Вход не выполнен: " + redact(e, proxy)}
    return _finish_login(sid, digits, result, proxy)


def submit_code(shop: str, code: str) -> dict:
    sid = _known_shop(shop)
    if not sid:
        return {"ok": False, "error": "Неизвестный магазин"}
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{4,8}", code):
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


def _finish_login(sid, phone, result, proxy, code="") -> dict:
    result = result or {}
    status = result.get("status")
    if result.get("ok") is False or result.get("lost"):
        err = redact(result.get("error") or "Вход не выполнен.", proxy, [code])
        _log(sid + " login not finished")
        return {"ok": False, "error": err, "status": status or _status(sid)}
    if status == "active" and storage_usable(result.get("storage")):
        _set_shop(sid, "active", phone_mask=mask_phone_value(phone) or None, storage=result.get("storage"))
        try:
            get_driver().close_login(sid)
        except Exception as e:
            _log("close after login " + redact(e, proxy, [code]))
        _log(sid + " session active")
        return {"ok": True, "status": "active", "message": result.get("message") or "Сессия сохранена."}
    if status == "active":
        _log(sid + " cabinet without storage")
        return {"ok": False, "error": "Кабинет открылся, но сессия не сохранилась. Вход не засчитан."}
    if status == "awaiting_code":
        _set_shop(sid, "awaiting_code", phone_mask=mask_phone_value(phone) or None, storage=None)
        _log(sid + " awaiting code")
        return {"ok": True, "status": "awaiting_code", "message": result.get("message") or "Открылась форма кода. Введите код из СМС."}
    _log(sid + " login not finished")
    return {"ok": False, "error": redact(result.get("error") or "Вход не выполнен.", proxy, [code])}


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
        return {"error": "Вход не закончен: нужен код из СМС.", "status": status}
    if status == "expired":
        return {"error": "Сессия слетела, нужен повторный вход (код из СМС).", "status": "expired"}
    if status != "active" or not storage_usable(_storage(shop)):
        return {"error": "Нет сессии. Войдите в Настройки → Маркетплейсы (код из СМС).", "status": status or "none"}
    return {}


def _apply_read_session(shop, result) -> dict:
    flag = (result or {}).get("session")
    if flag == "expired":
        _set_shop(shop, "expired", storage=None)
        _log(shop + " session expired")
        return {"error": "Сессия слетела, нужен повторный вход (код из СМС).", "shop": shop, "status": "expired"}
    if flag == "challenge":
        return {"error": "Антибот не пустил в кабинет. Заказы не прочитаны.", "shop": shop}
    if flag == "public":
        return {"error": "Открылась не страница кабинета. Данные витрины не читаю.", "shop": shop}
    if not result.get("ok"):
        return {"error": result.get("error") or "Кабинет не открылся.", "shop": shop}
    return {}


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

    def __init__(self):
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="market-browser", daemon=True)
        self._started = False
        self._start_lock = threading.Lock()

    def _loop(self):
        while True:
            item = self._q.get()
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


_PHONE_INPUTS = (
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phoneNumber']",
    "input[autocomplete='tel']",
    "input[inputmode='tel']",
)
_CODE_INPUTS = (
    "input[autocomplete='one-time-code']",
    "input[name='code']",
    "input[name='otp']",
    "input[inputmode='numeric']",
)


def classify_page(info: dict) -> str:
    url = (info.get("url") or "")
    path = (urlparse(url).path or "").lower()
    host = (urlparse(url).hostname or "").lower()
    text = ((info.get("title") or "") + "\n" + (info.get("text") or "")).lower()
    if "/product/" in path or path.endswith("detail.aspx") or ("/catalog/" in path and not path.startswith("/lk") and not path.startswith("/my")):
        return "public"
    if any(s in text for s in ("variti", "не робот", "captcha", "доступ ограничен", "проверка браузера", "подтвердите, что вы человек")):
        return "challenge"
    if any(s in text for s in ("введите код", "код из смс", "код из sms", "код подтверждения")):
        return "code"
    login_url = any(s in url.lower() for s in ("/security/login", "/signin")) or "id.ozon.ru" in host
    login_text = any(s in text for s in ("введите номер телефона", "войти или зарегистрироваться", "войдите, чтобы"))
    cabinet = path.startswith("/my") or path.startswith("/lk")
    if login_url:
        return "code" if "код" in text else "login"
    if login_text and not cabinet:
        return "login"
    if cabinet and login_text and not any(s in text for s in ("мои заказы", "покупки", "доставк", "избранн", "возврат", "профиль")):
        return "login"
    if cabinet:
        return "cabinet"
    return "unknown"


def _cabinet_path(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.startswith("/my") or path.startswith("/lk")


class PlaywrightCabinet:
    """Один браузер на процесс. Контексты — с одним и тем же прокси, без ротации."""

    def __init__(self):
        self._worker = SerialWorker()
        self._pw = None
        self._browser = None
        self._live = {}
        self._hints = build_hints("131.0.0.0")

    def exit_ip(self, proxy) -> str:
        return self._worker.call(self._exit_ip, proxy)

    def begin_login(self, shop, phone, proxy) -> dict:
        return self._worker.call(self._begin_login, shop, phone, proxy, timeout=70)

    def submit_code(self, shop, code, proxy) -> dict:
        return self._worker.call(self._submit_code, shop, code, proxy, timeout=70)

    def close_login(self, shop):
        return self._worker.call(self._close_login, shop, timeout=20)

    def read(self, shop, section, proxy, storage, order_id="") -> dict:
        return self._worker.call(self._read, shop, section, proxy, storage, order_id, timeout=70)

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

    def _context_kwargs(self, proxy, storage=None) -> dict:
        kw = dict(PIXEL7_PROFILE)
        kw["user_agent"] = self._hints["user_agent"]
        kw["extra_http_headers"] = dict(self._hints["headers"])
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

    def _new_context(self, proxy, storage=None):
        browser = self._ensure_browser()
        context = browser.new_context(**self._context_kwargs(proxy, storage))
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});"
            "Object.defineProperty(navigator, 'language', {get: () => 'ru-RU'});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});"
        )
        context.set_default_navigation_timeout(35000)
        return context

    def _open(self, context):
        page = context.new_page()
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
        self._drop(self._live.pop(shop, None))

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

    def _begin_login(self, shop, phone, proxy) -> dict:
        self._close_login(shop)
        context = None
        try:
            context = self._new_context(proxy)
            page = self._open(context)
            page.goto(_CABINET_URLS[shop]["login"], wait_until="domcontentloaded", timeout=40000)
            self._settle(page)
            info = self._snapshot(page)
            kind = classify_page(info)
            if kind == "challenge":
                self._drop(context)
                return {"ok": False, "error": "Антибот не пропустил на страницу входа. Вход не выполнен."}
            if kind == "cabinet":
                storage = context.storage_state()
                self._drop(context)
                if storage_usable(storage):
                    return {"ok": True, "status": "active", "storage": storage, "message": "Кабинет уже открыт, сессия сохранена."}
                return {"ok": False, "error": "Кабинет открылся, но сессия не сохранилась. Вход не засчитан."}
            if not _fill_first(page, _PHONE_INPUTS, phone):
                _click_label(page, [r"войти", r"вход"])
                self._settle(page)
                if not _fill_first(page, _PHONE_INPUTS, phone):
                    self._drop(context)
                    return {"ok": False, "error": "Поле телефона не найдено. Вход не выполнен."}
            _click_label(page, [r"получить код", r"выслать код", r"отправить код", r"продолжить", r"далее", r"войти"])
            self._settle(page)
            try:
                page.wait_for_selector(", ".join(_CODE_INPUTS), timeout=8000)
            except Exception:
                pass
            info = self._snapshot(page)
            kind = classify_page(info)
            code_field = _first_locator(page, ", ".join(_CODE_INPUTS)) is not None
            if kind == "code" or code_field:
                self._live[shop] = context
                sent = any(s in (info.get("text") or "").lower() for s in ("отправлен", "выслал", "смс", "sms"))
                message = "Введите код из СМС." if sent else "Открылась форма кода. Если СМС пришла — введите код."
                return {"ok": True, "status": "awaiting_code", "message": message}
            if kind == "cabinet":
                storage = context.storage_state()
                self._drop(context)
                return {"ok": True, "status": "active", "storage": storage, "message": "Кабинет открыт, сессия сохранена."}
            self._drop(context)
            title = (info.get("title") or "").strip()[:80]
            return {"ok": False, "error": "Форма кода не появилась. Вход не выполнен." + (f" Страница: {title}." if title else "")}
        except Exception:
            self._drop(context)
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

    def _submit_code(self, shop, code, proxy) -> dict:
        context = self._live.get(shop)
        if context is None:
            return {"ok": False, "lost": True, "error": "Окно ввода кода закрыто — начните вход заново."}
        page = context.pages[0] if context.pages else self._open(context)
        if not self._fill_code(page, code):
            return {"ok": False, "status": "awaiting_code", "error": "Поле кода не найдено. Вход не выполнен."}
        _click_label(page, [r"подтвердить", r"войти", r"готово", r"продолжить"])
        self._settle(page)
        info = self._snapshot(page)
        kind = classify_page(info)
        if kind == "cabinet" or (_cabinet_path(info.get("url") or "") and kind not in ("login", "code", "challenge")):
            storage = context.storage_state()
            self._close_login(shop)
            return {"ok": True, "status": "active", "storage": storage, "message": "Сессия сохранена."}
        if kind == "challenge":
            return {"ok": False, "error": "Антибот не пропустил после кода. Вход не выполнен."}
        if kind == "code":
            return {"ok": False, "status": "awaiting_code", "error": "Код не принят. Можно ввести его ещё раз или начать вход заново."}
        return {"ok": False, "error": "После кода кабинет не открылся. Вход не выполнен."}

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
        context = self._new_context(proxy, storage)
        page = self._open(context)
        try:
            self._goto_section(page, shop, section, order_id)
            info = self._snapshot(page)
            kind = classify_page(info)
            if kind == "login":
                return {"ok": False, "session": "expired", "url": info["url"]}
            if kind == "challenge":
                return {"ok": False, "session": "challenge", "url": info["url"]}
            if kind == "public" or not _cabinet_path(info.get("url") or ""):
                return {"ok": False, "session": "public", "url": info["url"]}
            if order_id and section == "order" and order_id not in ((info.get("text") or "") + (info.get("url") or "")):
                return {"ok": False, "session": "active", "error": "Карточка этого заказа не открылась."}
            return {"ok": True, "session": "active", "url": info["url"], "title": info["title"], "text": info["text"]}
        finally:
            self._drop(context)

    def _cancel(self, shop, order_id, proxy, storage) -> dict:
        context = self._new_context(proxy, storage)
        page = self._open(context)
        try:
            self._goto_section(page, shop, "order", order_id)
            info = self._snapshot(page)
            kind = classify_page(info)
            if kind == "login":
                return {"ok": False, "cancelled": False, "session": "expired"}
            if kind == "challenge":
                return {"ok": False, "cancelled": False, "session": "challenge"}
            if kind == "public" or not _cabinet_path(info.get("url") or ""):
                return {"ok": False, "cancelled": False, "session": "public"}
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
            kind = classify_page(after)
            if kind == "login":
                return {"ok": False, "cancelled": False, "session": "expired"}
            if _CANCELLED_RE.search(after.get("text") or "") and not before:
                return {"ok": True, "cancelled": True, "session": "active", "text": after.get("text") or "", "url": after.get("url") or ""}
            return {
                "ok": False, "cancelled": False, "session": "active",
                "error": "Кнопка отмены нажата, но страница не подтвердила, что заказ отменён.",
            }
        finally:
            self._drop(context)
