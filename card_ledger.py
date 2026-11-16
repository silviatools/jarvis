"""
card_ledger.py — учёт денег на карте друга.

Друг отдаёт карту (например, казахстанскую), пользователь её пополняет и
платит с неё. Страница /card/<token> — отдельная, без входа на сайт, по той
же модели, что гостевая ссылка события /e/<token>: токен и есть пропуск,
остальной сайт по ней недоступен.

Остаток — не баланс банка, а свои деньги: активные пополнения минус активные
покупки. Сумма операции вводится в любой валюте из курса ЦБ РФ, в остаток
ложится рублёвый эквивалент. Курс берётся на дату операции и сохраняется в
самой записи: поздняя смена курса старые суммы не пересчитывает.

Файл DATA_DIR/jarvis_card_ledger.json входит в бэкап.
"""

import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

STORE_NAME = "jarvis_card_ledger.json"
# Совпадает с PLANNER_TOKEN_RE в jarvis.py: гостевой токен той же формы.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^[A-Z]{3}$")

MSK = timezone(timedelta(hours=3))
MONEY_Q = Decimal("0.01")
RATE_Q = Decimal("0.00000001")
VALUE_Q = Decimal("0.0001")
MAX_AMOUNT = Decimal("1000000000000")  # 1 трлн единиц валюты
MAX_ENTRIES = 2000
MAX_COMMENT = 500
MAX_AUTHOR = 60
MAX_TITLE = 80
MAX_NAME = 40
MAX_PAST_DAYS = 3660

PINNED = ("RUB", "USD", "EUR", "KZT", "AMD")
PINNED_NAMES = {
    "RUB": "Российский рубль",
    "USD": "Доллар США",
    "EUR": "Евро",
    "KZT": "Казахстанский тенге",
    "AMD": "Армянский драм",
}
KINDS = ("topup", "purchase")
CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp?date_req={dd}"

DEFAULT_TITLE = "Карта друга"
DEFAULT_OWNER = "Я"
DEFAULT_FRIEND = "Друг"

_lock = threading.RLock()


def store_path() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / STORE_NAME


def today_msk() -> date:
    return datetime.now(timezone.utc).astimezone(MSK).date()


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fetch_cbr(day: date) -> bytes:
    """Дневной XML курсов ЦБ РФ. Подменяется в тестах."""
    url = CBR_URL.format(dd=day.strftime("%d/%m/%Y"))
    req = Request(url, headers={"User-Agent": "jarvis-card-ledger"})
    try:
        with urlopen(req, timeout=12) as resp:
            return resp.read()
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        raise OSError(f"cbr: {e}") from e


def _dec(raw) -> Decimal | None:
    try:
        return Decimal(str(raw).replace("\u00a0", "").replace(" ", "").replace(",", ".").strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def parse_cbr(raw: bytes) -> dict:
    """Таблица «сколько рублей за 1 единицу». Пустая таблица — не кэшируем."""
    root = ET.fromstring(raw)
    cbr_date = ""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", str(root.attrib.get("Date") or ""))
    if m:
        cbr_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    table = {}
    for node in root.findall("Valute"):
        code = str(node.findtext("CharCode") or "").strip().upper()
        if not CODE_RE.match(code) or code == "RUB":
            continue
        nominal = _dec(node.findtext("Nominal"))
        value = _dec(node.findtext("Value"))
        name = " ".join(str(node.findtext("Name") or code).split())[:80] or code
        if nominal is None or value is None or nominal <= 0 or value < 0:
            continue
        value_q = value.quantize(VALUE_Q, rounding=ROUND_HALF_UP)
        rate = (value_q / nominal).quantize(RATE_Q, rounding=ROUND_HALF_UP)
        table[code] = {
            "rate": format(rate, "f"),
            "nominal": format(nominal.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f"),
            "value": format(value_q, "f"),
            "name": name,
        }
    return {"cbrDate": cbr_date, "table": table}


def money_str(amount: Decimal) -> str:
    return format(amount.quantize(MONEY_Q, rounding=ROUND_HALF_UP), "f")


def parse_amount(raw):
    """Сумма в единицах валюты, уже округлённая до копейки, или None."""
    text = str(raw if raw is not None else "").replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    if not text or not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    try:
        amount = Decimal(text).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    return amount


def parse_day(raw, today: date | None = None):
    """date, либо строка-код ошибки."""
    text = str(raw or "").strip()
    if not DATE_RE.match(text):
        return "Укажите дату."
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return "Укажите дату."
    today = today or today_msk()
    if day > today:
        return "Дата ещё не наступила."
    if day < today - timedelta(days=MAX_PAST_DAYS):
        return "Слишком старая дата."
    return day


def _clean(raw, limit: int) -> str:
    return " ".join(str(raw or "").split())[:limit].strip()


def _read() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    if not isinstance(data.get("rates"), dict):
        data["rates"] = {}
    return data


def _write(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _blank_ledger() -> dict:
    now = now_ms()
    return {
        "id": str(uuid.uuid4()),
        "title": DEFAULT_TITLE,
        "ownerName": DEFAULT_OWNER,
        "friendName": DEFAULT_FRIEND,
        "shareToken": secrets.token_hex(16),
        "shareEnabled": True,
        "createdAt": now,
        "updatedAt": now,
    }


def _ledger(data: dict) -> dict | None:
    led = data.get("ledger")
    return led if isinstance(led, dict) and led.get("shareToken") else None


def _ensure(data: dict) -> dict:
    led = _ledger(data)
    if led is None:
        led = _blank_ledger()
        data["ledger"] = led
    return led


def _find_open(data: dict, token: str) -> dict | None:
    """Активная ссылка. Выключенная неотличима от несуществующей."""
    if not TOKEN_RE.match(token or ""):
        return None
    led = _ledger(data)
    if led is None or str(led.get("shareToken") or "") != token:
        return None
    return led if led.get("shareEnabled", True) else None


def _rub_of(entry: dict) -> Decimal:
    try:
        return Decimal(str(entry.get("amountRub") or "0"))
    except (InvalidOperation, ValueError, ArithmeticError):
        return Decimal(0)


def totals(entries) -> dict:
    top = Decimal(0)
    spent = Decimal(0)
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("status") == "void":
            continue
        rub = _rub_of(entry)
        if entry.get("kind") == "purchase":
            spent += rub
        elif entry.get("kind") == "topup":
            top += rub
    top = top.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    spent = spent.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    return {
        "balanceRub": money_str(top - spent),
        "topupRub": money_str(top),
        "purchaseRub": money_str(spent),
    }


def _public_entry(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "kind": entry.get("kind"),
        "amount": entry.get("amount"),
        "currency": entry.get("currency"),
        "amountRub": entry.get("amountRub"),
        "rate": entry.get("rate"),
        "nominal": entry.get("nominal"),
        "rateValue": entry.get("rateValue"),
        "rateName": entry.get("rateName") or "",
        "rateDate": entry.get("rateDate") or "",
        "cbrDate": entry.get("cbrDate") or "",
        "rateSource": entry.get("rateSource") or "",
        "comment": entry.get("comment") or "",
        "authorName": entry.get("authorName") or "",
        "editedBy": entry.get("editedBy") or "",
        "status": "void" if entry.get("status") == "void" else "active",
        "voidedBy": entry.get("voidedBy") or "",
        "voidedAt": entry.get("voidedAt") or 0,
        "restoredBy": entry.get("restoredBy") or "",
        "date": entry.get("date") or "",
        "createdAt": entry.get("createdAt") or 0,
        "updatedAt": entry.get("updatedAt") or 0,
    }


def _sorted_entries(entries) -> list:
    good = [e for e in (entries or []) if isinstance(e, dict) and e.get("id")]
    good.sort(key=lambda e: (str(e.get("date") or ""), int(e.get("createdAt") or 0)), reverse=True)
    return [_public_entry(e) for e in good]


def _currency_options(table: dict | None) -> list:
    table = table or {}
    out = [{"code": "RUB", "name": PINNED_NAMES["RUB"]}]
    if not table:
        for code in PINNED:
            if code != "RUB":
                out.append({"code": code, "name": PINNED_NAMES[code]})
        return out
    seen = {"RUB"}
    for code in PINNED:
        if code in table:
            out.append({"code": code, "name": table[code].get("name") or PINNED_NAMES.get(code, code)})
            seen.add(code)
    rest = sorted(
        ((code, row.get("name") or code) for code, row in table.items() if code not in seen),
        key=lambda item: item[1],
    )
    for code, name in rest:
        out.append({"code": code, "name": name})
    return out


def _view(data: dict, include_token: bool) -> dict:
    led = _ensure(data) if include_token else (_ledger(data) or {})
    ledger = {
        "title": led.get("title") or DEFAULT_TITLE,
        "ownerName": led.get("ownerName") or DEFAULT_OWNER,
        "friendName": led.get("friendName") or DEFAULT_FRIEND,
    }
    if include_token:
        ledger.update({
            "id": led.get("id"),
            "shareToken": led.get("shareToken") or "",
            "shareEnabled": led.get("shareEnabled", True) is not False,
            "createdAt": led.get("createdAt") or 0,
            "updatedAt": led.get("updatedAt") or 0,
        })
    return {
        "ledger": ledger,
        "balance": totals(data.get("entries")),
        "entries": _sorted_entries(data.get("entries")),
        "serverToday": today_msk().isoformat(),
    }


def owner_view() -> dict:
    with _lock:
        data = _read()
        before = json.dumps(data.get("ledger"), ensure_ascii=False, sort_keys=True)
        view = _view(data, True)
        if json.dumps(data.get("ledger"), ensure_ascii=False, sort_keys=True) != before:
            _write(data)
        return view


def public_view(token: str):
    with _lock:
        data = _read()
        if _find_open(data, token) is None:
            return 404, {"error": "Ссылка не найдена."}
        return 200, _view(data, False)


def _ensure_rates(data: dict, day: date):
    """Снимок курсов на дату операции. Уже сохранённый день не перезаписывается.
    Третий элемент — True, только если снимок только что записан в data."""
    key = day.isoformat()
    cached = data["rates"].get(key)
    if isinstance(cached, dict) and isinstance(cached.get("table"), dict) and cached["table"]:
        return cached, None, False
    try:
        parsed = parse_cbr(fetch_cbr(day))
    except Exception:
        return None, "Курс ЦБ сейчас недоступен. Повторите чуть позже.", False
    if not parsed["table"]:
        return None, "Курс ЦБ сейчас недоступен. Повторите чуть позже.", False
    record = {
        "source": "cbr",
        "cbrDate": parsed["cbrDate"] or key,
        "fetchedAt": now_ms(),
        "table": parsed["table"],
    }
    data["rates"][key] = record
    return record, None, True


def _rub_info(day: date) -> dict:
    return {
        "rate": format(Decimal(1).quantize(RATE_Q), "f"),
        "nominal": "1",
        "value": format(Decimal(1).quantize(VALUE_Q), "f"),
        "name": PINNED_NAMES["RUB"],
        "source": "rub",
        "cbrDate": day.isoformat(),
        "rateDate": day.isoformat(),
    }


def _info_from_row(row: dict, record: dict, day: date, code: str) -> dict:
    return {
        "rate": row["rate"],
        "nominal": row["nominal"],
        "value": row["value"],
        "name": row.get("name") or code,
        "source": "cbr",
        "cbrDate": record.get("cbrDate") or day.isoformat(),
        "rateDate": day.isoformat(),
    }


def _rate_for(data: dict, day: date, currency: str):
    """(info, error, cache_changed). RUB не ходит в ЦБ."""
    currency = str(currency or "").strip().upper()
    if currency == "RUB":
        return _rub_info(day), None, False
    if not CODE_RE.match(currency):
        return None, "Этой валюты нет в курсах ЦБ на выбранную дату.", False
    record, err, fresh = _ensure_rates(data, day)
    if err or record is None:
        return None, err or "Курс ЦБ сейчас недоступен. Повторите чуть позже.", fresh
    row = record["table"].get(currency)
    if not row:
        return None, "Этой валюты нет в курсах ЦБ на выбранную дату.", fresh
    return _info_from_row(row, record, day, currency), None, fresh


def _to_rub(amount: Decimal, info: dict) -> Decimal:
    if info.get("source") == "rub":
        return amount.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    value = Decimal(info["value"])
    nominal = Decimal(info["nominal"])
    return (amount * value / nominal).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _stored_info(entry: dict) -> dict:
    return {
        "rate": entry.get("rate"),
        "nominal": entry.get("nominal") or "1",
        "value": entry.get("rateValue") or entry.get("rate") or "1",
        "name": entry.get("rateName") or "",
        "source": entry.get("rateSource") or "",
        "cbrDate": entry.get("cbrDate") or entry.get("date") or "",
        "rateDate": entry.get("rateDate") or entry.get("date") or "",
    }


def _apply_money(entry: dict, info: dict, amount: Decimal) -> None:
    entry["amount"] = money_str(amount)
    entry["amountRub"] = money_str(_to_rub(amount, info))
    entry["rate"] = info["rate"]
    entry["nominal"] = info["nominal"]
    entry["rateValue"] = info["value"]
    entry["rateName"] = info.get("name") or ""
    entry["rateDate"] = info.get("rateDate") or entry.get("date") or ""
    entry["cbrDate"] = info.get("cbrDate") or ""
    entry["rateSource"] = info.get("source") or ""


def _actor(raw):
    name = _clean(raw, MAX_AUTHOR)
    if not name:
        return None, "Напишите, кто вносит запись."
    return name, None


def _payload_money(payload: dict, today: date):
    kind = str(payload.get("kind") or "")
    if kind not in KINDS:
        return None, "Выберите пополнение или покупку."
    amount = parse_amount(payload.get("amount"))
    if amount is None:
        return None, "Сумма должна быть больше нуля."
    currency = str(payload.get("currency") or "").strip().upper()
    day = parse_day(payload.get("date") or today.isoformat(), today)
    if isinstance(day, str):
        return None, day
    if currency != "RUB" and not CODE_RE.match(currency):
        return None, "Этой валюты нет в курсах ЦБ на выбранную дату."
    return {"kind": kind, "amount": amount, "currency": currency, "day": day, "comment": _clean(payload.get("comment"), MAX_COMMENT)}, None


def quote(token: str, currency: str, day_raw: str | None, amount_raw):
    with _lock:
        data = _read()
        if _find_open(data, token) is None:
            return 404, {"error": "Ссылка не найдена."}
        today = today_msk()
        day = parse_day(day_raw or today.isoformat(), today)
        if isinstance(day, str):
            return 400, {"error": day}
        currency = str(currency or "RUB").strip().upper() or "RUB"
        # Снимок нужен и для списка валют. Повторный запрос того же дня в сеть не ходит.
        record, rate_err, fresh = _ensure_rates(data, day)
        table = (record or {}).get("table") if isinstance(record, dict) else None
        if currency == "RUB":
            info, error = _rub_info(day), None
        elif record is None:
            info, error = None, rate_err
        elif not CODE_RE.match(currency) or currency not in record["table"]:
            info, error = None, "Этой валюты нет в курсах ЦБ на выбранную дату."
        else:
            info, error = _info_from_row(record["table"][currency], record, day, currency), None
        amount = None
        if amount_raw not in (None, ""):
            amount = parse_amount(amount_raw)
            if amount is None and error is None:
                error = "Сумма должна быть больше нуля."
        if fresh:
            _write(data)
        if error and info is None:
            code = 503 if error and "недоступен" in error else 400
            return code, {"error": error, "currencies": _currency_options(table), "ratesAvailable": bool(table)}
        body = {
            "currency": currency,
            "name": info["name"],
            "rate": info["rate"],
            "nominal": info["nominal"],
            "value": info["value"],
            "rateDate": info["rateDate"],
            "cbrDate": info["cbrDate"],
            "source": info["source"],
            "currencies": _currency_options(table),
            "ratesAvailable": bool(table),
        }
        if amount is not None and error is None:
            body["amount"] = money_str(amount)
            body["amountRub"] = money_str(_to_rub(amount, info))
        if error:
            body["error"] = error
            return 400, body
        return 200, body


def _mutate(token: str, fn):
    with _lock:
        data = _read()
        if _find_open(data, token) is None:
            return 404, {"error": "Ссылка не найдена."}
        code, body, changed = fn(data)
        if changed:
            _write(data)
        return code, body


def add_entry(token: str, payload: dict):
    def apply(data):
        actor, err = _actor(payload.get("authorName"))
        if err:
            return 400, {"error": err}, False
        parsed, err = _payload_money(payload, today_msk())
        if err:
            return 400, {"error": err}, False
        if len([e for e in data["entries"] if isinstance(e, dict)]) >= MAX_ENTRIES:
            return 400, {"error": "Слишком много записей."}, False
        info, err, changed = _rate_for(data, parsed["day"], parsed["currency"])
        if err or info is None:
            return (503 if err and "недоступен" in err else 400), {"error": err or "Нет курса."}, changed
        now = now_ms()
        entry = {
            "id": str(uuid.uuid4()),
            "kind": parsed["kind"],
            "currency": parsed["currency"],
            "comment": parsed["comment"],
            "authorName": actor,
            "editedBy": "",
            "status": "active",
            "voidedBy": "",
            "voidedAt": 0,
            "restoredBy": "",
            "date": parsed["day"].isoformat(),
            "createdAt": now,
            "updatedAt": now,
        }
        _apply_money(entry, info, parsed["amount"])
        data["entries"].append(entry)
        return 200, _view(data, False), True

    if not isinstance(payload, dict):
        return 400, {"error": "Некорректный запрос."}
    return _mutate(token, apply)


def update_entry(token: str, payload: dict):
    def apply(data):
        actor, err = _actor(payload.get("authorName"))
        if err:
            return 400, {"error": err}, False
        entry_id = str(payload.get("id") or "")
        entry = next((e for e in data["entries"] if isinstance(e, dict) and e.get("id") == entry_id), None)
        if entry is None:
            return 404, {"error": "Запись не найдена."}, False
        if entry.get("status") == "void":
            return 409, {"error": "Запись отменена. Сначала верните её."}, False
        parsed, err = _payload_money(payload, today_msk())
        if err:
            return 400, {"error": err}, False
        same_moment = parsed["day"].isoformat() == str(entry.get("date") or "") and parsed["currency"] == str(entry.get("currency") or "")
        changed = False
        if same_moment:
            info = _stored_info(entry)
        else:
            info, err, changed = _rate_for(data, parsed["day"], parsed["currency"])
            if err or info is None:
                return (503 if err and "недоступен" in err else 400), {"error": err or "Нет курса."}, changed
        entry["kind"] = parsed["kind"]
        entry["currency"] = parsed["currency"]
        entry["comment"] = parsed["comment"]
        entry["date"] = parsed["day"].isoformat()
        entry["editedBy"] = actor
        entry["updatedAt"] = now_ms()
        _apply_money(entry, info, parsed["amount"])
        return 200, _view(data, False), True

    if not isinstance(payload, dict):
        return 400, {"error": "Некорректный запрос."}
    return _mutate(token, apply)


def _set_status(token: str, payload: dict, void: bool):
    def apply(data):
        actor, err = _actor(payload.get("authorName"))
        if err:
            return 400, {"error": err}, False
        entry_id = str(payload.get("id") or "")
        entry = next((e for e in data["entries"] if isinstance(e, dict) and e.get("id") == entry_id), None)
        if entry is None:
            return 404, {"error": "Запись не найдена."}, False
        now = now_ms()
        if void:
            if entry.get("status") == "void":
                return 400, {"error": "Запись уже отменена."}, False
            entry["status"] = "void"
            entry["voidedBy"] = actor
            entry["voidedAt"] = now
        else:
            if entry.get("status") != "void":
                return 400, {"error": "Запись не отменена."}, False
            entry["status"] = "active"
            entry["voidedBy"] = ""
            entry["voidedAt"] = 0
            entry["restoredBy"] = actor
        entry["updatedAt"] = now
        return 200, _view(data, False), True

    if not isinstance(payload, dict):
        return 400, {"error": "Некорректный запрос."}
    return _mutate(token, apply)


def void_entry(token: str, payload: dict):
    return _set_status(token, payload, True)


def restore_entry(token: str, payload: dict):
    return _set_status(token, payload, False)


def update_settings(payload: dict):
    if not isinstance(payload, dict):
        return 400, {"error": "Некорректный запрос."}
    with _lock:
        data = _read()
        led = _ensure(data)
        if "title" in payload:
            led["title"] = _clean(payload.get("title"), MAX_TITLE) or DEFAULT_TITLE
        if "ownerName" in payload:
            led["ownerName"] = _clean(payload.get("ownerName"), MAX_NAME) or DEFAULT_OWNER
        if "friendName" in payload:
            led["friendName"] = _clean(payload.get("friendName"), MAX_NAME) or DEFAULT_FRIEND
        if "shareEnabled" in payload:
            led["shareEnabled"] = bool(payload.get("shareEnabled"))
        if payload.get("regenerate"):
            led["shareToken"] = secrets.token_hex(16)
            led["shareEnabled"] = True
        led["updatedAt"] = now_ms()
        _write(data)
        return 200, _view(data, True)
