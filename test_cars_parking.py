"""Свои авто уже в гараже. Здесь — резидентские разрешения, файлы и напоминания."""

import io
import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import cars_parking
import jarvis

CAR = "11111111-1111-4111-8111-111111111111"
CAR2 = "22222222-2222-4222-8222-222222222222"
def _png(w, h, rgb):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png(96, 64, (232, 214, 176))
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def permit(**extra):
    body = {
        "carId": CAR,
        "number": "77-100",
        "starts": "2026-01-01",
        "ends": "2026-12-02",
        "zone": "ЦАО",
        "address": "улица Пятницкая, 10",
        "spot": "12",
        "holder": "Иванов",
        "notes": "резидент",
        "reminders": [
            {"mode": "days", "daysBefore": 14, "time": "10:00", "text": "Продли разрешение"},
            {"mode": "date", "date": "2026-11-18", "time": "10:00", "text": "Проверь бланк <b>"},
        ],
    }
    body.update(extra)
    return body


class ParkingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def test_several_cars_and_permits(self):
        code, first = cars_parking.save_permit(permit())
        self.assertEqual(code, 200, first)
        code, second = cars_parking.save_permit(permit(carId=CAR, number="77-200", ends="2027-01-01", reminders=[]))
        self.assertEqual(code, 200, second)
        code, other = cars_parking.save_permit(permit(carId=CAR2, number="АБ-1", reminders=[]))
        self.assertEqual(code, 200, other)
        rows = cars_parking.view()["permits"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len([p for p in rows if p["carId"] == CAR]), 2)
        self.assertEqual(len(first["permit"]["reminders"]), 2)
        self.assertEqual(first["permit"]["reminders"][0]["firesAt"], "2026-11-18 10:00")
        self.assertEqual(cars_parking.purge_car(CAR)["removed"], 2)
        self.assertEqual([p["carId"] for p in cars_parking.view()["permits"]], [CAR2])

    def test_rejects_bad_permit(self):
        for bad in (
            permit(number=""),
            permit(ends="2025-01-01"),
            permit(starts="вчера"),
            permit(reminders=[{"mode": "days", "daysBefore": -1, "time": "10:00"}]),
            permit(reminders=[{"mode": "date", "date": "", "time": "10:00"}]),
            permit(reminders=[{"mode": "date", "date": "2026-11-18", "time": "25:00"}]),
            permit(carId="../x"),
        ):
            code, body = cars_parking.save_permit(bad)
            self.assertEqual(code, 400, body)
            self.assertFalse(body["ok"])

    def test_files_roundtrip_and_delete(self):
        _, saved = cars_parking.save_permit(permit(reminders=[]))
        pid = saved["permit"]["id"]
        code, stored = cars_parking.save_file(pid, "скан.png", "png", PNG)
        self.assertEqual(code, 200, stored)
        opened = cars_parking.open_file(stored["file"]["id"])
        self.assertIsNotNone(opened)
        self.assertEqual(opened[0].read_bytes(), PNG)
        self.assertEqual(cars_parking.save_file(pid, "a.exe", "exe", b"MZ")[0], 400)
        self.assertEqual(cars_parking.save_file(pid, "a.png", "png", b"not a png")[0], 400)
        self.assertIsNone(cars_parking.open_file("../jarvis_cars.json"))
        code, _ = cars_parking.save_file(pid, "бланк.pdf", "pdf", PDF)
        self.assertEqual(code, 200)
        cars_parking.delete_permit(pid)
        self.assertEqual(cars_parking.view()["permits"], [])
        self.assertFalse(any(cars_parking.files_dir().iterdir()))

    def test_reminders_fire_once_inside_grace(self):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=cars_parking.MSK)
        _, saved = cars_parking.save_permit(permit())
        due, missed = cars_parking.partition(now)
        self.assertEqual(len(due), 2)
        self.assertEqual(missed, [])
        early, _ = cars_parking.partition(now - timedelta(minutes=1))
        self.assertEqual(early, [])
        late_due, late_missed = cars_parking.partition(now + timedelta(hours=5))
        self.assertEqual(len(late_due), 2)
        self.assertEqual(late_missed, [])
        way_late_due, way_late_missed = cars_parking.partition(now + timedelta(hours=7))
        self.assertEqual(way_late_due, [])
        self.assertEqual(len(way_late_missed), 2)
        item = due[0]
        text = cars_parking.render(item["permit"], item["reminder"], {"name": "<b>", "plate": "А123ВС77", "make": "Toyota", "model": "Camry"})
        self.assertIn("Парковка — напоминание", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("<b>", text.split("Парковка", 1)[1])
        self.assertIn("А123ВС77", text)
        cars_parking.mark_done(item["permitId"], item["reminderId"], item["slot"])
        again, _ = cars_parking.partition(now)
        self.assertEqual(len(again), 1)
        revoked = dict(permit(revoked=True))
        cars_parking.save_permit({**revoked, "id": saved["permit"]["id"]})
        self.assertEqual(cars_parking.partition(now), ([], []))

    def test_recipients_prefer_owner(self):
        self.assertEqual(cars_parking.recipients(42, [1, 2], [2, 3]), [42, 2, 3])
        self.assertEqual(cars_parking.recipients(None, [7], []), [7])
        self.assertEqual(cars_parking.recipients(None, [7, 8], []), [])
        self.assertEqual(cars_parking.recipients("9", [], []), [9])

    def test_tick_sends_to_owner_once(self):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=cars_parking.MSK)
        _, saved = cars_parking.save_permit(permit(reminders=[{"mode": "days", "daysBefore": 14, "time": "10:00", "text": "Продли"}]))
        sent = []
        old_send, old_user = jarvis.send_message, jarvis.tg_user
        jarvis.send_message = lambda token, cid, text: sent.append((cid, text))
        jarvis.tg_user = type("U", (), {"_load_cfg": staticmethod(lambda: {"user_id": 99})})()
        try:
            jarvis._parking_tick({"cars": [{"id": CAR, "name": "Камри", "plate": "А123ВС77"}]}, {"chat_ids": [1, 2]}, "tok", now)
            jarvis._parking_tick({"cars": [{"id": CAR, "name": "Камри"}]}, {"chat_ids": [1]}, "tok", now)
        finally:
            jarvis.send_message, jarvis.tg_user = old_send, old_user
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 99)
        self.assertIn("Продли", sent[0][1])
        self.assertIn("Камри", sent[0][1])
        self.assertTrue(saved["permit"]["id"])

    def test_delete_car_purges_permits(self):
        _, saved = cars_parking.save_permit(permit(reminders=[]))
        code, stored = cars_parking.save_file(saved["permit"]["id"], "скан.png", "png", PNG)
        self.assertEqual(code, 200)
        app = {"cars": [{"id": CAR, "name": "Камри"}], "serviceRecords": [{"id": "s1", "carId": CAR}]}
        self.assertEqual(jarvis.delete_record_generic(app, "cars", CAR)["ok"], True)
        self.assertEqual(app["cars"], [])
        self.assertEqual(app["serviceRecords"], [])
        self.assertEqual(cars_parking.view()["permits"], [])
        self.assertFalse(cars_parking.files_dir().exists() and any(cars_parking.files_dir().iterdir()))

    def test_backup_gitignore_and_ui_contract(self):
        _, saved = cars_parking.save_permit(permit(reminders=[]))
        cars_parking.save_file(saved["permit"]["id"], "скан.png", "png", PNG)
        ignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("jarvis_cars.json", ignore)
        self.assertIn("jarvis_cars/", ignore)
        html = Path("index (9).html").read_text(encoding="utf-8")
        for needle in ("Марка", "Модель", "Резидентское разрешение", "За сколько дней", "Текст в Telegram",
                       "gar-plate", "gar-slip", "gar-road", "Боксы", "id: 'parking'"):
            self.assertIn(needle, html)
        self.assertIn("parking", jarvis.NOTIFICATION_CATEGORIES)
        old = jarvis.DATA_DIR
        jarvis.DATA_DIR = Path(self.tmp.name)
        try:
            names = zipfile.ZipFile(io.BytesIO(jarvis.build_backup_zip())).namelist()
        finally:
            jarvis.DATA_DIR = old
        self.assertIn("data/jarvis_cars.json", names)
        self.assertTrue(any(n.startswith("data/jarvis_cars/") and n.endswith(".png") for n in names))


class ParkingHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=False, raw=None, content_type="application/json"):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {}
        if data is not None and content_type:
            headers["Content-Type"] = content_type
        if cookie:
            headers["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                blob = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(blob.decode())
                return resp.status, blob
        except urllib.error.HTTPError as e:
            blob = e.read()
            try:
                return e.code, json.loads(blob.decode())
            except Exception:
                return e.code, blob

    def test_auth_and_crud(self):
        code, _ = self.req("GET", "/api/cars/parking")
        self.assertEqual(code, 401)
        code, created = self.req("POST", "/api/cars/parking", permit(reminders=[]), cookie=True)
        self.assertEqual(code, 200, created)
        pid = created["permit"]["id"]
        code, updated = self.req("POST", "/api/cars/parking", {"id": pid, "number": "77-300", "notes": "правка"}, cookie=True)
        self.assertEqual(code, 200, updated)
        self.assertEqual(updated["permit"]["number"], "77-300")
        self.assertEqual(updated["permit"]["zone"], "ЦАО")
        code, uploaded = self.req("POST", f"/api/cars/parking/file?permit={pid}&name=scan.png&ext=png", cookie=True, raw=PNG, content_type="application/octet-stream")
        self.assertEqual(code, 200, uploaded)
        fid = uploaded["file"]["id"]
        code, blob = self.req("GET", f"/api/cars/parking/file/{fid}", cookie=True)
        self.assertEqual(code, 200)
        self.assertEqual(blob, PNG)
        code, denied = self.req("GET", f"/api/cars/parking/file/{fid}")
        self.assertEqual(code, 401)
        code, removed = self.req("DELETE", f"/api/cars/parking/file/{fid}", cookie=True)
        self.assertEqual(code, 200, removed)
        code, gone = self.req("POST", "/api/cars/parking/delete", {"id": pid}, cookie=True)
        self.assertEqual(code, 200, gone)
        code, view = self.req("GET", "/api/cars/parking", cookie=True)
        self.assertEqual(view["permits"], [])


CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable")


@unittest.skipUnless(CHROME, "нужен Chrome")
class ParkingBrowserTest(unittest.TestCase):
    def test_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright

        tmp = tempfile.TemporaryDirectory()
        old = os.environ.get("DATA_DIR")
        old_paths = (jarvis.DATA_DIR, jarvis.APP_DATA_FILE, jarvis.CONFIG_FILE, jarvis.SUBSCRIBERS_FILE)
        os.environ["DATA_DIR"] = tmp.name
        jarvis.DATA_DIR = Path(tmp.name)
        jarvis.APP_DATA_FILE = jarvis.DATA_DIR / "jarvis_app_data.json"
        jarvis.CONFIG_FILE = jarvis.DATA_DIR / "jarvis_notify_config.json"
        jarvis.SUBSCRIBERS_FILE = jarvis.DATA_DIR / "jarvis_subscribers.json"
        srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        out = Path("/cursor/stores/self/media/cars")
        out.mkdir(parents=True, exist_ok=True)
        cdn = {
            "https://cdn.tailwindcss.com": "/tmp/jarvis-cdn/tailwind.js",
            "https://unpkg.com/react@18/umd/react.production.min.js": "/tmp/jarvis-cdn/react.js",
            "https://unpkg.com/react-dom@18/umd/react-dom.production.min.js": "/tmp/jarvis-cdn/react-dom.js",
            "https://unpkg.com/@babel/standalone@7.24.7/babel.min.js": "/tmp/jarvis-cdn/babel.js",
        }
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda e: errors.append(str(e)))

                def route(route):
                    url = route.request.url.split("?", 1)[0].rstrip("/")
                    path = cdn.get(url)
                    if path:
                        route.fulfill(path=path, content_type="application/javascript; charset=utf-8")
                    else:
                        route.continue_()

                page.route("**/*", route)
                page.context.add_cookies([{
                    "name": "jarvis_auth", "value": jarvis.AUTH_COOKIE_VALUE,
                    "url": f"http://127.0.0.1:{port}/",
                }])
                page.on("dialog", lambda d: d.accept())
                page.goto(f"http://127.0.0.1:{port}/cars", wait_until="domcontentloaded", timeout=60000)
                page.get_by_test_id("car-add").wait_for(timeout=180000)
                page.get_by_test_id("car-add").click()
                page.get_by_test_id("car-name").fill("Камри")
                page.get_by_test_id("car-make").fill("Toyota")
                page.get_by_test_id("car-model").fill("Camry")
                page.get_by_test_id("car-plate").fill("А123ВС77")
                page.get_by_test_id("car-color").fill("белый")
                page.screenshot(path=str(out / "desktop-car-form.png"), full_page=True)
                page.get_by_test_id("car-save").click()
                page.get_by_test_id("car-row").wait_for()
                page.get_by_test_id("car-permit").click()
                page.get_by_test_id("permit-number").fill("77-100")
                page.get_by_test_id("permit-starts").fill("2026-01-01")
                page.get_by_test_id("permit-ends").fill("2026-12-31")
                page.get_by_role("button", name="Ещё напоминание").click()
                shot = Path(tmp.name) / "scan.png"
                shot.write_bytes(PNG)
                page.get_by_test_id("permit-file").set_input_files(str(shot))
                page.get_by_text("scan.png").wait_for()
                page.screenshot(path=str(out / "desktop-permit-form.png"), full_page=True)
                page.get_by_test_id("permit-save").click()
                page.get_by_test_id("cars-tab-permits").click()
                page.get_by_test_id("permit-card").wait_for(timeout=15000)
                page.locator(".gar-scans img").wait_for()
                page.screenshot(path=str(out / "desktop-permits.png"), full_page=True)
                page.get_by_test_id("cars-tab-settings").click()
                page.get_by_test_id("car-row").wait_for()
                page.screenshot(path=str(out / "desktop-bays.png"), full_page=True)
                page.get_by_test_id("cars-tab-service").click()
                page.get_by_test_id("service-add").click()
                page.get_by_placeholder("Работа 1...").fill("Масло")
                page.locator("input[placeholder='₽']").fill("4500")
                page.get_by_role("button", name="Сохранить").click()
                page.get_by_test_id("service-row").wait_for()
                page.screenshot(path=str(out / "desktop-journal.png"), full_page=True)

                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_test_id("cars-tab-settings").click()
                page.get_by_test_id("car-row").wait_for()
                page.screenshot(path=str(out / "mobile-bays.png"), full_page=True)
                page.get_by_test_id("cars-tab-permits").click()
                page.screenshot(path=str(out / "mobile-permits.png"), full_page=True)
                page.get_by_test_id("cars-tab-service").click()
                page.get_by_test_id("service-row").wait_for()
                page.screenshot(path=str(out / "mobile-journal.png"), full_page=True)
                page.get_by_test_id("cars-tab-permits").click()
                page.get_by_test_id("permit-card").wait_for()
                self.assertIn("77-100", page.locator("body").inner_text())
                browser.close()
        finally:
            srv.shutdown()
            thread.join(timeout=3)
            srv.server_close()
            if old is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = old
            jarvis.DATA_DIR, jarvis.APP_DATA_FILE, jarvis.CONFIG_FILE, jarvis.SUBSCRIBERS_FILE = old_paths
            tmp.cleanup()
        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
