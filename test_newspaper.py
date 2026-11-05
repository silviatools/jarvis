"""Утренняя газета: реальные структуры данных, внешние ответы подменены."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import newspaper


class Response:
    def __init__(self, *, content=b"", data=None, status=200):
        self.content = content
        self._data = data
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return self._data


class Requests:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "news.google.com" in url:
            items = []
            for i in range(1, 6):
                day = "05" if i < 5 else "03"  # пятая заметка старше суток
                items.append(
                    f"<item><title>Новость {i} - Источник {i}</title>"
                    f"<link>https://example.test/{i}</link>"
                    f"<pubDate>Thu, {day} Nov 2026 03:0{i}:00 GMT</pubDate></item>"
                )
            return Response(content=("<rss><channel>" + "".join(items) + "</channel></rss>").encode())
        if "nominatim" in url:
            return Response(data=[{"lat": "55.75", "lon": "37.61", "display_name": "Москва, Россия"}])
        if "open-meteo" in url:
            hours = [f"2026-11-05T{h:02d}:00" for h in range(24)]
            return Response(data={
                "current": {"temperature_2m": 2.4, "weather_code": 2, "wind_speed_10m": 3.2, "precipitation": 0},
                "hourly": {"time": hours, "temperature_2m": list(range(24)),
                           "precipitation_probability": [10] * 24, "wind_speed_10m": [4] * 24},
                "daily": {"precipitation_sum": [0.2], "precipitation_probability_max": [10]},
            })
        raise AssertionError(url)


class NewspaperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache.json"
        self.now = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)
        self.app = {
            "settings": {"weatherCity": "Москва"},
            "newspaper": {"title": "Моя газета", "fontSize": "xlarge"},
            "bossTasks": [
                {"name": "Зарядка", "period": "morning", "days": [4]},
                {"name": "Подвести итоги", "period": "evening", "days": [4]},
                {"name": "Архив", "period": "morning", "days": [4], "archived": True},
            ],
        }

    def test_collects_today_from_every_source(self):
        requests = Requests()
        payload = newspaper.collect(
            self.app, requests,
            lambda day: {"events": [{"title": "Планёрка", "start": "2026-11-05 10:00", "calendar": "Работа"}]},
            self.cache, now=self.now,
        )
        self.assertEqual(payload["boss"], {"morning": ["Зарядка"], "evening": ["Подвести итоги"]})
        self.assertEqual([x["title"] for x in payload["news"]], ["Новость 1", "Новость 2", "Новость 3", "Новость 4"])
        self.assertEqual(payload["weather"]["day"], 14)
        self.assertEqual(payload["weather"]["evening"], 20)
        self.assertEqual(payload["events"][0]["title"], "Планёрка")
        self.assertEqual(payload["issues"], [])
        self.assertTrue(self.cache.exists())

    def test_layout_is_large_a4_and_escapes_personal_data(self):
        payload = newspaper.collect(
            self.app, Requests(),
            lambda day: {"events": [{"title": "<script>нет</script>", "start": "2026-11-05 12:00"}]},
            self.cache, now=self.now,
        )
        page = newspaper.render_html(payload)
        self.assertIn("@page{size:A4", page)
        self.assertIn('class="page xlarge"', page)
        self.assertIn("font-size:6.9mm", page)
        self.assertIn("&lt;script&gt;нет&lt;/script&gt;", page)
        self.assertNotIn("<script>нет</script>", page)

    def test_source_failure_keeps_last_successful_news(self):
        first = newspaper.collect(self.app, Requests(), lambda day: {"events": []}, self.cache, now=self.now)

        class Offline:
            def get(self, *args, **kwargs):
                raise RuntimeError("offline")

        second = newspaper.collect(self.app, Offline(), lambda day: {"events": []}, self.cache, now=self.now)
        self.assertEqual(second["news"], first["news"])
        self.assertTrue(any(x.startswith("Новости:") for x in second["issues"]))

    def test_railway_uses_full_chromium_channel_without_headless_shell(self):
        with patch.dict("os.environ", {"CHROMIUM_EXECUTABLE": "", "CHROMIUM_PATH": ""}), \
             patch("newspaper.shutil.which", return_value=None):
            launch = newspaper.chromium_launch_kwargs()
        self.assertEqual(launch["channel"], "chromium")
        self.assertNotIn("executable_path", launch)

    def test_print_settings_are_safe_and_enabled_by_default(self):
        cfg = newspaper.settings({"newspaper": {"printerHost": "192.168.1.45", "printerPath": "ipp/print"}})
        self.assertTrue(cfg["autoPrint"])
        self.assertEqual(cfg["printerPort"], 631)
        self.assertEqual(cfg["printerPath"], "/ipp/print")
        self.assertEqual(newspaper.settings({"newspaper": {"printerHost": "http://bad/path"}})["printerHost"], "")


class BlocksTest(unittest.TestCase):
    def setUp(self):
        NewspaperTest.setUp(self)
    def test_precip_outlook_uses_weather_codes(self):
        self.assertEqual(newspaper.precip_outlook([71, 3], [10, 80])["precipKind"], "снег")
        self.assertEqual(newspaper.precip_outlook([71, 3], [10, 80])["precipProbability"], 80)
        self.assertEqual(newspaper.precip_outlook([61], [40])["precipKind"], "дождь")
        self.assertEqual(newspaper.precip_outlook([2, 3], [10])["precipKind"], "нет")
        self.assertEqual(newspaper.precip_outlook([61, 73], [20, 70])["precipKind"], "снег и дождь")

    def test_calendar_weather_news_and_telegram_are_not_cut(self):
        prompts = []

        def complete(system, user):
            prompts.append((system, user))
            if "непрочитан" in system:
                self.assertNotIn("мой ответ не просьба", user)
                self.assertNotIn("hunter2", user)
                self.assertNotIn("918273", user)
                return json.dumps([
                    "просит ссылку на ножик",
                    "срочно нужно подтвердить доставку",
                    "сказал что опоздает сегодня",
                    "пароль hunter2 и код 918273",
                    "просят согласовать время созвона",
                ], ensure_ascii=False)
            return json.dumps([
                "Первая новость изложена спокойно. Второе предложение добавляет контекст заголовка. Третье не придумывает фактов.",
                "Вторая новость коротко пересказана. Второе предложение держится заголовка.",
                "Третья новость звучит сдержанно. Второе предложение не добавляет чисел.",
                "Четвёртая новость остаётся в рамках заголовка. Второе предложение завершает пересказ.",
            ], ensure_ascii=False)

        def telegram():
            return {"connected": True, "chats": [
                {"name": "Егор брат", "kind": "user", "messages": [
                    {"from": "Егор брат", "text": "скинь ссылку на ножик", "mine": False},
                    {"from": "я", "text": "мой ответ не просьба", "mine": True},
                ]},
                {"name": "Валя Ассистент", "kind": "user", "messages": [
                    {"from": "Валя Ассистент", "text": "срочно подтверди доставку", "mine": False},
                ]},
                {"name": "Игорь Влдмтель", "kind": "user", "messages": [
                    {"from": "Игорь Влдмтель", "text": "опоздаю сегодня", "mine": False},
                ]},
                {"name": "Секрет", "kind": "user", "messages": [
                    {"from": "Секрет", "text": "пароль: hunter2, код 918273", "mine": False},
                ]},
                {"name": "Семейный чат", "kind": "group", "messages": [
                    {"from": "Мама", "text": "давай созвон в восемь", "mine": False},
                ]},
            ]}

        long = "Очень длинное название события которое раньше обрезали бы на полуслове и прятали хвост"
        events = [{"title": long, "start": "2026-11-05 08:05", "calendar": "Дом"}]
        events += [
            {"title": f"Событие {i:02d}", "start": f"2026-11-05 {9 + i // 2:02d}:{(i % 2) * 30:02d}", "calendar": "Работа"}
            for i in range(1, 14)
        ]
        events.append({"title": "Подготовка", "allDay": True, "start": "2026-11-05", "calendar": "Дом"})

        class Snow(Requests):
            def get(self, url, **kwargs):
                if "open-meteo" in url:
                    self.meteo = kwargs.get("params") or {}
                    hours = [f"2026-11-05T{h:02d}:00" for h in range(24)]
                    return Response(data={
                        "current": {"temperature_2m": -3.2, "weather_code": 71, "wind_speed_10m": 4, "precipitation": 1.2},
                        "hourly": {
                            "time": hours,
                            "temperature_2m": [-4] * 24,
                            "precipitation_probability": [15] * 8 + [80] * 8 + [20] * 8,
                            "weather_code": [3] * 8 + [71] * 8 + [3] * 8,
                            "wind_speed_10m": [4] * 24,
                        },
                        "daily": {"precipitation_sum": [4], "precipitation_probability_max": [80]},
                    })
                return super().get(url, **kwargs)

        req = Snow()
        payload = newspaper.collect(
            self.app, req, lambda day: {"events": events}, self.cache, now=self.now,
            complete=complete, telegram=telegram,
        )
        self.assertEqual(len(payload["events"]), 15)
        self.assertIn("Первая новость изложена спокойно.", payload["news"][0]["summary"])
        self.assertEqual(payload["weather"]["precipKind"], "снег")
        self.assertEqual(payload["weather"]["precipProbability"], 80)
        self.assertIn("weather_code", req.meteo["hourly"])
        self.assertIn("precipitation_probability", req.meteo["hourly"])
        self.assertIn("weather_code", req.meteo["current"])
        page = newspaper.render_html(payload)
        self.assertIn(long, page)
        for i in range(1, 14):
            self.assertIn(f"Событие {i:02d}", page)
        self.assertIn("08:05", page)
        self.assertIn("весь день", page)
        self.assertNotIn("и ещё", page)
        self.assertNotRegex(page, r"ещё \d")
        self.assertNotIn("overflow:hidden", page)
        self.assertIn("min-height:297mm", page)
        self.assertIn("@page{size:A4", page)
        self.assertIn("снег, вероятность 80%", page)
        self.assertNotIn("hunter2", page)
        self.assertNotIn("918273", page)
        self.assertIn("Егор брат — просит ссылку на ножик", page)
        self.assertIn("Валя Ассистент — срочно нужно подтвердить доставку", page)
        self.assertIn("Игорь Влдмтель — сказал что опоздает сегодня", page)
        self.assertIn("Семейный чат — просят согласовать время созвона", page)
        bottom = page.split('class="bottom"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(bottom.count("<article"), 1)
        self.assertIn("Цитата дня", bottom)
        self.assertIn("Интересный факт", bottom)
        self.assertNotIn("min-height:36mm", page)
        self.assertNotIn("min-height:91mm", page)
        joined = "\n".join(system for system, _user in prompts)
        self.assertIn("непрочитан", joined)
        self.assertNotIn("gpt-", joined.lower())

    def test_clear_sky_says_no_precipitation_and_keeps_empty_telegram(self):
        payload = newspaper.collect(
            self.app, Requests(), lambda day: {"events": []}, self.cache, now=self.now,
            telegram=lambda: {"connected": True, "chats": []},
        )
        page = newspaper.render_html(payload)
        self.assertIn("осадков нет, вероятность 10%", page)
        self.assertIn("непрочитанных нет", page)
        self.assertIn('class="telegram"', page)

    def test_missing_telegram_session_still_builds_the_issue(self):
        payload = newspaper.collect(
            self.app, Requests(), lambda day: {"events": []}, self.cache, now=self.now,
        )
        page = newspaper.render_html(payload)
        self.assertIn("Telegram не подключён", page)
        self.assertIn("Новость 1", page)
        self.assertEqual(payload["issues"], [])

    def test_own_messages_are_not_requests(self):
        def complete(system, user):
            if "непрочитан" in system:
                raise AssertionError(user)
            return json.dumps(["а. б.", "а. б.", "а. б.", "а. б."], ensure_ascii=False)

        payload = newspaper.collect(
            self.app, Requests(), lambda day: {"events": []}, self.cache, now=self.now,
            complete=complete,
            telegram=lambda: {"connected": True, "chats": [
                {"name": "Заметки", "kind": "user", "messages": [{"from": "я", "text": "заметка себе", "mine": True}]},
            ]},
        )
        self.assertEqual(payload["telegram"], ["непрочитанных нет"])

    def test_block_defaults_merge_quote_and_fact(self):
        cfg = newspaper.settings({})
        self.assertTrue(cfg["telegram"])
        self.assertTrue(cfg["aside"])
        self.assertNotIn("quote", cfg)
        self.assertNotIn("fact", cfg)
        self.assertFalse(newspaper.settings({"newspaper": {"quote": False, "fact": False}})["aside"])
        self.assertTrue(newspaper.settings({"newspaper": {"quote": False, "fact": True}})["aside"])
        self.assertFalse(newspaper.settings({"newspaper": {"aside": False, "quote": True}})["aside"])
        page = Path("index (9).html").read_text(encoding="utf-8")
        card = page.split("function NewspaperSettings")[1].split("function WeatherCalendarSettings")[0]
        self.assertIn("Непрочитанное в Telegram", card)
        self.assertIn("Цитата дня и интересный факт", card)
        self.assertNotIn("['quote'", card)
        self.assertNotIn("['fact'", card)

    def test_background_role_is_taken_from_settings(self):
        import jarvis
        captured = {}

        def conf(app, role="main"):
            captured["role"] = role
            return jarvis._AiConf("openai", "test-key", "model-from-settings", "", "effort-from-settings", role)

        def llm(ai_conf, system, user, max_tokens=2500, timeout=120, func=""):
            captured["model"] = ai_conf.model
            captured["effort"] = ai_conf.effort
            captured["func"] = func
            return "[]"

        with patch.object(jarvis, "_ai_conf", conf), patch.object(jarvis, "_llm_text", llm):
            self.assertEqual(jarvis._newspaper_complete({"settings": {}})("sys", "user"), "[]")
        self.assertEqual(captured["role"], "background")
        self.assertEqual(captured["model"], "model-from-settings")
        self.assertEqual(captured["effort"], "effort-from-settings")
        self.assertEqual(captured["func"], "newspaper")
        src = Path("newspaper.py").read_text(encoding="utf-8")
        self.assertNotIn("gpt-", src.lower())
        self.assertNotIn("reasoning_effort", src)

    def test_telegram_reader_does_not_start_login(self):
        import tg_tools
        from tg_user import TgError

        with patch.object(tg_tools, "TelegramClient", None):
            self.assertEqual(tg_tools.newspaper_unread(), {"connected": False, "chats": []})

        def fake_run(coro, timeout=40):
            coro.close()
            raise TgError("Личный Telegram не подключён: Настройки → Телеграм.")

        with patch.object(tg_tools, "TelegramClient", object()), patch.object(tg_tools, "_run", fake_run):
            self.assertEqual(tg_tools.newspaper_unread(), {"connected": False, "chats": []})


if __name__ == "__main__":
    unittest.main()
