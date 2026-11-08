"""Ozon on the existing Windows bridge; no real accounts or Telegram sends."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import jarvis
import market_pc as pc
import market_skill
import wb_watch_skill as watch

POSITION = {"id": "ozon:12345678-9001:parcel-1:123456789", "article": "123456789",
            "name": "Тетрадь", "price": "755 ₽", "status": "Можно забирать", "pvz": "Пункт Ozon: Тестовая, 1",
            "image_url": "https://ir.ozone.ru/s3/test.jpg?secret=remove"}


class OzonPcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.token = pc.pair()["token"]
        self.addCleanup(pc.disconnect)
        self.poll()

    def poll(self, capabilities=None):
        return pc.device_call('/api/market/pc/poll', self.token, {"capabilities": capabilities or ["wb", "ozon"]})[1]

    def raw(self, **updates):
        return {"ok": True, "cabinet": True, "url": "https://www.ozon.ru/my/orderlist", "rows": [POSITION], **updates}

    def read(self, raw, details=False, section="delivery"):
        result = []
        thread = threading.Thread(target=lambda: result.append(pc.request(section, shop="ozon", timeout=2, include_details=details)))
        thread.start()
        try:
            job = None
            for _ in range(200):
                job = self.poll().get("job")
                if job:
                    break
                time.sleep(.005)
            self.assertIsNotNone(job)
            self.assertEqual(job["shop"], "ozon")
            self.assertEqual(job["section"], section)
            self.assertEqual(pc.device_call('/api/market/pc/result', self.token, {"id": job["id"], "result": raw})[0], 200)
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive())
        return result[0]

    def test_queue_round_trip_and_separate_shop_sessions(self):
        result = self.read(self.raw())
        self.assertTrue(result["ok"])
        self.assertEqual(result["shop"], "ozon")
        self.assertEqual(result["rows"][0]["price"], "755 ₽")
        self.assertNotIn("pvz", result["rows"][0])
        self.assertEqual(result["rows"][0]["product_url"], "https://www.ozon.ru/product/123456789/")
        self.assertNotIn("secret", result["rows"][0]["image_url"])
        status = pc.public_status()
        self.assertEqual(status["shops"]["ozon"]["session"], "active")
        self.assertEqual(status["session"], "unknown", "Ozon must not change the WB session")
        self.assertTrue(status["shops"]["wb"]["supported"])
        failed = self.read({"ok": False, "code": "login", "error": "secret"})
        self.assertEqual(failed["session"], "expired")
        self.assertNotIn("WB", failed["error"])
        self.assertNotIn("secret", json.dumps(failed))

    def test_old_extension_reports_upgrade_without_claiming_ozon(self):
        pc.device_call('/api/market/pc/poll', self.token, {})
        self.assertEqual(pc.request("delivery", shop="ozon")["code"], "unsupported")
        self.assertFalse(pc._jobs)
        self.assertTrue(pc.public_status()["online"])

    def test_origin_section_archive_and_explicit_empty_are_validated(self):
        self.assertFalse(self.read(self.raw(url="https://www.wildberries.ru/lk/myorders/delivery"))["ok"])
        for url in ("https://www.ozon.ru.evil.test/my/orderlist", "https://user:pass@www.ozon.ru/my/orderlist", "https://www.ozon.ru/my/orderdetails/"):
            self.assertFalse(pc._clean_result("delivery", self.raw(url=url), shop="ozon")["ok"])
        archive = self.raw(url="https://www.ozon.ru/my/orderlist?selectedTab=archive")
        self.assertTrue(pc._clean_result("orders", archive, shop="ozon")["ok"])
        self.assertFalse(pc._clean_result("delivery", archive, shop="ozon")["ok"])
        self.assertFalse(pc._clean_result("orders", self.raw(), shop="ozon")["ok"])
        self.assertFalse(pc._clean_result("delivery", self.raw(rows=[]), shop="ozon")["ok"])
        self.assertTrue(pc._clean_result("delivery", self.raw(rows=[], empty=True), shop="ozon")["ok"])

    def test_details_and_images_are_allowlisted(self):
        row = {**POSITION, "extra_fields": [{"path": "office.hours", "value": "09:00–21:00"},
                                             {"path": "item.token", "value": "secret"}], "image_url": "https://evil.test/track.jpg"}
        result = self.read(self.raw(rows=[row]), details=True)
        clean = result["rows"][0]
        self.assertEqual(clean["pvz"], POSITION["pvz"])
        self.assertEqual(clean["extra_fields"], [{"path": "office.hours", "value": "09:00–21:00"}])
        self.assertNotIn("image_url", clean)
        self.assertNotIn("secret", json.dumps(result))

    def test_assistant_uses_pc_for_both_shops_without_proxy(self):
        with patch.object(pc, "request", return_value={"ok": True, "source": "pc"}) as read, \
                patch.object(market_skill, "get_driver", side_effect=AssertionError("server browser used")):
            market_skill.execute("market_delivery", {"shop": "ozon"})
            read.assert_called_once_with("delivery", include_details=False, shop="ozon")
            read.reset_mock()
            market_skill.execute("market_delivery", {"shop": "ozon", "details": True})
            read.assert_called_once_with("delivery", include_details=True, shop="ozon")
            self.assertFalse(market_skill.execute("market_cancel_order", {"shop": "ozon", "order_id": "12345678-9001", "confirmed": True})["ok"])
            read.reset_mock()
            self.assertIn("error", market_skill.execute("market_orders", {"shop": "ozon"}, {"marketplaces": False}))
            read.assert_not_called()

    def test_notification_schedule_dedupe_and_isolation_from_wb(self):
        app, notify = {"telegramSkills": {}}, Mock(return_value=True)
        skill = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == "ozon_pickup")
        self.assertTrue(skill["default"])
        def check(day, hour=10, rows=None):
            now = datetime(2026, 11, day, hour, tzinfo=watch.MSK).timestamp()
            watch.run_once(lambda: app, lambda: {"ok": True, "rows": rows or [POSITION]}, notify, lambda: now, shop="ozon")
        check(8, hour=9)
        notify.assert_not_called()
        check(8)
        self.assertEqual(notify.call_count, 1)
        self.assertIn("Ozon", notify.call_args.args[0])
        self.assertIn("755 ₽", notify.call_args.args[0])
        check(9)
        check(10)
        self.assertEqual(notify.call_count, 1)
        check(11)
        self.assertEqual(notify.call_count, 2)
        self.assertTrue(watch.store_path("ozon").exists())
        self.assertFalse(watch.store_path().exists())
        check(14, rows=[{**POSITION, "status": "Получен"}])
        self.assertEqual(notify.call_count, 2)

    def test_notifications_require_verified_parcel_identity_and_toggle(self):
        now = datetime(2026, 11, 8, 10, tzinfo=watch.MSK).timestamp()
        notify = Mock(return_value=True)
        watch.run_once(lambda: {}, lambda: {"ok": True, "rows": [{**POSITION, "id": "ozon-list:12345678-9001:0:0"}]}, notify,
                       lambda: now, shop="ozon")
        notify.assert_not_called()
        read = Mock()
        watch.run_once(lambda: {"telegramSkills": {"ozon_pickup": False}}, read, notify, lambda: now + 86400, shop="ozon")
        read.assert_not_called()
        self.assertTrue(watch.skill_on({"ozon_pickup": False}, "wb"))


if __name__ == "__main__":
    unittest.main()
