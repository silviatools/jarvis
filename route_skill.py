"""Saved places, free-quota Mapbox traffic routes and persistent conditional notifications.

Provider contracts: https://docs.mapbox.com/api/navigation/directions/
and https://docs.tomtom.com/search-api/documentation/search-service/fuzzy-search. Keys stay on the server and are excluded from backups.
"""
import copy
import difflib
import hashlib
import html
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import route_analysis

STORE_NAME = "jarvis_routes.json"
KEYS_NAME = "jarvis_route_keys.json"
SKILL_ID = "routes"
_lock = threading.RLock()
_worker_lock = threading.Lock()
_photon_lock = threading.Lock()
_last_photon = 0.0
SKILLS = [{"id": SKILL_ID, "title": "Места, маршруты и пробки", "risk": "create", "default": True,
           "desc": "Понимает примерные названия адресов и заведений, ищет места и уточняет неоднозначные варианты. "
                   "Запоминает адреса и алиасы; сравнивает маршруты Mapbox, показывает затруднения по участкам. "
                   "«Сообщи, когда от Vegas до дома будет меньше часа» — проверяет каждые 5 минут, "
                   "присылает одно сообщение в Telegram. Отслеживается выбранная точка отправления, не перемещение телефона. "
                   "Ключи API и адреса: Настройки → Ассистент → Места и маршруты. Только бесплатные квоты. "
                   "Поиск: TomTom с резервным Photon без ключа; доступность и полнота данных не гарантированы.",
           "tools": ["route_places", "route_save_place", "route_delete_place", "route_find_place", "route_check",
                     "route_watch", "route_watches", "route_cancel", "route_traffic"]}]


def _tool(name, desc, props=None, required=None):
    return {"name": name, "description": desc, "input_schema": {"type": "object", "properties": props or {},
                                                                 "required": required or []}}


S = {"type": "string"}
POINT = {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "label": S},
         "required": ["lat", "lon"]}
END = {"oneOf": [S, POINT], "description": "Название сохранённого места, примерный адрес/заведение или координаты. Неоднозначность вернёт вопрос и кандидатов."}
TRIP = {"origin": END, "destination": END, "avoid_tolls": {"type": "boolean"},
        "via": {"type": "array", "items": END, "maxItems": 3, "description": "До 3 промежуточных точек, если пользователь просит ехать через конкретный участок. Не угадывай съезды."}}
TOOLS = [
    _tool("route_places", "Сохранённые места: Дом, Работа и другие адреса."),
    _tool("route_find_place", "Найти адрес/место в Москве и области. При нескольких подходящих результатах спроси пользователя; "
          "не выбирай Vegas на МКАД наугад. Координаты результата передай в route_check / route_watch.",
          {"query": S, "near": POINT}, ["query"]),
    _tool("route_save_place", "Запомнить место под именем (например, Дом). Координаты бери из route_find_place или от пользователя. "
          "Не угадывай адрес. Повторное имя заменяет сохранённый адрес. aliases — привычные названия пользователя.",
          {"name": S, "address": S, "point": POINT, "aliases": {"type": "array", "items": S, "maxItems": 10}}, ["name", "point"]),
    _tool("route_delete_place", "Удалить сохранённое место по точному имени.", {"name": S}, ["name"]),
    _tool("route_check", "Получить свежие автомобильные маршруты Mapbox с пробками, временем, расстоянием и платными дорогами. "
          "Время API может отличаться от приложения Навигатора. Не выдавай прогноз за текущие пробки.", TRIP, ["origin", "destination"]),
    _tool("route_watch", "Сообщить один раз, когда самый быстрый допустимый маршрут займёт СТРОГО меньше threshold_minutes. "
          "Проверка каждые 5 минут, по умолчанию до 12 часов. Отсчёт от фиксированной точки, не GPS телефона. "
          "Явная просьба сообщить уже задаёт уведомление; plan_create для этого не нужен.",
          {**TRIP, "threshold_minutes": {"type": "number"}, "expires_in_hours": {"type": "number"}},
          ["origin", "destination", "threshold_minutes"]),
    _tool("route_watches", "Список проверок маршрутов со статусами, последними результатами и ошибками."),
    _tool("route_cancel", "Отменить проверку маршрута по id из route_watches.", {"id": S}, ["id"]),
    _tool("route_traffic", "Дорожная обстановка на конкретном маршруте: участки с затруднениями, разница с типичным временем, "
          "перекрытия и неизвестные данные. Названия могут быть приблизительными: при неоднозначности вернутся кандидаты. "
          "Для «пробки в районе ТТК» без направления сначала уточни участок и откуда докуда. "
          "road фильтрует найденные участки маршрута; это не обзор всего кольца. via помогает явно задать путь.",
          {**TRIP, "area": S, "road": S}),
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class RouteError(Exception):
    pass


class QuotaError(RouteError):
    pass


class PlaceChoice(RouteError):
    def __init__(self, role, query, result):
        self.result = {"needs_clarification": True, "needs_answer": True, "endpoint": role, "query": str(query)[:300],
                       "question": ("Какое место вы имеете в виду: " + str(query)[:150] + "? Уточните район, адрес или выберите вариант."),
                       **result}


def skill_on(state):
    return (state or {}).get(SKILL_ID, True) is not False


def prompt_section(state=None):
    if not skill_on(state):
        return ""
    return ("МЕСТА И МАРШРУТЫ: route_places — сохранённые адреса, включая Дом/Работа из общих настроек. Если у адреса нет координат, найди его через route_find_place и попроси выбрать точку. Неизвестный Дом сначала спроси и сохрани через "
            "route_save_place. route_find_place ищет адреса; при неоднозначности (особенно Vegas на МКАД) уточни точку, "
            "не угадывай координаты. route_check возвращает варианты с пробками и временем проверки. Для «сообщи, когда "
            "до дома меньше часа» используй route_watch(threshold_minutes=60), а не обычное напоминание. "
            "Уведомление по самому быстрому допустимому варианту, строго меньше порога. Назови интервал (5 минут), "
            "срок наблюдения и фиксированную точку отправления. Не обещай GPS-слежение, точного совпадения с экраном "
            "Навигатора или запуска наблюдения, если инструмент вернул ошибку. Отсутствие подробной раскраски congestion "
            "не отменяет расчёт времени driving-traffic: называй его оценкой Mapbox, не подтверждением свободной дороги. "
            "Если поиск не сработал, назови сервис и точную безопасную ошибку инструмента (например HTTP 403), "
            "а пустой список кандидатов назови отсутствием результатов, не недоступностью поиска. "
            "ПРИМЕРНЫЕ НАЗВАНИЯ: передавай «Крокус Экспо», «Вегас», «кафе у работы» строкой в route_check/route_traffic; "
            "исполнитель сам ищет совпадения. Если ответ needs_clarification — задай question и предложи 2–5 кандидатов "
            "с адресами; после выбора используй их координаты. Не выбирай филиал по памяти и не выдумывай геопозицию. "
            "«У работы» задаёт ориентир поиска (near из сохранённой Работы), но не подменяет адрес кафе. "
            "Сохраняй привычные названия как aliases только по просьбе пользователя. "
            "ВОПРОСЫ О ПРОБКАХ: route_traffic возвращает сводку и bottlenecks по каждому варианту. "
            "Указывай участок, направление, время проверки, варианты и неизвестные данные. Разница с typical — "
            "это разница с обычной обстановкой, не задержка относительно пустой дороги. "
            "Для «пробки у ТТК / третьего кольца / МКАД» уточни сектор, внутреннюю/внешнюю сторону и откуда докуда; "
            "не считай один маршрут проверкой всего района. Если road не найден на пути, скажи это и уточни путь/via, "
            "не утверждай, что на этой дороге нет пробок. При пустых congestion говори «подробностей нет», а не «свободно». "
            "Не предлагай платные API или повышение тарифа: при лимите остановись. Ключи — Настройки → Ассистент → Места и маршруты.\n\n")


def voice_payload(state):
    return {"tools": TOOLS if skill_on(state) else [], "prompt": prompt_section(state)}


def _path(name=STORE_NAME):
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / name


def _read(name=STORE_NAME):
    try:
        return json.loads(_path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if name == KEYS_NAME else {"places": [], "watches": []}


def _write(data, name=STORE_NAME):
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


LIMITS = {"mapbox": 5000, "tomtom": 2000, "photon": 1000}
WINDOW = 32 * 86400  # rolling window, conservatively longer than a billing month


def _key(kind, settings=None):
    env_name = {"mapbox": "MAPBOX_TOKEN", "tomtom": "TOMTOM_KEY"}[kind]
    with _lock:
        value = os.environ.get(env_name) or _read(KEYS_NAME).get(kind, "")
    if kind == "tomtom":
        value = value or (settings or {}).get("tomtomKey", "")
    return value


def configure(inp):
    with _lock:
        keys = _read(KEYS_NAME)
        for kind in ("mapbox", "tomtom"):
            if kind in inp:
                keys[kind] = str(inp[kind] or "").strip()[:500]
        _write(keys, KEYS_NAME)
    return {"ok": True}


def _usage(data):
    now = time.time()
    return {kind: [t for t in data.get("usage", {}).get(kind, []) if now - WINDOW < t]
            for kind in LIMITS}


def _reserve(kind):
    # Reserve BEFORE any network call, including errors/timeouts; persisted across restart.
    with _lock:
        data = _read()
        usage = _usage(data)
        if len(usage[kind]) >= LIMITS[kind]:
            raise QuotaError("Бесплатный лимит " + kind + " исчерпан. Запросы остановлены; платного продолжения нет.")
        usage[kind].append(time.time())
        data["usage"] = usage
        _write(data)


def places(settings=None):
    with _lock:
        rows = copy.deepcopy(_read()["places"])
    for name, field in (("Дом", "homeAddress"), ("Работа", "workAddress")):
        address = str((settings or {}).get(field) or "").strip()
        if address:
            rows = [p for p in rows if not (p["name"].casefold() == name.casefold() and
                    "settings_address" in p and p["settings_address"] != address)]
        if address and not any(p["name"].casefold() == name.casefold() for p in rows):
            rows.append({"name": name, "address": address, "source": "settings", "needs_coordinates": True})
    return rows


def snapshot(settings=None):
    with _lock:
        data = copy.deepcopy(_read())
    for w in data["watches"]:
        w.pop("chat_id", None)
    data["places"] = places(settings)
    data.pop("search_cache", None)
    data["usage"] = {k: {"used": len(v), "limit": LIMITS[k], "window_days": 32} for k, v in _usage(data).items()}
    return {**data, "routing_ready": bool(_key("mapbox")), "geocoder_ready": bool(_key("tomtom", settings))}


def _number(value, lo, hi, label):
    if isinstance(value, bool):
        raise RouteError(label + ": нужно число.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise RouteError(label + ": нужно число.")
    if not math.isfinite(n) or not lo <= n <= hi:
        raise RouteError(label + ": значение вне допустимого диапазона.")
    return n


def point(value, settings=None):
    if isinstance(value, str):
        hits = [p for p in places(settings) if p["name"].casefold() == value.strip().casefold()]
        if not hits:
            raise RouteError("Место «" + value[:100] + "» не сохранено. Уточните адрес и используйте route_find_place.")
        if hits[0].get("needs_coordinates"):
            raise RouteError("Адрес «" + hits[0]["name"] + "» уже задан: " + hits[0]["address"] +
                             ". Найдите его через route_find_place и выберите точку, не спрашивайте адрес повторно.")
        return {"lat": hits[0]["lat"], "lon": hits[0]["lon"], "label": hits[0]["name"]}
    if not isinstance(value, dict):
        raise RouteError("Нужно сохранённое место или координаты.")
    return {"lat": _number(value.get("lat"), -90, 90, "Широта"),
            "lon": _number(value.get("lon"), -180, 180, "Долгота"), "label": str(value.get("label") or "Точка")[:200]}


def _record_check(kind, ok, message):
    with _lock:
        data = _read()
        data.setdefault("last_checks", {})[kind] = {"ok": ok, "message": message, "at": time.time()}
        _write(data)


def _get(url, params, kind):
    try:
        _reserve(kind)
        extra = {"headers": {"User-Agent": "Jarvis-place-search/1.0 (+https://github.com/silviatools/jarvis)"}} if kind == "photon" else {}
        r = requests.get(url, params=params, timeout=20, allow_redirects=False, **extra)
        if r.status_code in (402, 403, 429):
            raise QuotaError(kind + ": бесплатный доступ ограничен (HTTP " + str(r.status_code) +
                             "). Запросы остановлены. Проверьте ключ и квоту без перехода на платный тариф.")
        if r.status_code != 200:
            raise RouteError(kind + " API: HTTP " + str(r.status_code) + ". Проверьте ключ и бесплатную квоту. Платные тарифы не подключаются.")
        data = r.json()
        if not isinstance(data, dict) or data.get("error") or data.get("errors"):
            raise RouteError("Сервис карт не смог обработать запрос.")
        _record_check(kind, True, "Сервис ответил успешно (HTTP 200).")
        return data
    except RouteError as e:
        _record_check(kind, False, str(e))
        raise
    except (requests.RequestException, ValueError):
        _record_check(kind, False, "Не удалось получить свежие данные карт. Повторите позже.")
        raise RouteError("Не удалось получить свежие данные карт. Повторите позже.") from None


def _norm(value):
    text = re.sub(r"[^\w]+", " ", str(value or "").casefold().replace("ё", "е")).strip()
    return {"домой": "дом", "дома": "дом", "home": "дом", "на работу": "работа", "офис": "работа"}.get(text, text)


def _saved_candidates(query, settings=None):
    q = _norm(query)
    exact, approximate = [], []
    for row in places(settings):
        names = [_norm(row["name"])] + [_norm(a) for a in row.get("aliases", [])]
        hit = {**row, "label": row["name"], "source": "saved", "match": "exact"}
        if q in names:
            exact.append(hit)
        elif len(q) >= 4 and any((q in n or n in q or difflib.SequenceMatcher(None, q, n).ratio() >= 0.8)
                                for n in names if len(n) >= 4):
            approximate.append({**hit, "match": "approximate"})
    return exact or approximate


def _distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    return 6371000 * 2 * math.asin(min(1, math.sqrt(math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))


def _tomtom_search(q, settings, near):
    key = _key("tomtom", settings)
    if not key:
        return []
    data = _get("https://api.tomtom.com/search/2/search/" + quote(q, safe="") + ".json",
                {"key": key, "language": "ru-RU", "countrySet": "RU", "limit": 5,
                 "lat": near["lat"], "lon": near["lon"], "radius": 150000}, "tomtom")
    out = []
    for obj in data.get("results") or []:
        try:
            pos = obj["position"]
            address = obj.get("address", {}).get("freeformAddress", "")
            label = obj.get("poi", {}).get("name") or address or q
            out.append({**point({"lat": pos["lat"], "lon": pos["lon"], "label": label}),
                        "address": address, "precision": obj.get("type", "unknown"), "source": "TomTom Search"})
        except (KeyError, TypeError, RouteError):
            continue
    return out


def _photon_search(q, near):
    global _last_photon
    # Public demo: light use only, never called by the background route watcher.
    with _photon_lock:
        wait = 1 - (time.monotonic() - _last_photon)
        if wait > 0:
            time.sleep(wait)
        try:
            data = _get("https://photon.komoot.io/api/", {"q": q, "lat": near["lat"], "lon": near["lon"], "limit": 5}, "photon")
        finally:
            _last_photon = time.monotonic()
    out = []
    for feature in data.get("features") or []:
        try:
            lon, lat = feature["geometry"]["coordinates"][:2]
            props = feature.get("properties") or {}
            p = point({"lat": lat, "lon": lon, "label": props.get("name") or props.get("street") or q})
            if _distance(p, near) > 200000:
                continue
            address = ", ".join(str(props[k]) for k in ("city", "district", "street", "housenumber") if props.get(k))
            out.append({**p, "address": address, "precision": "Point Address" if props.get("housenumber") and not props.get("name") else "POI",
                        "source": "Photon / OpenStreetMap", "attribution": "© OpenStreetMap contributors"})
        except (KeyError, TypeError, ValueError, RouteError):
            continue
    return out


def find_place(query, settings=None, near=None):
    q = str(query or "").strip()[:300]
    if not q:
        raise RouteError("Введите адрес или название места.")
    saved = _saved_candidates(q, settings)
    if saved:
        return {"candidates": saved, "needs_choice": len(saved) != 1 or saved[0]["match"] != "exact",
                "confidence": "high" if len(saved) == 1 and saved[0]["match"] == "exact" else "needs_choice", "source": "saved"}
    bias = point(near) if near is not None else {"lat": 55.7558, "lon": 37.6176}
    cache_id = hashlib.sha256(json.dumps([q.casefold(), bias["lat"], bias["lon"]], ensure_ascii=False).encode()).hexdigest()
    with _lock:
        cached = _read().get("search_cache", {}).get(cache_id)
    if cached and time.time() - cached["at"] < 86400:
        return {**cached["result"], "cached": True}
    errors = []
    try:
        out = _tomtom_search(q, settings, bias)
    except RouteError as e:
        errors.append(str(e)); out = []
    if not out:
        try:
            out = _photon_search(q, bias)
        except RouteError as e:
            errors.append(str(e)); out = []
    if not out and errors:
        raise RouteError("Поиск мест не выполнен: " + "; ".join(errors))
    unique = {}
    for row in out:
        unique.setdefault((_norm(row["label"]), round(row["lat"], 5), round(row["lon"], 5)), row)
    out = list(unique.values())[:5]
    for row in out:
        row["distance_from_bias_km"] = round(_distance(row, bias) / 1000, 1)
    # A unique precise address or exact POI name can be resolved automatically.
    # Popular chains, streets and approximate matches always require a choice.
    high = False
    if len(out) == 1:
        row = out[0]
        exact_poi = row["precision"] == "POI" and len(_norm(q).split()) >= 2 and _norm(row["label"]) == _norm(q)
        exact_address = (row["precision"] == "Point Address" and bool(re.search(r"\d", q)) and
                         (_norm(q) == _norm(row["label"]) or _norm(q) in _norm(row["address"])))
        high = exact_poi or exact_address
    result = {"candidates": out, "needs_choice": bool(out) and not high,
              "confidence": "high" if high else "needs_choice", "source": out[0]["source"] if out else "place search",
              "message": ("Проверьте адрес; при неоднозначности уточните место." if out else
                          "Поиск ответил, но подходящих мест не нашёл. Уточните название, район или передайте координаты.")}
    if errors:
        result["provider_warnings"] = errors
    if out:
        with _lock:
            data = _read()
            cache = {k: v for k, v in data.get("search_cache", {}).items() if time.time() - v["at"] < 86400}
            cache[cache_id] = {"at": time.time(), "result": result}
            data["search_cache"] = dict(list(cache.items())[-100:])
            _write(data)
    return result


def _wide_area(value):
    return route_analysis.road_key(value) in ("ттк", "мкад", "третье кольцо", "садовое кольцо", "транспортное кольцо")


def resolve(value, settings=None, role="origin"):
    if not isinstance(value, str):
        return point(value)
    text = value.strip()
    coords = re.fullmatch(r"\s*([+-]?\d{1,3}(?:\.\d+)?)\s*[,;]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*", text)
    if coords:
        return point({"lat": coords[1], "lon": coords[2], "label": text})
    if _wide_area(text):
        raise PlaceChoice(role, text, {"candidates": [], "question": "Какой участок " + text +
                          " интересует: откуда докуда и какое направление (внутреннее/внешнее)?"})
    result = find_place(text, settings)
    rows = result["candidates"]
    if len(rows) == 1 and result.get("confidence") == "high" and rows[0].get("needs_coordinates"):
        result = find_place(rows[0]["address"], settings)
        rows = result["candidates"]
    if len(rows) != 1 or result.get("confidence") != "high" or rows[0].get("needs_coordinates"):
        raise PlaceChoice(role, text, result)
    return point(rows[0])


def _trip(inp, settings=None):
    via = inp.get("via") or []
    if not isinstance(via, list) or len(via) > 3:
        raise RouteError("via — список максимум из трёх промежуточных точек.")
    return (resolve(inp.get("origin"), settings, "origin"), resolve(inp.get("destination"), settings, "destination"),
            [resolve(v, settings, "via") for v in via])


def check(origin, destination, avoid_tolls=False, via=None, road=None, details=False):
    a, b = point(origin), point(destination)
    middle = [point(v) for v in (via or [])]
    if len(middle) > 3:
        raise RouteError("Можно задать максимум три промежуточные точки.")
    waypoints = [a] + middle + [b]
    key = _key("mapbox")
    if not key:
        raise RouteError("Нужен токен Mapbox Directions: Настройки → Ассистент → Места и маршруты. Используйте доступ без платёжного метода.")
    if not isinstance(avoid_tolls, bool):
        raise RouteError("avoid_tolls — true или false.")
    started = time.time()
    params = {"access_token": key, "alternatives": "true", "steps": "true", "overview": "full",
              "annotations": "congestion,distance,duration,closure", "geometries": "geojson", "depart_at": "now", "language": "ru"}
    if avoid_tolls:
        params["exclude"] = "toll"
    data = _get("https://api.mapbox.com/directions/v5/mapbox/driving-traffic/" +
                ";".join(f"{v['lon']},{v['lat']}" for v in waypoints), params, "mapbox")
    if data.get("code") != "Ok":
        raise RouteError("Mapbox не построил маршрут с пробками.")
    variants = []
    for i, route in enumerate(data.get("routes") or []):
        legs = route.get("legs") or []
        if not legs:
            continue
        try:
            duration = _number(route.get("duration"), 0.001, 604800, "Время")
            distance = _number(route.get("distance"), 0, 40000000, "Расстояние")
            typical = _number(route.get("duration_typical"), 0, 604800, "Типичное время")
        except RouteError:
            continue
        # Congestion annotations describe segments; they are not an ETA validity
        # flag. The traffic-profile ETA and its typical baseline were validated
        # above. Unknown annotations must not be described as empty roads.
        known_m, total_m = 0.0, 0.0
        for leg in legs:
            ann = leg.get("annotation") or {}
            levels, distances = ann.get("congestion") or [], ann.get("distance") or []
            for idx, level in enumerate(levels):
                if idx >= len(distances):
                    continue
                try:
                    meters = _number(distances[idx], 0, 40000000, "Расстояние")
                except RouteError:
                    continue
                total_m += meters
                if level in ("low", "moderate", "heavy", "severe"):
                    known_m += meters
        coverage = min(1, known_m / distance) if distance > 0 else 0
        traffic_details_available = known_m > 0
        steps = [s for l in legs for s in l.get("steps", [])]
        tolls = any("toll" in inter.get("classes", []) for s in steps for inter in s.get("intersections", []))
        if avoid_tolls and tolls:
            continue
        traffic = route_analysis.summarize(route, road)
        if not details:
            traffic.pop("segments", None)
        variants.append({"variant": i + 1, "duration_seconds": duration, "minutes": math.ceil(duration / 60),
                         "typical_minutes": math.ceil(typical / 60), "distance_km": round(distance / 1000, 1),
                         "has_tolls": tolls, "summary": "; ".join(str(l.get("summary") or "") for l in legs)[:250],
                         "traffic": traffic,
                         "traffic_coverage": round(coverage, 3), "watch_eligible": True,
                         "traffic_details_available": traffic_details_available,
                         "traffic_warning": ("" if traffic_details_available else
                             "Подробная загруженность участков недоступна. Время — оценка Mapbox, не подтверждение свободной дороги.")})
    if not variants or time.time() - started > 60:
        raise RouteError("Нет свежего допустимого маршрута с пробками.")
    variants.sort(key=lambda r: r["duration_seconds"])
    url = "https://yandex.ru/maps/?" + urlencode({"rtext": "~".join(f"{v['lat']},{v['lon']}" for v in waypoints), "rtt": "auto"})
    return {"ok": True, "origin": a, "destination": b, "via": middle, "routes": variants, "checked_at": time.time(),
            "traffic_type": "live_and_typical", "source": "Mapbox driving-traffic", "url": url,
            "watch_eligible": any(r["watch_eligible"] for r in variants),
            "note": "Оценка Mapbox сочетает доступные пробки и статистику; свежесть каждого участка API не сообщает. "
                    "Отсутствие подробностей загруженности не отменяет оценку времени driving-traffic. "
                    "Неизвестные пробки не считаются свободной дорогой. Время может отличаться от Яндекс Навигатора; ссылка открывает отдельный расчёт."}


def execute(name, inp, state=None, chat_id=None, unattended=False, settings=None):
    if not skill_on(state):
        return {"error": "Навык «Места, маршруты и пробки» выключен."}
    inp = inp or {}
    try:
        if name == "route_find_place":
            return find_place(inp.get("query"), settings, inp.get("near"))
        if name == "route_places":
            return {"places": places(settings)}
        if name in ("route_save_place", "route_delete_place"):
            label = str(inp.get("name") or "").strip()[:100]
            if not label:
                raise RouteError("Укажите имя места.")
            p = point(inp.get("point")) if name == "route_save_place" else None
            aliases = inp.get("aliases")
            if aliases is not None and (not isinstance(aliases, list) or len(aliases) > 10 or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in aliases)):
                raise RouteError("aliases — список до 10 непустых названий длиной до 100 символов.")
            with _lock:
                data = _read()
                if aliases is None:
                    previous = next((x for x in data["places"] if x["name"].casefold() == label.casefold()), {})
                    aliases = previous.get("aliases", [])
                saved_places = [x for x in data["places"] if x["name"].casefold() != label.casefold()]
                if p:
                    if len(saved_places) >= 100:
                        raise RouteError("Можно сохранить до 100 мест.")
                    proposed = {_norm(label)} | {_norm(a) for a in aliases}
                    if any(proposed & ({_norm(x["name"])} | {_norm(a) for a in x.get("aliases", [])}) for x in saved_places):
                        raise RouteError("Такое название или алиас уже принадлежит другому месту. Уточните название.")
                    saved = {"name": label, "address": str(inp.get("address") or "")[:300], "lat": p["lat"], "lon": p["lon"],
                             "aliases": list(dict.fromkeys(a.strip() for a in aliases))}
                    field = {"дом": "homeAddress", "работа": "workAddress"}.get(label.casefold())
                    if field and (settings or {}).get(field):
                        saved["settings_address"] = str(settings[field]).strip()
                    saved_places.append(saved)
                data["places"] = saved_places
                _write(data)
            return {"ok": True, "name": label}
        if name in ("route_check", "route_traffic"):
            if name == "route_traffic" and (not inp.get("origin") or not inp.get("destination")):
                return {"needs_clarification": True, "needs_answer": True,
                        "question": "Какой участок " + str(inp.get("area") or inp.get("road") or "дороги")[:150] +
                                    " интересует: откуда докуда и в каком направлении (внутреннее/внешнее для кольца)?",
                        "scope": "Нужен конкретный маршрут или участок; весь район ещё не проверен."}
            a, b, via = _trip(inp, settings)
            return check(a, b, inp.get("avoid_tolls", False), via, inp.get("road") or inp.get("area"), name == "route_traffic")
        if name == "route_watch":
            if unattended:
                raise RouteError("Во время фоновой задачи новые наблюдения создавать нельзя.")
            if not chat_id:
                raise RouteError("Нет личного Telegram-чата для уведомления. Подключите личный Telegram или задайте просьбу в боте.")
            threshold = _number(inp.get("threshold_minutes"), 1, 1440, "Порог в минутах")
            hours = _number(inp.get("expires_in_hours", 12), 0.1, 24, "Срок в часах")
            a, b, via = _trip(inp, settings)
            result = check(a, b, inp.get("avoid_tolls", False), via)
            if not result["watch_eligible"]:
                raise RouteError("Недостаточно данных о пробках на маршруте. Наблюдение не запущено.")
            now = time.time()
            w = {"id": uuid.uuid4().hex[:12], "origin": result["origin"], "destination": result["destination"],
                 "via": via,
                 "threshold_minutes": threshold, "avoid_tolls": inp.get("avoid_tolls", False), "chat_id": chat_id,
                 "created_at": now, "expires_at": now + hours * 3600, "next_check": now, "status": "watching",
                 "last_result": result, "last_error": ""}
            with _lock:
                data = _read()
                if sum(x["status"] == "watching" for x in data["watches"]) >= 20:
                    raise RouteError("Уже есть 20 активных наблюдений. Отмените ненужные.")
                data["watches"] = [x for x in data["watches"] if x["status"] == "watching"] + [
                    x for x in data["watches"] if x["status"] != "watching"][-79:] + [w]
                _write(data)
            return {"ok": True, "id": w["id"], "interval_minutes": 5, "expires_in_hours": hours,
                    "already_below": any(r["watch_eligible"] and r["duration_seconds"] < threshold * 60 for r in result["routes"]), "current": result}
        if name == "route_watches":
            return {"watches": snapshot()["watches"]}
        if name == "route_cancel":
            with _lock:
                data = _read()
                w = next((x for x in data["watches"] if x["id"] == inp.get("id")), None)
                if not w:
                    raise RouteError("Наблюдение не найдено.")
                w.update(status="cancelled", finished_at=time.time())
                _write(data)
            return {"ok": True}
        return {"error": "Неизвестный инструмент маршрутов."}
    except PlaceChoice as e:
        return e.result
    except RouteError as e:
        return {"error": str(e)}


def poll(load_app, notify):
    """One serialized pass; network work runs outside the global data lock."""
    if not _worker_lock.acquire(blocking=False):
        return
    try:
        if not skill_on((load_app() or {}).get("telegramSkills")):
            return
        with _lock:
            watches = copy.deepcopy(_read()["watches"])
        for old in watches:
            now = time.time()
            if old["status"] != "watching" or old["next_check"] > now:
                continue
            result, error, message, terminal = None, "", "", ""
            if old["expires_at"] <= now:
                terminal = "expired"
                message = html.escape(old["origin"]["label"] + " → " + old["destination"]["label"]) + ": Проверка маршрута завершена по сроку. Условие «меньше " + str(old["threshold_minutes"]) + " мин» не подтверждено."
            else:
                try:
                    result = check(old["origin"], old["destination"], old["avoid_tolls"], old.get("via"))
                    eligible = [r for r in result["routes"] if r["watch_eligible"]]
                    best = eligible[0] if eligible else None
                    if not best:
                        error = "Недостаточно данных о пробках. Условие не подтверждено."
                    if best and best["duration_seconds"] < old["threshold_minutes"] * 60:
                        terminal = "done"
                        traffic = "по оценке Mapbox с пробками и статистикой"
                        seconds = int(best["duration_seconds"])
                        duration_text = f"{seconds // 60} мин {seconds % 60} с"
                        times = ", ".join(str(r["minutes"]) + " мин" + (" (платный)" if r["has_tolls"] else "") for r in result["routes"])
                        message = (f"🚗 {html.escape(old['origin']['label'])} → {html.escape(old['destination']['label'])}: "
                                   f"{duration_text}, {best['distance_km']} км {traffic}. "
                                   f"Время строго меньше {old['threshold_minutes']:g} мин.\nВарианты: {times}.\n"
                                   f'<a href="{html.escape(result["url"], quote=True)}">Открыть в Яндекс Картах</a>')
                        if best.get("traffic_warning"):
                            message += "\n" + html.escape(best["traffic_warning"])
                except QuotaError as e:
                    terminal = "quota_exhausted"
                    error = str(e)
                    message = html.escape(old["origin"]["label"] + " → " + old["destination"]["label"] + ": " + error)
                except Exception:
                    error = "Не удалось проверить маршрут. Следующая попытка через 5 минут."
            with _lock:
                data = _read()
                w = next((x for x in data["watches"] if x["id"] == old["id"]), None)
                if not w or w["status"] != "watching" or not skill_on((load_app() or {}).get("telegramSkills")):
                    continue
                w.update(next_check=time.time() + 300, last_error=error)
                if result:
                    w["last_result"] = result
                # The data lock serializes cancellation with notification delivery.
                if message:
                    try:
                        if notify(w["chat_id"], message):
                            w.update(status=terminal, finished_at=time.time())
                        else:
                            w["last_error"] = "Уведомление не доставлено. Повторим проверку и отправку."
                    except Exception:
                        w["last_error"] = "Не удалось отправить уведомление. Повторим."
                _write(data)
    finally:
        _worker_lock.release()


def tick(load_app, notify):
    if not _worker_lock.locked() and skill_on((load_app() or {}).get("telegramSkills")):
        threading.Thread(target=poll, args=(load_app, notify), daemon=True, name="route-watch").start()
