"""Утренняя газета: сбор данных, газетная вёрстка A4 и рендер PDF/PNG.

Модуль не знает о HTTP-сервере Jarvis. Сервер передаёт ему app-data,
функцию чтения Apple Calendar и requests, поэтому сборку легко проверять
без настоящих аккаунтов и внешней сети.
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
    "time": "07:30",
    "city": "",
    "title": "The Jarvis Daily",
    "fontSize": "xlarge",
    "news": True,
    "boss": True,
    "calendar": True,
    "weather": True,
    "quote": True,
    "fact": True,
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


def _s(value, limit=240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _e(value, limit=240) -> str:
    return html.escape(_s(value, limit), quote=True)


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
    out["time"] = _hhmm(out.get("time"))
    out["city"] = _s(out.get("city") or (app.get("settings") or {}).get("weatherCity") or "Москва", 100)
    out["title"] = _s(out.get("title") or DEFAULTS["title"], 80)
    out["fontSize"] = "xlarge" if out.get("fontSize") == "xlarge" else "large"
    for key in ("news", "boss", "calendar", "weather", "quote", "fact"):
        out[key] = bool(out.get(key, True))
    return out


def boss_for_day(app: dict, day: date) -> dict:
    js_day = day.isoweekday() % 7
    active = [t for t in (app.get("bossTasks") or [])
              if isinstance(t, dict) and not t.get("archived") and js_day in (t.get("days") or [])]
    morning, evening = [], []
    for task in active:
        name = _s(task.get("name") or "Дело", 90)
        period = task.get("period")
        if period in ("morning", "allday"):
            morning.append(name)
        if period in ("evening", "allday"):
            evening.append(name)
    return {"morning": morning[:7], "evening": evening[:7]}


def _plain(text: str) -> str:
    return _s(html.unescape(re.sub(r"<[^>]+>", " ", text or "")), 500)


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
            source = _s(source_el.text, 80)
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
        result.append({
            "title": _s(title, 150),
            "source": source or "Google News",
            "time": published.astimezone(MSK).strftime("%H:%M"),
            "url": _s(item.findtext("link") or "", 500),
        })
        if len(result) == 4:
            break
    if not result:
        raise RuntimeError("За последние 24 часа лента не вернула новостей")
    return result


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
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
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
        value = next((v for t, v in pairs if wanted in t), None)
        return value

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    probabilities = hourly.get("precipitation_probability") or []
    winds = hourly.get("wind_speed_10m") or []
    return {
        "city": name,
        "current": current.get("temperature_2m"),
        "day": at_hour(14),
        "evening": at_hour(20),
        "wind": max([v for v in winds if isinstance(v, (int, float))] or [current.get("wind_speed_10m") or 0]),
        "precipProbability": max([v for v in probabilities if isinstance(v, (int, float))] or
                                 (daily.get("precipitation_probability_max") or [0])),
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
    if code is not None and (51 <= code <= 67 or 80 <= code <= 82):
        return "Дождь"
    if code is not None and 71 <= code <= 77:
        return "Снег"
    if code is not None and 95 <= code <= 99:
        return "Гроза"
    return "Прогноз на сегодня"


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


def collect(app: dict, requests_module, calendar_loader, cache_file: Path,
            now: datetime | None = None, refresh: bool = True) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    cfg = settings(app)
    cached = _load_json(cache_file)
    issues = []
    news = cached.get("news") or []
    weather = cached.get("weather") or {}
    if weather and weather.get("city") != cfg["city"]:
        weather = {}

    if cfg["news"] and (refresh or not news):
        try:
            news = fetch_news(requests_module, now)
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
            events = loaded.get("events") or []
        except Exception as exc:
            calendar_error = _s(exc, 180)
            issues.append(f"Apple Calendar: {calendar_error}")

    day_index = now.date().toordinal()
    payload = {
        "generatedAt": now.isoformat(),
        "date": now.date().isoformat(),
        "dateLabel": f"{now.day} {MONTHS_RU[now.month - 1]} {now.year}",
        "weekday": WEEKDAYS_RU[now.weekday()],
        "config": cfg,
        "boss": boss_for_day(app, now.date()) if cfg["boss"] else {"morning": [], "evening": []},
        "events": events[:8],
        "calendarError": calendar_error,
        "weather": weather if cfg["weather"] else {},
        "news": news[:4] if cfg["news"] else [],
        "quote": {"text": QUOTES[day_index % len(QUOTES)][0], "author": QUOTES[day_index % len(QUOTES)][1]},
        "fact": FACTS[day_index % len(FACTS)],
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
        return f'<p class="empty">{_e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{_e(item, 80)}</li>" for item in items) + "</ul>"


def _event_rows(events: list, error: str) -> str:
    if not events:
        text = f"Календарь недоступен: {error}" if error else "На сегодня событий нет"
        return f'<div class="event"><time>—</time><div><b>{_e(text, 120)}</b></div></div>'
    rows = []
    for event in events[:6]:
        start = str(event.get("start") or "")
        tm = "весь день" if event.get("allDay") else (start[11:16] if len(start) >= 16 else "—")
        meta = " · ".join(x for x in (_s(event.get("calendar"), 35), _s(event.get("location"), 45)) if x)
        rows.append(f'<div class="event"><time>{_e(tm, 10)}</time><div><b>{_e(event.get("title"), 90)}</b>'
                    f'{f"<small>{_e(meta, 85)}</small>" if meta else ""}</div></div>')
    return "".join(rows)


def _news_cards(news: list) -> str:
    cards = list(news[:4])
    while len(cards) < 4:
        cards.append({"title": "Новость не загружена", "source": "Проверьте подключение", "time": "—"})
    out = []
    for i, item in enumerate(cards, 1):
        cls = "lead" if i == 1 else ""
        out.append(
            f'<article class="{cls}"><div class="kicker">{("Главная история" if i == 1 else "Мир")}</div>'
            f'<h3><span class="num">{i}</span>{_e(item.get("title"), 150)}</h3>'
            f'<span class="source">{_e(item.get("source"), 70)} · {_e(item.get("time"), 12)}</span></article>'
        )
    return "".join(out)


def render_html(payload: dict) -> str:
    cfg = payload["config"]
    weather = payload.get("weather") or {}
    boss = payload.get("boss") or {}
    size_class = "xlarge" if cfg.get("fontSize") == "xlarge" else "large"
    sections = []
    if cfg.get("boss"):
        sections.append(f'''<section class="boss"><div class="kicker">Раздел «Босс»</div><h2>Ритм дня</h2>
          <div class="boss-grid"><div><h4>Утро</h4>{_task_list(boss.get("morning") or [], "Утренних дел нет")}</div>
          <div><h4>Вечер</h4>{_task_list(boss.get("evening") or [], "Вечерних дел нет")}</div></div></section>''')
    if cfg.get("calendar"):
        sections.append(f'''<section class="calendar"><div class="kicker">Apple Calendar</div><h2>Планы</h2>
          <div class="events">{_event_rows(payload.get("events") or [], payload.get("calendarError") or "")}</div></section>''')
    if cfg.get("weather"):
        sections.append(f'''<section class="weather"><div class="kicker">Погода · {_e(weather.get("city") or cfg.get("city"), 50)}</div><h2>Сегодня</h2>
          <div class="temp">{_temp(weather.get("current"))}</div><div class="condition">{_e(_weather_label(weather.get("code")))}</div>
          <table><tr><td>Сейчас</td><td>{_temp(weather.get("current"))}</td></tr><tr><td>Днём</td><td>{_temp(weather.get("day"))}</td></tr>
          <tr><td>Вечером</td><td>{_temp(weather.get("evening"))}</td></tr><tr><td>Ветер</td><td>{round(weather.get("wind") or 0)} м/с</td></tr>
          <tr><td>Осадки</td><td>{round(weather.get("precipProbability") or 0)}% · {weather.get("precipMm") or 0:g} мм</td></tr></table></section>''')
    day_grid = "".join(sections)
    day_section = f'<div class="day-grid">{day_grid}</div>' if sections else ""
    news_section = ""
    if cfg.get("news"):
        news_section = f'''<section class="news"><div class="section-title">Четыре главных события мира · последние 24 часа</div>
          <div class="news-grid">{_news_cards(payload.get("news") or [])}</div></section>'''
    bottom = []
    if cfg.get("quote"):
        quote = payload.get("quote") or {}
        bottom.append(f'''<article><h4>Цитата дня</h4><blockquote>«{_e(quote.get("text"), 180)}»<footer>{_e(quote.get("author"), 60)}</footer></blockquote></article>''')
    if cfg.get("fact"):
        bottom.append(f'''<article><h4>Интересный факт</h4><p>{_e(payload.get("fact"), 280)}</p></article>''')
    bottom_section = f'<section class="bottom">{"".join(bottom)}</section>' if bottom else ""
    issues = payload.get("issues") or []
    issue_line = f'<div class="issues">Не всё загрузилось: {_e("; ".join(issues), 260)}</div>' if issues else ""
    columns = max(1, len(sections))
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(cfg.get("title"))} — {_e(payload.get("dateLabel"))}</title><style>
@page{{size:A4;margin:0}}*{{box-sizing:border-box}}:root{{--ink:#151411;--soft:#625f57;--paper:#f8f5ec;--hair:#9c978d}}
html,body{{margin:0;background:#dad7d0;color:var(--ink)}}body{{font-family:"Nimbus Roman","Times New Roman",serif}}
.page{{position:relative;width:210mm;height:297mm;margin:12px auto;overflow:hidden;background:var(--paper);padding:8mm 9.5mm 7mm;box-shadow:0 8px 32px #0003}}
.page:after{{content:"";position:absolute;inset:0;pointer-events:none;opacity:.08;background-image:radial-gradient(#000 .3px,transparent .4px);background-size:3px 3px}}
.top{{display:flex;justify-content:space-between;border-bottom:.35mm solid;padding-bottom:1.5mm;font:700 2.3mm Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase}}
.mast{{text-align:center;padding:2.1mm 0 1mm}}.mast h1{{margin:0;font-family:C059,"Nimbus Roman",serif;font-size:12.5mm;line-height:.9;letter-spacing:-.05em}}
.mast p{{margin:1mm 0 0;font-size:2.9mm;font-style:italic;letter-spacing:.12em}}.edition{{display:flex;justify-content:space-between;padding:1.5mm 0;border-top:.25mm solid;border-bottom:1mm double;font:700 2.2mm Arial,sans-serif;text-transform:uppercase}}
.issues{{margin:1.7mm 0 0;padding:1mm 1.5mm;border:.2mm solid var(--hair);font:700 1.85mm Arial,sans-serif;color:var(--soft)}}
.section-title{{display:flex;align-items:center;gap:2.5mm;margin:2.2mm 0 1.8mm;font:700 2.15mm Arial,sans-serif;letter-spacing:.15em;text-transform:uppercase}}.section-title:before,.section-title:after{{content:"";height:.25mm;background:var(--ink);flex:1}}
.day-grid{{display:grid;grid-template-columns:repeat({columns},1fr);gap:3mm;min-height:65mm;border-bottom:.45mm solid;padding-bottom:2.5mm;margin-top:2.5mm}}.day-grid>section+section{{border-left:.2mm solid var(--hair);padding-left:3mm}}
.kicker{{font:700 1.95mm Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--soft);margin-bottom:1mm}}h2{{margin:0 0 1.5mm;font-size:5.2mm;line-height:.95}}h4{{margin:0 0 1mm;font:700 2.05mm Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase}}
.boss-grid{{display:grid;grid-template-columns:1fr 1fr;gap:2mm}}ul{{margin:0;padding-left:4mm;font-size:2.85mm;line-height:1.2}}li{{margin-bottom:1.1mm}}.empty{{margin:0;padding:1.5mm;border:.2mm solid var(--hair);font-size:2.65mm;color:var(--soft)}}
.event{{display:grid;grid-template-columns:12mm 1fr;gap:1.5mm;padding:1.3mm 0;border-top:.2mm solid var(--hair)}}.event time{{font:700 2.3mm Arial,sans-serif}}.event b{{display:block;font-size:2.9mm;line-height:1.05}}.event small{{display:block;margin-top:.4mm;font:2mm Arial,sans-serif;color:var(--soft)}}
.temp{{font-size:12mm;line-height:.85;letter-spacing:-.06em;margin:1mm 0}}.condition{{font-size:3mm;font-style:italic;margin-bottom:1.5mm}}table{{width:100%;border-collapse:collapse;font:2.35mm Arial,sans-serif}}td{{border-top:.2mm solid var(--hair);padding:1mm 0}}td:last-child{{text-align:right;font-weight:700}}
.news{{margin-top:2.2mm}}.news-grid{{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:3mm;min-height:91mm}}.news article+article{{border-left:.2mm solid var(--hair);padding-left:3mm}}.lead{{grid-row:span 2}}.news article:nth-child(4){{grid-column:2/4;border-left:.2mm solid var(--hair);border-top:.2mm solid var(--hair);padding-top:2.5mm}}
.news h3{{margin:0;font-size:4.5mm;line-height:1.03;letter-spacing:-.015em}}.news .lead h3{{font-size:6.5mm;line-height:.98}}.num{{float:left;font-size:9mm;line-height:.72;margin:1mm 1.2mm 0 0}}.source{{display:block;margin-top:1.6mm;font:700 1.8mm Arial,sans-serif;letter-spacing:.1em;text-transform:uppercase;color:var(--soft)}}
.bottom{{display:grid;grid-template-columns:repeat({max(1,len(bottom))},1fr);gap:3mm;min-height:36mm;margin-top:2.5mm;padding-top:2.2mm;border-top:1mm double}}.bottom article+article{{border-left:.2mm solid var(--hair);padding-left:3mm}}.bottom h4{{font-size:2mm}}blockquote{{margin:0;font-size:4.2mm;line-height:1.05;font-style:italic}}blockquote footer{{margin-top:1mm;font:700 1.9mm Arial,sans-serif;font-style:normal;text-transform:uppercase;letter-spacing:.1em}}.bottom p{{margin:0;font-size:3mm;line-height:1.22}}
.foot{{position:absolute;left:9.5mm;right:9.5mm;bottom:3.6mm;display:flex;justify-content:space-between;border-top:.2mm solid var(--hair);padding-top:1mm;font:1.7mm Arial,sans-serif;text-transform:uppercase;color:var(--soft)}}
.xlarge ul,.xlarge .event b,.xlarge table{{font-size:3mm}}.xlarge .news h3{{font-size:4.8mm}}.xlarge .news .lead h3{{font-size:6.9mm}}.xlarge .bottom p{{font-size:3.15mm}}
@media print{{html,body{{background:white}}.page{{margin:0;box-shadow:none}}}}@media screen and (max-width:850px){{body{{overflow-x:auto}}.page{{margin:0}}}}
</style></head><body><main class="page {size_class}">
<div class="top"><span>Личный утренний выпуск</span><span>{_e(payload.get("weekday"))}</span><span>№ {str(payload.get("date") or "").replace("-", "")}</span></div>
<header class="mast"><h1>{_e(cfg.get("title"),80)}</h1><p>Ваш день — ясно, коротко, на бумаге</p></header>
<div class="edition"><span>{_e(payload.get("dateLabel"))}</span><span>Утреннее издание · {_e(cfg.get("time"))}</span><span>1 страница</span></div>{issue_line}
{day_section}{news_section}{bottom_section}
<footer class="foot"><span>Собрано Jarvis · {_e((payload.get("generatedAt") or "")[11:16])} МСК</span><span>Для личного использования</span></footer>
</main></body></html>'''


def render_outputs(html_text: str) -> tuple[bytes, bytes]:
    """Возвращает (PDF A4, PNG-превью) без внешних ресурсов."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        explicit = (os.environ.get("CHROMIUM_EXECUTABLE") or os.environ.get("CHROMIUM_PATH") or "").strip()
        executable = explicit if explicit and os.path.isfile(explicit) else (shutil.which("chromium") or "")
        if executable:
            launch["executable_path"] = executable
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
