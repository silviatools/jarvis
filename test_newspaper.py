"""Утренняя газета: реальные структуры данных, внешние ответы подменены."""

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


if __name__ == "__main__":
    unittest.main()
