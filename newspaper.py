"""Утренняя газета: сбор данных, газетная вёрстка A4 и рендер PDF/PNG.

Модуль не знает о HTTP-сервере Jarvis. Сервер передаёт ему app-data,
функцию чтения Apple Calendar, requests, фоновую модель и чтение Telegram,
поэтому сборку легко проверять без настоящих аккаунтов и внешней сети.
Если текст не влезает на один лист, вёрстка продолжается на следующей
странице: строки выпуска не заменяются на «и ещё N».
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


MSK = timezone(timedelta(hours=3))
NEWS_URL = "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ru&gl=RU&ceid=RU:ru"

DEFAULTS = {
    "enabled": False,
    "autoPrint": True,
    "time": "07:30",
    "city": "",
    "title": "The Jarvis Daily",
    "fontSize": "xlarge",
    "printerHost": "",
    "printerPort": 631,
    "printerPath": "",
    "news": True,
    "boss": True,
    "calendar": True,
    "weather": True,
    "telegram": True,
    "aside": True,
}

QUOTES = [
    ("Пока мы откладываем жизнь, она проходит.", "Сенека"),
    ("У тебя есть власть над своим умом, а не над внешними событиями.", "Марк Аврелий"),
    ("Не вещи тревожат людей, а представления о вещах.", "Эпиктет"),
    ("Дорогу осилит идущий.", "Латинская пословица"),
    ("Начало — важнейшая часть работы.", "Платон"),
    ("Счастье зависит от нас самих.", "Аристотель"),
    ("Делай что должно, и будь что будет.", "Марк Аврелий"),
]

FACTS = [
    "У осьминога три сердца: два прокачивают кровь через жабры, а третье снабжает остальные органы.",
    "Свет от Солнца достигает Земли примерно за 8 минут 20 секунд.",
    "Самая глубокая точка океана — Бездна Челленджера в Марианской впадине; её глубина около 11 километров.",
    "Антарктида считается крупнейшей пустыней Земли: осадков там выпадает меньше, чем во многих жарких пустынях.",
    "В человеческом теле более 600 скелетных мышц, и они составляют значительную часть массы тела.",
    "Международная космическая станция делает один оборот вокруг Земли примерно за полтора часа.",
    "Первый известный ежедневный печатный лист выходил в Лейпциге в XVII веке.",
]

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

NEWS_SYSTEM = (
    "Ты редактор личной утренней газеты. Для каждого заголовка напиши краткое описание "
    "новости по-русски: два или три полных предложения. Не выдумывай факты, числа и имена, "
    "которых нет в заголовке и справке. Если справки мало, перескажи смысл заголовка "
    "осторожно, без новых подробностей. Верни только JSON-массив строк той же длины и в том же порядке."
)
TELEGRAM_SYSTEM = (
    "Ты готовишь строки личной утренней газеты по непрочитанным сообщениям Telegram. "
    "По каждому чату одна короткая фраза по-русски: чего от владельца хотят. "
    "Сообщения самого владельца уже убраны и просьбой не являются. "
    "Для группы или канала фраза — суть просьбы к владельцу, без пересказа всего чата. "
    "Если явной просьбы нет, одной фразой скажи, о чём сообщили. "
    "Не пиши пароли, коды, пины, номера карт и одноразовые секреты. "
    "Не выдумывай. Верни только JSON-массив строк той же длины и в том же порядке: "
    "фраза без имени чата и без тире в начале."
)

_LABELED_SECRET = re.compile(
    r"(?i)\b("
    r"парол\w*|password|passwd|passcode|"
    r"pin-?код|пин-?код|"
    r"pin|пин|"
    r"cvv|cvc|"
    r"токен\w*|token|"
    r"secret|api[_ -]?key|"
    r"код\s+подтверждения|код\s+из\s+смс|код\s+домофона|код\s+доступа"
    r")\b\s*[:#№=\-]?\s*\S+"
)
_OTP = re.compile(r"(?i)\bкод\s*[:#№]?\s*\d{4,8}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{24,}={0,2}\b")


def _s(value, limit=240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _clip(value, limit=2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _esc(value) -> str:
    return html.escape(_clip(value, 2000), quote=True)


def _e(value, limit=240) -> str:
    return html.escape(_s(value, limit), quote=True)


def _redact(value) -> str:
    """Убирает из строки выпуска пароли, коды и длинные секреты."""
    text = str(value or "")
    text = _LABELED_SECRET.sub(lambda m: m.group(1) + " [скрыто]", text)
    text = _OTP.sub("код [скрыто]", text)
    text = _CARD.sub("[скрыто]", text)
    text = _LONG_TOKEN.sub("[скрыто]", text)
    return _clip(text, 500)


def _phrase(value) -> str:
    text = _redact(value)
    text = re.sub(r"^[—–\-]+\s*", "", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(p for p in parts[:2] if p).strip()
    if len(text) > 240:
        text = text[:240].rsplit(" ", 1)[0].strip()
    return text


def _hhmm(value, fallback="07:30") -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return fallback
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def settings(app: dict | None) -> dict:
    app = app or {}
    raw = app.get("newspaper") if isinstance(app.get("newspaper"), dict) else {}
    out = {**DEFAULTS, **raw}
    out["enabled"] = bool(out.get("enabled"))
    out["autoPrint"] = bool(out.get("autoPrint", True))
    out["time"] = _hhmm(out.get("time"))
    out["city"] = _s(out.get("city") or (app.get("settings") or {}).get("weatherCity") or "Москва", 100)
    out["title"] = _s(out.get("title") or DEFAULTS["title"], 80)
    out["fontSize"] = "xlarge" if out.get("fontSize") == "xlarge" else "large"
    host = _s(out.get("printerHost"), 200)
    out["printerHost"] = host if re.fullmatch(r"[A-Za-z0-9._:-]*", host) else ""
    try:
        port = int(out.get("printerPort") or 631)
    except (TypeError, ValueError):
        port = 631
    out["printerPort"] = port if 1 <= port <= 65535 else 631
    path = _s(out.get("printerPath"), 200)
    out["printerPath"] = ("/" + path.lstrip("/")) if path else ""
    for key in ("news", "boss", "calendar", "weather", "telegram"):
        out[key] = bool(out.get(key, True))
    if "aside" in raw:
        out["aside"] = bool(raw.get("aside"))
    elif "quote" in raw or "fact" in raw:
        out["aside"] = bool(raw.get("quote", True)) or bool(raw.get("fact", True))
    else:
        out["aside"] = bool(out.get("aside", True))
    out.pop("quote", None)
    out.pop("fact", None)
    return out


def boss_for_day(app: dict, day: date) -> dict:
    js_day = day.isoweekday() % 7
    active = [t for t in (app.get("bossTasks") or [])
              if isinstance(t, dict) and not t.get("archived") and js_day in (t.get("days") or [])]
    morning, evening = [], []
    for task in active:
        name = _clip(task.get("name") or "Дело", 200)
        period = task.get("period")
        if period in ("morning", "allday"):
            morning.append(name)
        if period in ("evening", "allday"):
            evening.append(name)
    return {"morning": morning, "evening": evening}


def _plain(text: str) -> str:
    return _clip(html.unescape(re.sub(r"<[^>]+>", " ", text or "")), 500)


def fetch_news(requests_module, now: datetime) -> list:
    response = requests_module.get(NEWS_URL, timeout=15, headers={"User-Agent": "Jarvis/1.0"})
    if not response.ok:
        raise RuntimeError(f"Google News ответил {response.status_code}")
    root = ET.fromstring(response.content)
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    result, seen = [], set()
    for item in root.findall(".//item"):
        title = _plain(item.findtext("title") or "")
        if not title:
            continue
        source = ""
        source_el = item.find("source")
        if source_el is not None:
            source = _clip(source_el.text, 80)
        if not source and " - " in title:
            title, source = title.rsplit(" - ", 1)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            continue
        if published < cutoff:
            continue
        key = re.sub(r"[^a-zа-я0-9]+", " ", title.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        detail = _plain(item.findtext("description") or "")
        if detail.lower().startswith(title.lower()[:40]):
            detail = ""
        result.append({
            "title": _clip(title, 300),
            "source": source or "Google News",
            "time": published.astimezone(MSK).strftime("%H:%M"),
            "url": _clip(item.findtext("link") or "", 500),
            "detail": detail,
        })
        if len(result) == 4:
            break
    if not result:
        raise RuntimeError("За последние 24 часа лента не вернула новостей")
    return result


def _code_num(code):
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _is_snow(code) -> bool:
    n = _code_num(code)
    if n is None:
        return False
    return 71 <= n <= 77 or n in (85, 86)


def _is_rain(code) -> bool:
    n = _code_num(code)
    if n is None:
        return False
    return (51 <= n <= 67) or (80 <= n <= 82) or (95 <= n <= 99)


def precip_outlook(codes, probabilities, current_code=None) -> dict:
    """Тип осадков по weathercode и вероятность по precipitation_probability.

    kind: «снег», «дождь», «снег и дождь» или «нет».
    """
    usable = []
    for code in list(codes or []):
        n = _code_num(code)
        if n is not None:
            usable.append(n)
    if not usable:
        n = _code_num(current_code)
        if n is not None:
            usable.append(n)
    snow = any(_is_snow(c) for c in usable)
    rain = any(_is_rain(c) for c in usable)
    if snow and rain:
        kind = "снег и дождь"
    elif snow:
        kind = "снег"
    elif rain:
        kind = "дождь"
    else:
        kind = "нет"
    nums = [p for p in (probabilities or []) if isinstance(p, (int, float))]
    return {"precipKind": kind, "precipProbability": max(nums) if nums else None}


def fetch_weather(requests_module, city: str, now: datetime) -> dict:
    geo = requests_module.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": city, "format": "json", "limit": 1},
        headers={"Accept-Language": "ru", "User-Agent": "Jarvis/1.0"}, timeout=12,
    )
    found = geo.json() if geo.ok else []
    if not found:
        raise RuntimeError(f"Город «{city}» не найден")
    lat, lon = float(found[0]["lat"]), float(found[0]["lon"])
    name = _s((found[0].get("display_name") or city).split(",")[0], 80)
    response = requests_module.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
            "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
            "daily": "precipitation_sum,precipitation_probability_max",
            "wind_speed_unit": "ms", "timezone": "Europe/Moscow", "forecast_days": 1,
        }, timeout=15,
    )
    if not response.ok:
        raise RuntimeError(f"Open-Meteo ответил {response.status_code}")
    data = response.json()
    hourly = data.get("hourly") or {}
    pairs = list(zip(hourly.get("time") or [], hourly.get("temperature_2m") or []))

    def at_hour(hour):
        wanted = f"T{hour:02d}:00"
        return next((v for t, v in pairs if wanted in t), None)

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    probabilities = hourly.get("precipitation_probability") or []
    outlook = precip_outlook(hourly.get("weather_code") or [], probabilities, current.get("weather_code"))
    if outlook["precipProbability"] is None:
        daily_nums = [p for p in (daily.get("precipitation_probability_max") or []) if isinstance(p, (int, float))]
        outlook["precipProbability"] = max(daily_nums) if daily_nums else None
    winds = hourly.get("wind_speed_10m") or []
    return {
        "city": name,
        "current": current.get("temperature_2m"),
        "day": at_hour(14),
        "evening": at_hour(20),
        "wind": max([v for v in winds if isinstance(v, (int, float))] or [current.get("wind_speed_10m") or 0]),
        "precipProbability": outlook["precipProbability"],
        "precipKind": outlook["precipKind"],
        "precipMm": (daily.get("precipitation_sum") or [current.get("precipitation") or 0])[0],
        "code": current.get("weather_code"),
    }


def _weather_label(code) -> str:
    if code == 0:
        return "Ясно"
    if code in (1, 2, 3):
        return "Переменная облачность"
    if code in (45, 48):
        return "Туман"
    if code is not None and 95 <= code <= 99:
        return "Гроза"
    if _is_snow(code):
        return "Снег"
    if _is_rain(code):
        return "Дождь"
    return "Прогноз на сегодня"


def _precip_text(weather: dict) -> str:
    kind = weather.get("precipKind")
    if kind not in ("снег", "дождь", "снег и дождь", "нет"):
        kind = precip_outlook([], [], weather.get("code"))["precipKind"]
    label = {"снег": "снег", "дождь": "дождь", "снег и дождь": "снег и дождь"}.get(kind, "осадков нет")
    probability = weather.get("precipProbability")
    if isinstance(probability, (int, float)):
        return f"{label}, вероятность {int(round(probability))}%"
    return label


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _json_strings(text: str, n: int) -> list:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("ответ не JSON-массив")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("ответ не список")
    out = []
    for item in data[:n]:
        if isinstance(item, str):
            out.append(item.strip())
        elif isinstance(item, dict):
            out.append(str(item.get("ask") or item.get("summary") or item.get("text") or item.get("line") or "").strip())
        else:
            out.append("")
    if len(out) < n:
        raise ValueError("в ответе меньше строк, чем нужно")
    return out


def _describe_news(news: list, complete) -> list:
    payload = [{"title": n.get("title") or "", "source": n.get("source") or "", "note": n.get("detail") or ""} for n in news]
    summaries = _json_strings(complete(NEWS_SYSTEM, json.dumps(payload, ensure_ascii=False)), len(news))
    out = []
    for item, summary in zip(news, summaries):
        text = _clip(summary, 900)
        copied = dict(item)
        if text:
            copied["summary"] = text
        out.append(copied)
    if not any(item.get("summary") for item in out):
        raise RuntimeError("модель не вернула описания")
    return out


def _strip_name(name: str, ask: str) -> str:
    text = (ask or "").strip()
    for sep in (" — ", " – ", " - ", "—", "–"):
        prefix = name + sep
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _telegram_lines(data: dict, complete) -> tuple[list, str]:
    if not isinstance(data, dict) or not data.get("connected", False):
        return ["Telegram не подключён"], ""
    ready = []
    for chat in data.get("chats") or []:
        if not isinstance(chat, dict):
            continue
        name = _clip(chat.get("name") or "Без имени", 80) or "Без имени"
        if chat.get("unreadable"):
            ready.append({"name": name, "group": chat.get("kind") in ("group", "channel"), "messages": [], "unreadable": True})
            continue
        msgs = []
        for message in chat.get("messages") or []:
            if not isinstance(message, dict) or message.get("mine"):
                continue
            text = _redact(message.get("text") or "")
            if not text:
                continue
            who = _clip(message.get("from") or name, 80)
            msgs.append(f"{who}: {text}")
        if not msgs:
            continue
        ready.append({
            "name": name,
            "group": chat.get("kind") in ("group", "channel"),
            "messages": msgs[:30],
        })
    if not ready:
        if data.get("error"):
            return ["не удалось прочитать непрочитанные"], _clip(data.get("error"), 180)
        return ["непрочитанных нет"], ""
    lines, error = [], ""
    for start in range(0, len(ready), 12):
        batch = ready[start:start + 12]
        asks = [""] * len(batch)
        ai_batch = [c for c in batch if c.get("messages")]
        if ai_batch and not complete:
            error = error or "нет фоновой модели"
        elif ai_batch:
            try:
                raw = complete(TELEGRAM_SYSTEM, json.dumps([
                    {"name": c["name"], "group": bool(c.get("group")), "messages": c["messages"]}
                    for c in ai_batch
                ], ensure_ascii=False))
                got = _json_strings(raw, len(ai_batch))
                by_pos = {id(c): phrase for c, phrase in zip(ai_batch, got)}
                asks = [by_pos.get(id(c), "") for c in batch]
            except Exception as exc:
                error = error or _clip(exc, 180)
        for chat, ask in zip(batch, asks):
            phrase = _phrase(_strip_name(chat["name"], ask))
            if not phrase:
                phrase = "непрочитанное есть"
            lines.append(f"{chat['name']} — {phrase}")
    return lines, error


def collect(app: dict, requests_module, calendar_loader, cache_file: Path,
            now: datetime | None = None, refresh: bool = True,
            complete=None, telegram=None) -> dict:
    """Собирает выпуск.

    complete(system, user) -> str — фоновая модель из настроек Jarvis, без имени модели в этом модуле.
    telegram() -> {connected, chats, error} — незаглушённые диалоги с непрочитанным.
    Если telegram не передан, в блоке строка «Telegram не подключён», выпуск всё равно собирается.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    cfg = settings(app)
    cached = _load_json(cache_file)
    issues = []
    news = cached.get("news") or []
    weather = cached.get("weather") or {}
    if weather and weather.get("city") != cfg["city"]:
        weather = {}

    fetched_news = False
    if cfg["news"] and (refresh or not news):
        try:
            news = fetch_news(requests_module, now)
            fetched_news = True
        except Exception as exc:
            issues.append(f"Новости: {_s(exc, 180)}")
    need_summary = any(not (item or {}).get("summary") for item in (news or [])[:4])
    if cfg["news"] and news and complete and (fetched_news or need_summary):
        try:
            news = _describe_news(news[:4], complete)
        except Exception as exc:
            issues.append(f"Новости: {_s(exc, 180)}")
    if cfg["weather"] and (refresh or not weather or weather.get("city") != cfg["city"]):
        try:
            weather = fetch_weather(requests_module, cfg["city"], now)
        except Exception as exc:
            issues.append(f"Погода: {_s(exc, 180)}")

    events, calendar_error = [], ""
    if cfg["calendar"]:
        try:
            loaded = calendar_loader(now.date()) or {}
            events = [e for e in (loaded.get("events") or []) if isinstance(e, dict)]
            events.sort(key=lambda e: (str(e.get("start") or ""), str(e.get("title") or "")))
        except Exception as exc:
            calendar_error = _s(exc, 180)
            issues.append(f"Apple Calendar: {calendar_error}")

    tg_lines = []
    if cfg["telegram"]:
        try:
            tg_data = telegram() if telegram else {"connected": False, "chats": []}
        except Exception as exc:
            tg_data = {"connected": True, "chats": [], "error": _s(exc, 180)}
        tg_lines, tg_error = _telegram_lines(tg_data if isinstance(tg_data, dict) else {}, complete)
        if tg_error:
            issues.append(f"Telegram: {tg_error}")

    day_index = now.date().toordinal()
    payload = {
        "generatedAt": now.isoformat(),
        "date": now.date().isoformat(),
        "dateLabel": f"{now.day} {MONTHS_RU[now.month - 1]} {now.year}",
        "weekday": WEEKDAYS_RU[now.weekday()],
        "config": cfg,
        "boss": boss_for_day(app, now.date()) if cfg["boss"] else {"morning": [], "evening": []},
        "events": events,
        "calendarError": calendar_error,
        "weather": weather if cfg["weather"] else {},
        "news": news[:4] if cfg["news"] else [],
        "quote": {"text": QUOTES[day_index % len(QUOTES)][0], "author": QUOTES[day_index % len(QUOTES)][1]},
        "fact": FACTS[day_index % len(FACTS)],
        "telegram": tg_lines,
        "issues": issues,
    }
    _save_json(cache_file, {"news": news[:4], "weather": weather, "updatedAt": now.isoformat()})
    return payload


def _temp(value) -> str:
    if not isinstance(value, (int, float)):
        return "—°"
    n = int(round(value))
    return f"+{n}°" if n > 0 else f"{n}°".replace("-", "−")


def _task_list(items: list, empty: str) -> str:
    if not items:
        return f'<p class="empty">{_esc(empty)}</p>'
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _event_rows(events: list, error: str) -> str:
    if not events:
        text = f"Календарь недоступен: {error}" if error else "На сегодня событий нет"
        return f'<div class="events"><div class="event"><time>—</time><div><b>{_esc(text)}</b></div></div></div>'
    rows = []
    for event in events:
        start = str(event.get("start") or "")
        tm = "весь день" if event.get("allDay") else (start[11:16] if len(start) >= 16 else "—")
        meta = " · ".join(x for x in (_clip(event.get("calendar"), 80), _clip(event.get("location"), 120)) if x)
        rows.append(
            f'<div class="event"><time>{_esc(tm)}</time><div><b>{_esc(event.get("title") or "Без названия")}</b>'
            f'{f"<small>{_esc(meta)}</small>" if meta else ""}</div></div>'
        )
    # Короткие названия не занимают правую половину листа: со второго ряда
    # события идут второй колонкой, иначе день съезжает на следующую страницу.
    if len(events) >= 5:
        per_col = (len(events) + 1) // 2
        return f'<div class="events cols-2" style="grid-template-rows:repeat({per_col},auto)">{"".join(rows)}</div>'
    return f'<div class="events">{"".join(rows)}</div>'


def _news_cards(news: list) -> str:
    cards = list(news[:4])
    while len(cards) < 4:
        cards.append({"title": "Новость не загружена", "source": "Проверьте подключение", "time": "—"})
    out = []
    for i, item in enumerate(cards, 1):
        cls = "lead" if i == 1 else ""
        summary = item.get("summary") or ""
        dek = f'<p class="dek">{_esc(summary)}</p>' if summary else ""
        out.append(
            f'<article class="{cls}"><div class="kicker">{("Главная история" if i == 1 else "Мир")}</div>'
            f'<h3><span class="num">{i}</span>{_esc(item.get("title"))}</h3>{dek}'
            f'<span class="source">{_esc(item.get("source"))} · {_esc(item.get("time"))}</span></article>'
        )
    return "".join(out)


def render_html(payload: dict) -> str:
    cfg = payload["config"]
    weather = payload.get("weather") or {}
    boss = payload.get("boss") or {}
    size_class = "xlarge" if cfg.get("fontSize") == "xlarge" else "large"
    top = []
    if cfg.get("boss"):
        top.append(f'''<section class="boss"><div class="kicker">Раздел «Босс»</div><h2>Ритм дня</h2>
          <div class="boss-grid"><div><h4>Утро</h4>{_task_list(boss.get("morning") or [], "Утренних дел нет")}</div>
          <div><h4>Вечер</h4>{_task_list(boss.get("evening") or [], "Вечерних дел нет")}</div></div></section>''')
    if cfg.get("weather"):
        precip = _precip_text(weather)
        top.append(f'''<section class="weather"><div class="kicker">Погода · {_esc(weather.get("city") or cfg.get("city"))}</div><h2>Сегодня</h2>
          <div class="temp">{_temp(weather.get("current"))}</div>
          <div class="condition">{_esc(_weather_label(weather.get("code")))}</div>
          <div class="precip">Осадки: {_esc(precip)}</div>
          <table><tr><td>Сейчас</td><td>{_temp(weather.get("current"))}</td></tr><tr><td>Днём</td><td>{_temp(weather.get("day"))}</td></tr>
          <tr><td>Вечером</td><td>{_temp(weather.get("evening"))}</td></tr><tr><td>Ветер</td><td>{round(weather.get("wind") or 0)} м/с</td></tr>
          <tr><td>Осадки</td><td>{_esc(precip)}</td></tr></table></section>''')
    day_top = f'<div class="day-top cols-{max(1, len(top))}">{"".join(top)}</div>' if top else ""
    calendar = ""
    if cfg.get("calendar"):
        calendar = f'''<section class="calendar"><div class="kicker">Apple Calendar</div><h2>Планы</h2>
          {_event_rows(payload.get("events") or [], payload.get("calendarError") or "")}</section>'''
    telegram = ""
    if cfg.get("telegram"):
        lines = payload.get("telegram") or ["непрочитанных нет"]
        body = "".join(f'<p class="tg-line">{_esc(line)}</p>' for line in lines)
        telegram = f'''<section class="telegram"><div class="section-title">Telegram · непрочитанное</div>{body}</section>'''
    news_section = ""
    if cfg.get("news"):
        news_section = f'''<section class="news"><div class="section-title">Четыре главных события мира · последние 24 часа</div>
          <div class="news-grid">{_news_cards(payload.get("news") or [])}</div></section>'''
    aside = ""
    if cfg.get("aside"):
        quote = payload.get("quote") or {}
        aside = f'''<section class="bottom"><article class="aside"><h4>Цитата дня</h4>
          <blockquote>«{_esc(quote.get("text"))}»<footer>{_esc(quote.get("author"))}</footer></blockquote>
          <h4>Интересный факт</h4><p>{_esc(payload.get("fact"))}</p></article></section>'''
    issues = payload.get("issues") or []
    issue_line = f'<div class="issues">Не всё загрузилось: {_esc("; ".join(issues))}</div>' if issues else ""
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(cfg.get("title"))} — {_esc(payload.get("dateLabel"))}</title><style>
@page{{size:A4;margin:7mm 9mm 7mm}}*{{box-sizing:border-box}}:root{{--ink:#151411;--soft:#625f57;--paper:#f8f5ec;--hair:#9c978d}}
html,body{{margin:0;background:#dad7d0;color:var(--ink)}}body{{font-family:"Nimbus Roman","Times New Roman",serif}}
.page{{position:relative;display:flex;flex-direction:column;width:210mm;min-height:297mm;margin:12px auto;background:var(--paper);padding:8mm 9.5mm 7mm;box-shadow:0 8px 32px #0003}}
.page:after{{content:"";position:absolute;inset:0;pointer-events:none;opacity:.08;background-image:radial-gradient(#000 .3px,transparent .4px);background-size:3px 3px}}
.body{{flex:1 0 auto;position:relative;z-index:1}}
.top,.mast,.edition,.issues,.foot{{position:relative;z-index:1}}
.top{{display:flex;justify-content:space-between;border-bottom:.35mm solid;padding-bottom:1.5mm;font:700 2.3mm Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase}}
.mast{{text-align:center;padding:1.2mm 0 .4mm}}.mast h1{{margin:0;font-family:C059,"Nimbus Roman",serif;font-size:11mm;line-height:.9;letter-spacing:-.05em}}
.mast p{{margin:.6mm 0 0;font-size:2.7mm;font-style:italic;letter-spacing:.12em}}.edition{{display:flex;justify-content:space-between;padding:1.1mm 0;border-top:.25mm solid;border-bottom:.8mm double;font:700 2.2mm Arial,sans-serif;text-transform:uppercase}}
.issues{{margin:1.7mm 0 0;padding:1mm 1.5mm;border:.2mm solid var(--hair);font:700 1.85mm Arial,sans-serif;color:var(--soft)}}
.section-title{{display:flex;align-items:center;gap:2.5mm;margin:1.5mm 0 1.2mm;font:700 2.15mm Arial,sans-serif;letter-spacing:.15em;text-transform:uppercase}}.section-title:before,.section-title:after{{content:"";height:.25mm;background:var(--ink);flex:1}}
.day-top{{display:grid;gap:3mm;margin-top:1.6mm;align-items:start}}.day-top.cols-2{{grid-template-columns:1fr 1fr}}.day-top>section+section{{border-left:.2mm solid var(--hair);padding-left:3mm}}
.calendar,.telegram{{margin-top:1.6mm;padding-top:1.2mm;border-top:.45mm solid}}
.kicker{{font:700 1.95mm Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--soft);margin-bottom:1mm}}h2{{margin:0 0 1.5mm;font-size:5.2mm;line-height:.95}}h4{{margin:0 0 1mm;font:700 2.05mm Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase}}
.boss-grid{{display:grid;grid-template-columns:1fr 1fr;gap:2mm}}ul{{margin:0;padding-left:4mm;font-size:2.85mm;line-height:1.2}}li{{margin-bottom:1.1mm}}.empty{{margin:0;padding:1.5mm;border:.2mm solid var(--hair);font-size:2.65mm;color:var(--soft)}}
.events.cols-2{{display:grid;grid-auto-flow:column;grid-template-columns:1fr 1fr;column-gap:4mm;position:relative}}.events.cols-2:before{{content:"";position:absolute;left:calc(50% - 2mm);top:0;bottom:0;border-left:.2mm solid var(--hair)}}
.event{{display:grid;grid-template-columns:16mm 1fr;gap:1.2mm;padding:.9mm 0;border-top:.2mm solid var(--hair);break-inside:avoid;page-break-inside:avoid}}.event time{{font:700 2.3mm Arial,sans-serif}}.event b{{display:block;font-size:2.9mm;line-height:1.15;overflow-wrap:anywhere}}.event small{{display:block;margin-top:.3mm;font:2mm Arial,sans-serif;color:var(--soft)}}
.temp{{font-size:10mm;line-height:.85;letter-spacing:-.06em;margin:.4mm 0}}.condition{{font-size:3mm;font-style:italic;margin-bottom:.4mm}}.precip{{font:700 2.7mm Arial,sans-serif;line-height:1.25;margin:0 0 1mm}}
table{{width:100%;border-collapse:collapse;font:2.35mm Arial,sans-serif}}td{{border-top:.2mm solid var(--hair);padding:.55mm 0}}td:last-child{{text-align:right;font-weight:700}}
.news{{margin-top:1.2mm}}.news-grid{{display:grid;grid-template-columns:1fr 1fr;gap:2.4mm 3.5mm;align-items:start}}.news article{{break-inside:avoid;page-break-inside:avoid}}
.news h3{{margin:0;font-size:4.5mm;line-height:1.05;letter-spacing:-.015em}}.news .lead h3{{font-size:6.5mm;line-height:.98}}.num{{float:left;font-size:9mm;line-height:.72;margin:1mm 1.2mm 0 0}}
.dek{{margin:.8mm 0 0;font-size:3.05mm;line-height:1.2;overflow-wrap:anywhere}}.source{{display:block;margin-top:1mm;font:700 1.8mm Arial,sans-serif;letter-spacing:.1em;text-transform:uppercase;color:var(--soft)}}
.telegram .tg-line{{margin:0;padding:1.15mm 0;border-top:.2mm solid var(--hair);font-size:3.2mm;line-height:1.3;overflow-wrap:anywhere;break-inside:avoid;page-break-inside:avoid}}.telegram .tg-line:first-of-type{{border-top:0}}
.bottom{{margin-top:1.6mm;padding-top:1.3mm;border-top:.8mm double}}.aside h4{{margin:1.5mm 0 .5mm}}.aside h4:first-child{{margin-top:0}}
blockquote{{margin:0;font-size:4.2mm;line-height:1.15;font-style:italic;overflow-wrap:anywhere}}blockquote footer{{margin-top:1mm;font:700 1.9mm Arial,sans-serif;font-style:normal;text-transform:uppercase;letter-spacing:.1em}}.aside p{{margin:0;font-size:3.15mm;line-height:1.28;overflow-wrap:anywhere}}
.foot{{margin-top:auto;display:flex;justify-content:space-between;border-top:.2mm solid var(--hair);padding-top:1mm;font:1.7mm Arial,sans-serif;text-transform:uppercase;color:var(--soft)}}
.xlarge ul,.xlarge .event b,.xlarge table,.xlarge .tg-line{{font-size:3.15mm}}.xlarge .news h3{{font-size:4.8mm}}.xlarge .news .lead h3{{font-size:6.9mm}}.xlarge .dek,.xlarge .aside p{{font-size:3.3mm}}
@media print{{html,body{{background:white}}.page{{margin:0;box-shadow:none;min-height:0;width:auto;padding:0;display:block}}.page:after{{display:none}}.foot{{margin-top:2mm}}
.event,.news article,.tg-line,.aside,li{{break-inside:avoid;page-break-inside:avoid}}}}
@media screen and (max-width:850px){{body{{overflow-x:auto}}.page{{margin:0}}}}
</style></head><body><main class="page {size_class}">
<div class="top"><span>Личный утренний выпуск</span><span>{_esc(payload.get("weekday"))}</span><span>№ {str(payload.get("date") or "").replace("-", "")}</span></div>
<header class="mast"><h1>{_esc(cfg.get("title"))}</h1><p>Ваш день — ясно, коротко, на бумаге</p></header>
<div class="edition"><span>{_esc(payload.get("dateLabel"))}</span><span>Утреннее издание · {_esc(cfg.get("time"))}</span><span>A4</span></div>{issue_line}
<div class="body">{day_top}{calendar}{telegram}{news_section}{aside}</div>
<footer class="foot"><span>Собрано Jarvis · {_esc((payload.get("generatedAt") or "")[11:16])} МСК</span><span>Для личного использования</span></footer>
</main></body></html>'''


def chromium_launch_kwargs() -> dict:
    """Выбирает полный Chromium, который ставится с `playwright install --no-shell`.

    Без `channel=chromium` Playwright ищет отдельный chrome-headless-shell. Его
    намеренно нет в Railway-образе, чтобы не хранить два браузера.
    """
    launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    explicit = (os.environ.get("CHROMIUM_EXECUTABLE") or os.environ.get("CHROMIUM_PATH") or "").strip()
    executable = explicit if explicit and os.path.isfile(explicit) else (shutil.which("chromium") or "")
    if executable:
        launch["executable_path"] = executable
    else:
        launch["channel"] = "chromium"
    return launch


def render_outputs(html_text: str) -> tuple[bytes, bytes]:
    """Возвращает (PDF A4, PNG-превью) без внешних ресурсов."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch = chromium_launch_kwargs()
        browser = playwright.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 850, "height": 1200}, device_scale_factor=1.5)
        page.set_content(html_text, wait_until="load")
        pdf = page.pdf(print_background=True, prefer_css_page_size=True)
        png = page.locator(".page").screenshot(type="png")
        browser.close()
    return pdf, png


def write_outputs(directory: Path, payload: dict) -> dict:
    html_text = render_html(payload)
    pdf, png = render_outputs(html_text)
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "html": directory / "jarvis_newspaper_latest.html",
        "pdf": directory / "jarvis_newspaper_latest.pdf",
        "png": directory / "jarvis_newspaper_latest.png",
    }
    for key, data in (("html", html_text.encode("utf-8")), ("pdf", pdf), ("png", png)):
        target = files[key]
        with tempfile.NamedTemporaryFile(dir=directory, prefix=target.name + ".", suffix=".tmp", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, target)
    return {"html": str(files["html"]), "pdf": str(files["pdf"]), "png": str(files["png"]),
            "pdfBytes": len(pdf), "pngBytes": len(png)}
