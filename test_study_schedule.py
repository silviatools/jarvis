"""Два запуска расписания РЭУ → календарь. Сеть и календарь не вызываются."""

import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import jarvis  # noqa: E402


def app(**schedule):
    base = {"enabled": True, "group": "15.14д-мц01/25м", "days": [1, 2, 3, 4, 5, 6, 7], "time": "08:00"}
    base.update(schedule)
    return {"studySchedule": base}


class StudyScheduleTwiceTest(unittest.TestCase):
    def setUp(self):
        if jarvis.STUDY_STATE_FILE.exists():
            jarvis.STUDY_STATE_FILE.unlink()
        if jarvis._STUDY_LOCK.locked():
            jarvis._STUDY_LOCK.release()
        with jarvis._study_inflight_lock:
            jarvis._study_inflight.clear()
        self.day = jarvis.today_msk().isoformat()

    def tearDown(self):
        if jarvis._STUDY_LOCK.locked():
            jarvis._STUDY_LOCK.release()
        with jarvis._study_inflight_lock:
            jarvis._study_inflight.clear()

    def test_defaults_and_old_single_time(self):
        fresh = jarvis.study_settings({})
        self.assertEqual(fresh["time"], "08:00")
        self.assertEqual(fresh["time2"], "16:00")
        self.assertEqual(fresh["days"], [1, 2, 3, 4, 5, 6, 7])

        old = jarvis.study_settings({"studySchedule": {"time": "09:30", "days": [1, 3, 5], "group": "г"}})
        self.assertEqual(old["time"], "09:30")
        self.assertEqual(old["time2"], "16:00")
        self.assertEqual(old["days"], [1, 3, 5])

        blank = jarvis.study_settings({"studySchedule": {"time": "7:05", "time2": ""}})
        self.assertEqual(blank["time"], "07:05")
        self.assertEqual(blank["time2"], "16:00")

        custom = jarvis.study_settings({"studySchedule": {"time": "09:30", "time2": "18:40"}})
        self.assertEqual(custom["time"], "09:30")
        self.assertEqual(custom["time2"], "18:40")
        self.assertEqual(jarvis.study_slot_times(custom), ["09:30", "18:40"])

        same = jarvis.study_settings({"studySchedule": {"time": "10:00", "time2": "10:00"}})
        self.assertEqual(jarvis.study_slot_times(same), ["10:00"])

    def test_missed_minute_catches_up_each_slot_once(self):
        data = app()
        morning = f"{self.day} 08:00"
        evening = f"{self.day} 16:00"
        self.assertEqual(jarvis.study_due_slot(data, "07:59", self.day), "")
        self.assertEqual(jarvis.study_due_slot(data, "08:00", self.day), morning)
        self.assertEqual(jarvis.study_due_slot(data, "08:07", self.day), morning)
        jarvis.study_claim_slot(morning)
        self.assertEqual(jarvis.study_due_slot(data, "12:00", self.day), "")
        self.assertEqual(jarvis.study_due_slot(data, "16:00", self.day), evening)
        self.assertEqual(jarvis.study_due_slot(data, "16:11", self.day), evening)
        # Оба пропущены: сначала ранний, следующим тиком — поздний.
        jarvis.study_claim_slot(morning)
        self.assertEqual(jarvis.study_due_slot(data, "17:40", self.day), evening)
        jarvis.study_claim_slot(evening)
        self.assertEqual(jarvis.study_due_slot(data, "23:00", self.day), "")

    def test_saved_first_time_and_swapped_slots(self):
        data = app(time="09:30")
        self.assertEqual(jarvis.study_due_slot(data, "09:00", self.day), "")
        self.assertEqual(jarvis.study_due_slot(data, "09:31", self.day), f"{self.day} 09:30")
        swapped = app(time="16:00", time2="08:00")
        self.assertEqual(jarvis.study_due_slot(swapped, "09:00", self.day), f"{self.day} 08:00")
        off = app(enabled=False)
        self.assertEqual(jarvis.study_due_slot(off, "18:00", self.day), "")
        empty = app(group="")
        self.assertEqual(jarvis.study_due_slot(empty, "18:00", self.day), "")
        other = 1 if jarvis.today_msk().isoweekday() != 1 else 2
        self.assertEqual(jarvis.study_due_slot(app(days=[other]), "18:00", self.day), "")

    def test_tick_runs_both_slots_and_skips_a_busy_sync(self):
        started = []

        class SyncThread:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        def sync(preview=False, reason="manual"):
            started.append(reason)
            return {"ok": True}

        data = app()
        with patch.object(jarvis.threading, "Thread", SyncThread), patch.object(jarvis, "study_sync", sync):
            jarvis.study_tick(data, "08:05", self.day)
            jarvis.study_tick(data, "08:06", self.day)
            jarvis.study_tick(data, "16:02", self.day)
            jarvis.study_tick(data, "16:03", self.day)
        self.assertEqual(started, ["schedule", "schedule"])
        state = jarvis.study_load_state()
        self.assertEqual(state["doneSlots"], [f"{self.day} 08:00", f"{self.day} 16:00"])

        started.clear()
        if jarvis.STUDY_STATE_FILE.exists():
            jarvis.STUDY_STATE_FILE.unlink()

        def busy(preview=False, reason="manual"):
            started.append("busy")
            return {"ok": False, "error": "Синхронизация уже идёт — подождите минуту."}

        with patch.object(jarvis.threading, "Thread", SyncThread), patch.object(jarvis, "study_sync", busy):
            jarvis.study_tick(data, "08:05", self.day)
            jarvis.study_tick(data, "08:06", self.day)
        self.assertEqual(started, ["busy", "busy"])
        self.assertNotIn("doneSlots", jarvis.study_load_state())

    def test_claim_survives_a_stale_state_save(self):
        slot = f"{self.day} 08:00"
        stale = jarvis.study_load_state()
        jarvis.study_claim_slot(slot)
        stale["lastRunAt"] = 1
        jarvis.study_save_state(stale)
        self.assertIn(slot, jarvis.study_load_state()["doneSlots"])
        self.assertEqual(jarvis.study_load_state()["lastRunAt"], 1)

    def test_tick_waits_while_sync_lock_is_held(self):
        jarvis._STUDY_LOCK.acquire()
        try:
            self.assertEqual(jarvis.study_due_slot(app(), "18:00", self.day), "")
        finally:
            jarvis._STUDY_LOCK.release()


if __name__ == "__main__":
    unittest.main()
