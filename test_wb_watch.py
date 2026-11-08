"""WB pickup scheduling and real bridge → mocked Telegram, no live services."""
import json
import os
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import jarvis
import market_pc
import wb_watch_skill as watch


def at(day=8, hour=10, minute=0, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=watch.MSK).timestamp()


READY = {"id": "eb6.order-1.0.0", "name": "Книга", "price": "755 ₽", "status": "Приходите за товаром"}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = {"telegramSkills": {}}
        self.now = at()
        self.read = Mock(return_value={"ok": True, "rows": [READY]})
        self.notify = Mock(return_value=True)

    def check(self, when, rows=None, result=None):
        self.now = when
        self.read.return_value = result if result is not None else {"ok": True, "rows": [READY] if rows is None else rows}
        watch.run_once(lambda: self.app, self.read, self.notify, lambda: self.now)

    def state(self):
        return json.loads(watch.store_path().read_text())

    def test_catalog_default_and_both_toggles(self):
        skill = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == watch.SKILL_ID)
        self.assertTrue(skill["default"])
        self.assertTrue(watch.skill_on({}))
        for sid in (watch.SKILL_ID, "marketplaces"):
            self.app["telegramSkills"] = {sid: False}
            self.check(at())
        self.read.assert_not_called()
        self.notify.assert_not_called()
        self.assertFalse(watch.store_path().exists())

    def test_status_detection_only_positive_readiness(self):
        for status in ("Приходите за товаром", "Готов к выдаче", "Можно забирать", "Заказ готов к получению!", "Доставлен в пункт выдачи"):
            self.assertTrue(watch.is_ready(status), status)
        for status in (None, "", "Получен", "Выдан", "Отменён", "Едет в сортировочный центр", "Не готов к выдаче", "Возврат готов к выдаче", "Доставлен", "Передан курьеру"):
            self.assertFalse(watch.is_ready(status), status)

    def test_hourly_and_moscow_morning(self):
        self.check(at(hour=9, minute=59))
        self.notify.assert_not_called()
        self.check(at(hour=9, minute=59, second=45))
        self.assertEqual(self.read.call_count, 1)
        self.check(at(second=30))  # UTC 07:00:30 == Moscow 10:00:30
        self.notify.assert_called_once()
        text = self.notify.call_args.args[0]
        self.assertIn("Книга — 755 ₽", text)
        self.assertNotIn(READY["id"], text)
        self.check(at(hour=10, minute=1))
        self.assertEqual(self.read.call_count, 2)
        self.check(at(hour=11))
        self.assertEqual(self.read.call_count, 3)
        self.assertEqual(self.notify.call_count, 1)

    def test_after_morning_waits_and_nothing_for_in_transit(self):
        self.check(at(hour=10, minute=11), [{**READY, "status": "В пути"}])
        self.check(at(hour=11))
        self.notify.assert_not_called()
        self.check(at(day=9))
        self.notify.assert_called_once()

    def test_repeat_third_morning_persistent_history(self):
        self.check(at(second=40))
        self.assertEqual(self.notify.call_count, 1)
        # A new invocation reloads the on-disk deduplication history each time.
        self.check(at(day=9))
        self.check(at(day=10))
        self.assertEqual(self.notify.call_count, 1)
        self.check(at(day=11, second=5))
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn("всё ещё ждёт выдачи", self.notify.call_args.args[0])
        self.check(at(day=12))
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(watch.store_path().stat().st_mode & 0o777, 0o600)

    def test_collected_cancels_repeat(self):
        self.check(at())
        self.check(at(day=9), [{**READY, "status": "Получен"}])
        self.check(at(day=11), [])
        self.assertEqual(self.notify.call_count, 1)

    def test_error_missing_or_truncated_never_sends_stale_ready(self):
        self.check(at())
        before = self.state()["orders"][READY["id"]].copy()
        self.check(at(day=11), result={"ok": False, "code": "offline"})
        self.assertEqual(self.state()["orders"][READY["id"]], before)
        self.check(at(day=11, minute=5), result={"ok": True, "rows": [], "truncated": True})
        self.assertEqual(self.notify.call_count, 1)
        self.check(at(day=12))
        self.assertEqual(self.notify.call_count, 2)

    def test_no_duplicate_after_temporary_missing_row(self):
        self.check(at())
        self.check(at(hour=11), [])
        self.check(at(day=9))
        self.assertEqual(self.notify.call_count, 1)

    def test_changed_readiness_waits_three_days_again(self):
        self.check(at())
        self.check(at(day=9), [{**READY, "status": "Возврат в пути"}])
        self.check(at(day=10))
        self.check(at(day=11))
        self.assertEqual(self.notify.call_count, 1)
        self.check(at(day=13))
        self.assertEqual(self.notify.call_count, 2)

    def test_batch_dedupe_by_position_not_article_and_partial_send_retry(self):
        other = {**READY, "id": "eb6.order-1.1.0", "price": "489 ₽"}
        self.notify.side_effect = [True, RuntimeError("mock Telegram failure"), True]
        self.check(at(), [READY, other])
        history = self.state()["orders"]
        self.assertTrue(history[READY["id"]]["last_notified"])
        self.assertFalse(history[other["id"]]["last_notified"])
        self.check(at(minute=1), [READY, other])
        self.assertEqual(self.notify.call_count, 3)
        self.assertIn("489 ₽", self.notify.call_args.args[0])
        self.assertTrue(self.state()["orders"][other["id"]]["last_notified"])

    def test_busy_retries_without_notification_or_state_change(self):
        self.check(at(), result={"ok": False, "code": "busy"})
        self.assertEqual(self.state()["next_check"], at(minute=1))
        self.notify.assert_not_called()
        self.check(at(minute=1))
        self.notify.assert_called_once()

    def test_unexpected_read_failure_retries_without_losing_history(self):
        self.check(at())
        self.read.side_effect = RuntimeError("mock read failure")
        self.check(at(day=11))
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.state()["next_check"], at(day=11, minute=5))
        self.read.side_effect = None
        self.check(at(day=11, minute=5))
        self.assertEqual(self.notify.call_count, 2)

    def test_toggle_during_read_prevents_notice(self):
        def read():
            self.app["telegramSkills"][watch.SKILL_ID] = False
            return {"ok": True, "rows": [READY]}
        watch.run_once(lambda: self.app, read, self.notify, lambda: self.now)
        self.notify.assert_not_called()

    def test_corrupt_history_fails_closed(self):
        watch.store_path().write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.check(at())
        self.notify.assert_not_called()
        self.assertEqual(watch.store_path().read_text(), "{broken")

    def test_html_escaped(self):
        self.check(at(), [{**READY, "name": "<b>Книга & журнал</b>"}])
        self.assertIn("&lt;b&gt;Книга &amp; журнал&lt;/b&gt;", self.notify.call_args.args[0])

    def test_nonblocking_tick_and_no_overlapping_reads(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def read():
            entered.set()
            release.wait(2)
            return {"ok": True, "rows": [READY]}
        original = watch._run
        def wrapped(*args):
            try:
                return original(*args)
            finally:
                finished.set()
        with patch.object(watch, "_run", side_effect=wrapped), patch.object(watch.time, "time", return_value=at()):
            watch.tick(lambda: self.app, read, self.notify)
            self.assertTrue(entered.wait(1))
            competing = Mock()
            watch.run_once(lambda: self.app, competing, self.notify, lambda: self.now)
            competing.assert_not_called()
            release.set()
            self.assertTrue(finished.wait(2))
        # The worker releases the lock immediately after returning from _run.
        with watch._lock:
            pass

    def test_real_pc_queue_to_mocked_telegram_owner_only(self):
        token = market_pc.pair()["token"]
        self.addCleanup(market_pc.disconnect)
        def poll():
            return market_pc.device_call("/api/market/pc/poll", token, {})[1]
        poll()
        sends = []
        def fake_api(bot_token, method, payload):
            self.assertEqual(method, "sendMessage")
            sends.append(payload)
            return {"message_id": len(sends)}
        with patch.object(jarvis, "get_token", return_value="fake-token"), \
                patch.object(jarvis, "tg_user", types.SimpleNamespace(_load_cfg=lambda: {"user_id": 123})), \
                patch.object(jarvis.tg_bot, "api", side_effect=fake_api):
            runner = threading.Thread(target=lambda: watch.run_once(
                lambda: self.app, lambda: market_pc.request("delivery", timeout=2), jarvis._wb_pickup_notify, lambda: self.now))
            runner.start()
            try:
                job = None
                for _ in range(200):
                    job = poll().get("job")
                    if job:
                        break
                    time.sleep(.005)
                self.assertIsNotNone(job)
                self.assertEqual(job["section"], "delivery")
                self.assertFalse(job["details"])
                status, _ = market_pc.device_call("/api/market/pc/result", token, {"id": job["id"], "result": {
                    "ok": True, "cabinet": True, "url": "https://www.wildberries.ru/lk/myorders/delivery", "rows": [READY]}})
                self.assertEqual(status, 200)
            finally:
                runner.join(3)
            self.assertFalse(runner.is_alive())
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0]["chat_id"], 123)
            self.assertIn("Книга — 755 ₽", sends[0]["text"])
            self.check(at(day=9))
            self.notify.assert_not_called()  # persisted ACK from the real bridge round trip

    def test_no_owner_does_not_send_to_other_subscribers(self):
        with patch.object(jarvis, "get_token", return_value="fake-token"), \
                patch.object(jarvis, "tg_user", types.SimpleNamespace(_load_cfg=lambda: {})), \
                patch.object(jarvis.tg_bot, "send_text") as send:
            self.assertFalse(jarvis._wb_pickup_notify("test"))
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
