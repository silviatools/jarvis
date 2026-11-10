"""Saved places, free-quota Mapbox traffic routes and persistent conditional notifications.

Provider contracts: https://docs.mapbox.com/api/navigation/directions/
and https://docs.tomtom.com/search-api/documentation/search-service/fuzzy-search. Keys stay on the server and are excluded from backups.
"""
import copy
import html
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

STORE_NAME = "jarvis_routes.json"
KEYS_NAME = "jarvis_route_keys.json"
SKILL_ID = "routes"
_lock = threading.RLock()
_worker_lock = threading.Lock()
SKILLS = [{"id": SKILL_ID, "title": "Места, маршруты и пробки", "risk": "create", "default": True,
           "desc": "Запоминает адреса «Дом», «Работа»; сравнивает автомобильные маршруты Mapbox с пробками. "
                   "«Сообщи, когда от Vegas до дома будет меньше часа» — проверяет каждые 5 минут, "
                   "присылает одно сообщение в Telegram. Отслеживается выбранная точка отправления, не перемещение телефона. "
                   "Ключи API и адреса: Настройки → Ассистент → Места и маршруты. Только бесплатная квота: при лимите запросы останавливаются. Поиск — существующий ключ TomTom.",
           "tools": ["route_places", "route_save_place", "route_delete_place", "route_find_place", "route_check",
                     "route_watch", "route_watches", "route_cancel"]}]


def _tool(name, desc, props=None, required=None):
    return {"name": name, "description": desc, "input_schema": {"type": "object", "properties": props or {},
                                                                 "required": required or []}}


S = {"type": "string"}
POINT = {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "label": S},
         "required": ["lat", "lon"]}
END = {"oneOf": [S, POINT], "description": "Точное имя сохранённого места или координаты выбранного результата поиска."}
TRIP = {"origin": END, "destination": END, "avoid_tolls": {"type": "boolean"}}
TOOLS = [
    _tool("route_places", "Сохранённые места: Дом, Работа и другие адреса."),
    _tool("route_find_place", "Найти адрес/место в Москве и области. При нескольких подходящих результатах спроси пользователя; "
          "не выбирай Vegas на МКАД наугад. Координаты результата передай в route_check / route_watch.", {"query": S}, ["query"]),
    _tool("route_save_place", "Запомнить место под именем (например, Дом). Координаты бери из route_find_place или от пользователя. "
          "Не угадывай адрес. Повторное имя заменяет сохранённый адрес.", {"name": S, "address": S, "point": POINT}, ["name", "point"]),
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
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class RouteError(Exception):
    pass


class QuotaError(RouteError):
    pass


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
            "Навигатора или запуска наблюдения, если инструмент вернул ошибку. Не предлагай платные API или повышение тарифа: при лимите остановись. Ключи — Настройки → Ассистент → Места и маршруты.\n\n")


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


LIMITS = {"mapbox": 5000, "tomtom": 2000}  # below card-free demo/search allowances
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
        for kind in LIMITS:
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


def _get(url, params, kind):
    _reserve(kind)
    try:
        r = requests.get(url, params=params, timeout=20, allow_redirects=False)
        if r.status_code in (402, 403, 429):
            raise QuotaError(kind + ": бесплатный доступ ограничен (HTTP " + str(r.status_code) +
                             "). Запросы остановлены. Проверьте ключ и квоту без перехода на платный тариф.")
        if r.status_code != 200:
            raise RouteError(kind + " API: HTTP " + str(r.status_code) + ". Проверьте ключ и бесплатную квоту. Платные тарифы не подключаются.")
        data = r.json()
        if not isinstance(data, dict) or data.get("error") or data.get("errors"):
            raise RouteError("Сервис карт не смог обработать запрос.")
        return data
    except (requests.RequestException, ValueError):
        raise RouteError("Не удалось получить свежие данные карт. Повторите позже.") from None


def find_place(query, settings=None):
    key = _key("tomtom", settings)
    if not key:
        raise RouteError("Для поиска используйте бесплатный ключ TomTom из Настройки → Общее → Дорога до работы; или укажите координаты места вручную.")
    q = str(query or "").strip()[:300]
    if not q:
        raise RouteError("Введите адрес или название места.")
    data = _get("https://api.tomtom.com/search/2/search/" + quote(q, safe="") + ".json",
                {"key": key, "language": "ru-RU", "countrySet": "RU", "limit": 5,
                 "lat": 55.7558, "lon": 37.6176, "radius": 150000}, "tomtom")
    out = []
    for obj in data.get("results", []):
        try:
            pos = obj["position"]
            address = obj.get("address", {}).get("freeformAddress", "")
            label = obj.get("poi", {}).get("name") or address or q
            out.append({**point({"lat": pos["lat"], "lon": pos["lon"], "label": label}),
                        "address": address, "precision": obj.get("type", "unknown")})
        except (KeyError, TypeError, RouteError):
            continue
    return {"candidates": out, "needs_choice": len(out) > 1,
            "message": "Выберите нужный адрес. Поиск: TomTom. При неоднозначности не выбирайте автоматически."}


def check(origin, destination, avoid_tolls=False):
    a, b = point(origin), point(destination)
    key = _key("mapbox")
    if not key:
        raise RouteError("Нужен токен Mapbox Directions: Настройки → Ассистент → Места и маршруты. Используйте доступ без платёжного метода.")
    if not isinstance(avoid_tolls, bool):
        raise RouteError("avoid_tolls — true или false.")
    started = time.time()
    params = {"access_token": key, "alternatives": "true", "steps": "true", "overview": "full",
              "annotations": "congestion,distance", "depart_at": "now", "language": "ru"}
    if avoid_tolls:
        params["exclude"] = "toll"
    data = _get("https://api.mapbox.com/directions/v5/mapbox/driving-traffic/" +
                f"{a['lon']},{a['lat']};{b['lon']},{b['lat']}", params, "mapbox")
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
        # Unknown congestion must not be interpreted as an empty road.
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
        eligible = coverage >= 0.8 and total_m >= distance * 0.8
        steps = [s for l in legs for s in l.get("steps", [])]
        tolls = any("toll" in inter.get("classes", []) for s in steps for inter in s.get("intersections", []))
        if avoid_tolls and tolls:
            continue
        variants.append({"variant": i + 1, "duration_seconds": duration, "minutes": math.ceil(duration / 60),
                         "typical_minutes": math.ceil(typical / 60), "distance_km": round(distance / 1000, 1),
                         "has_tolls": tolls, "summary": "; ".join(str(l.get("summary") or "") for l in legs)[:250],
                         "traffic_coverage": round(coverage, 3), "watch_eligible": eligible})
    if not variants or time.time() - started > 60:
        raise RouteError("Нет свежего допустимого маршрута с пробками.")
    variants.sort(key=lambda r: r["duration_seconds"])
    url = "https://yandex.ru/maps/?" + urlencode({"rtext": f"{a['lat']},{a['lon']}~{b['lat']},{b['lon']}", "rtt": "auto"})
    return {"ok": True, "origin": a, "destination": b, "routes": variants, "checked_at": time.time(),
            "traffic_type": "live_and_typical", "source": "Mapbox driving-traffic", "url": url,
            "watch_eligible": any(r["watch_eligible"] for r in variants),
            "note": "Оценка Mapbox сочетает доступные пробки и статистику; свежесть каждого участка API не сообщает. "
                    "Неизвестные пробки не считаются свободной дорогой. Время может отличаться от Яндекс Навигатора; ссылка открывает отдельный расчёт."}


def execute(name, inp, state=None, chat_id=None, unattended=False, settings=None):
    if not skill_on(state):
        return {"error": "Навык «Места, маршруты и пробки» выключен."}
    inp = inp or {}
    try:
        if name == "route_find_place":
            return find_place(inp.get("query"), settings)
        if name == "route_places":
            return {"places": places(settings)}
        if name in ("route_save_place", "route_delete_place"):
            label = str(inp.get("name") or "").strip()[:100]
            if not label:
                raise RouteError("Укажите имя места.")
            p = point(inp.get("point")) if name == "route_save_place" else None
            with _lock:
                data = _read()
                saved_places = [x for x in data["places"] if x["name"].casefold() != label.casefold()]
                if p:
                    if len(saved_places) >= 100:
                        raise RouteError("Можно сохранить до 100 мест.")
                    saved = {"name": label, "address": str(inp.get("address") or "")[:300], "lat": p["lat"], "lon": p["lon"]}
                    field = {"дом": "homeAddress", "работа": "workAddress"}.get(label.casefold())
                    if field and (settings or {}).get(field):
                        saved["settings_address"] = str(settings[field]).strip()
                    saved_places.append(saved)
                data["places"] = saved_places
                _write(data)
            return {"ok": True, "name": label}
        if name == "route_check":
            return check(point(inp.get("origin"), settings), point(inp.get("destination"), settings), inp.get("avoid_tolls", False))
        if name == "route_watch":
            if unattended:
                raise RouteError("Во время фоновой задачи новые наблюдения создавать нельзя.")
            if not chat_id:
                raise RouteError("Нет личного Telegram-чата для уведомления. Подключите личный Telegram или задайте просьбу в боте.")
            threshold = _number(inp.get("threshold_minutes"), 1, 1440, "Порог в минутах")
            hours = _number(inp.get("expires_in_hours", 12), 0.1, 24, "Срок в часах")
            result = check(point(inp.get("origin"), settings), point(inp.get("destination"), settings), inp.get("avoid_tolls", False))
            if not result["watch_eligible"]:
                raise RouteError("Недостаточно данных о пробках на маршруте. Наблюдение не запущено.")
            now = time.time()
            w = {"id": uuid.uuid4().hex[:12], "origin": result["origin"], "destination": result["destination"],
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
                    result = check(old["origin"], old["destination"], old["avoid_tolls"])
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
