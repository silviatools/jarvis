"""Контроль учебных чатов: расписание, тишина и доставка. Telegram и MAX подменены."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import jarvis  # noqa: E402
import study_skills  # noqa: E402
import tg_tools  # noqa: E402


def app(**extra):
    base = {
        "telegramSkills": {},
        "studyChats": {"enabled": True, "time": "20:00", "folder": "", "chats": [{"id": 5, "title": "Семинар"}],
                       "notifyEmpty": False, "maxFolder": ""},
        "studyTasks": [],
        "knowledgeNotes": [],
        "settings": {"notifyRouting": {"42": ["study"]}},
    }
    base.update(extra)
    return base


class StudyChatsTest(unittest.TestCase):
    def setUp(self):
        if jarvis._study_scan_lock.locked():
            jarvis._study_scan_lock.release()
        if study_skills.STATE_FILE.exists():
            study_skills.STATE_FILE.unlink()
        self.sent = []
        self.scans = []
        self.llm_n = 0
        self.old = (jarvis.tg_user, jarvis.tg_tools, jarvis.tg_bot)

    def tearDown(self):
        jarvis.tg_user, jarvis.tg_tools, jarvis.tg_bot = self.old
        if jarvis._study_scan_lock.locked():
            jarvis._study_scan_lock.release()

    def _install(self, scan_chat, llm=None):
        jarvis.tg_user = SimpleNamespace(
            is_owner_chat=lambda cid: int(cid) == 42,
            _load_cfg=lambda: {"session": "ok", "user_id": 42},
        )
        jarvis.tg_tools = SimpleNamespace(scan_chat=scan_chat, list_dialogs=lambda **k: {"dialogs": []})
        jarvis.tg_bot = SimpleNamespace(
            send_text=lambda token, cid, text: self.sent.append((cid, text)) or [1],
            remember_anchor=lambda *a, **k: None,
            seed_dialog=lambda *a, **k: None,
        )

        def llm_text(*args, **kwargs):
            self.llm_n += 1
            if llm:
                return llm(*args, **kwargs)
            return json.dumps({"items": []}, ensure_ascii=False)

        return llm_text

    def _run(self, data, scan_chat, manual=False, slot="", chat_id=None, llm=None, subs=None):
        llm_text = self._install(scan_chat, llm)
        subscribers = subs if subs is not None else {"chat_ids": [42], "offset": 0}
        with patch.object(jarvis, "load_app_data", return_value=data), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=llm_text), \
                patch.object(jarvis, "load_subscribers", return_value=subscribers):
            return jarvis.study_chats_scan(chat_id, manual=manual, slot=slot)

    def test_parse_triage_ignores_text_after_json(self):
        raw = '```json\n{"items":[{"text":"срок сдачи","chat":"Семинар"}]}\n```\nещё } скобка'
        items, ok = study_skills.parse_triage(raw)
        self.assertTrue(ok)
        self.assertEqual(items[0]["summary"], "срок сдачи")
        items, ok = study_skills.parse_triage('{"items":[]}')
        self.assertTrue(ok)
        self.assertEqual(items, [])
        items, ok = study_skills.parse_triage("не json")
        self.assertFalse(ok)
        self.assertEqual(study_skills.parse_items("не json"), [])

    def test_schedule_catches_a_missed_minute_and_stops_after_success(self):
        data = app()
        self.assertEqual(jarvis.study_chats_due(data, "19:59", "2026-11-13", now_ts=1), "")
        self.assertEqual(jarvis.study_chats_due(data, "20:00", "2026-11-13", now_ts=1), "2026-11-13 20:00")
        self.assertEqual(jarvis.study_chats_due(data, "21:10", "2026-11-13", now_ts=1), "2026-11-13 20:00")
        off = app(studyChats={"enabled": False, "time": "20:00"})
        self.assertEqual(jarvis.study_chats_due(off, "21:10", "2026-11-13", now_ts=1), "")
        disabled = app(telegramSkills={"study_chats": False})
        self.assertEqual(jarvis.study_chats_due(disabled, "21:10", "2026-11-13", now_ts=1), "")
        study_skills.state_update(lastDoneSlot="2026-11-13 20:00")
        self.assertEqual(jarvis.study_chats_due(data, "22:00", "2026-11-13", now_ts=1), "")
        study_skills.state_update(lastDoneSlot="", nextRetryAt=time.time() + 600, slotAttempts={"2026-11-13 20:00": 1})
        self.assertEqual(jarvis.study_chats_due(data, "22:00", "2026-11-13", now_ts=time.time()), "")
        self.assertEqual(jarvis.study_chats_due(data, "22:00", "2026-11-13", now_ts=time.time() + 1000), "2026-11-13 20:00")
        study_skills.state_update(nextRetryAt=0, slotAttempts={"2026-11-13 20:00": 4})
        self.assertEqual(jarvis.study_chats_due(data, "22:00", "2026-11-13", now_ts=time.time() + 1000), "")

    def test_tick_starts_scan_after_the_minute(self):
        started = []

        class SyncThread:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        def scan(chat_id=None, manual=False, slot=""):
            started.append(slot)
            return {"ok": True}

        data = app()
        with patch.object(jarvis.threading, "Thread", SyncThread), patch.object(jarvis, "study_chats_scan", scan):
            jarvis.study_chats_tick(data, "20:05", "2026-11-13")
        self.assertEqual(started, ["2026-11-13 20:00"])

    def test_quiet_scheduled_run_does_not_message_and_closes_the_day(self):
        def scan_chat(peer, since):
            self.scans.append(since)
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 9, "date": "2026-11-13 12:00", "from": "Анна", "text": "ок"}]}

        result = self._run(app(), scan_chat, slot="2026-11-13 20:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.sent, [])
        state = study_skills.state_load()
        self.assertEqual(state["lastDoneSlot"], "2026-11-13 20:00")
        self.assertTrue(state["lastResult"]["quiet"])
        self.assertIn("telegram:5", state["chatCursors"])
        self.assertFalse(state.get("chatPending"))

    def test_manual_empty_still_replies(self):
        def scan_chat(peer, since):
            return {"chat": "Семинар", "chat_id": peer, "messages": [], "me": {"name": "Миша"}}

        result = self._run(app(), scan_chat, manual=True, chat_id=42)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ничего", self.sent[0][1])

    def test_mention_is_sent_even_when_the_model_returns_nothing(self):
        def scan_chat(peer, since):
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 9, "date": "2026-11-13 12:00", "from": "Анна", "text": "Миша, где файл?",
                                  "mentioned": True}]}

        result = self._run(app(), scan_chat, slot="2026-11-13 20:00",
                           llm=lambda *a, **k: json.dumps({"items": []}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sent"], 1)
        self.assertIn("где файл", self.sent[0][1])
        self.assertNotIn("не удалось", self.sent[0][1])
        state = study_skills.state_load()
        self.assertEqual(state["lastDoneSlot"], "2026-11-13 20:00")
        self.assertIn("telegram:5", state["chatCursors"])

    def test_broken_model_output_does_not_skip_the_window(self):
        def scan_chat(peer, since):
            self.scans.append(since)
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 9, "date": "2026-11-13 12:00", "from": "Анна", "text": "перенесли пару"}]}

        first = self._run(app(), scan_chat, slot="2026-11-13 20:00", llm=lambda *a, **k: "не смог")
        self.assertFalse(first["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("не удалось", self.sent[0][1])
        self.assertIn("Повторю позже", self.sent[0][1])
        state = study_skills.state_load()
        self.assertNotIn("telegram:5", state.get("chatCursors") or {})
        self.assertIn("telegram:5", state["chatPending"])
        self.assertNotEqual(state.get("lastDoneSlot"), "2026-11-13 20:00")
        self.assertGreater(state["nextRetryAt"], time.time())
        pending = state["chatPending"]["telegram:5"]
        self.sent.clear()
        second = self._run(app(), scan_chat, slot="2026-11-13 20:00", llm=lambda *a, **k: "снова нет")
        self.assertFalse(second["ok"])
        self.assertAlmostEqual(self.scans[-1], pending, delta=1)
        self.assertEqual(len(self.sent), 0)

    def test_mention_is_delivered_when_triage_json_is_unusable(self):
        def scan_chat(peer, since):
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 4, "from": "Лена", "text": "Миша, ответь", "mentioned": True,
                                  "date": "2026-11-13 18:00"}]}

        result = self._run(app(), scan_chat, slot="2026-11-13 20:00", llm=lambda *a, **k: "мусор }")
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ответь", self.sent[0][1])
        self.assertNotIn("не удалось", self.sent[0][1])
        state = study_skills.state_load()
        self.assertNotIn("telegram:5", state.get("chatCursors") or {})
        self.assertIn("telegram:5", state["chatPending"])
        self.assertTrue(any(str(x).endswith(":4") for x in state["notified"]))

    def test_failed_send_keeps_the_messages(self):
        def scan_chat(peer, since):
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 9, "from": "Анна", "text": "задание", "date": "12:00"}]}

        def boom(token, cid, text):
            raise RuntimeError("telegram down")

        self._install(scan_chat, llm=lambda *a, **k: json.dumps({
            "items": [{"chat": "Семинар", "message_id": 9, "summary": "задание", "why": "срок"}]}))
        jarvis.tg_bot.send_text = boom
        with patch.object(jarvis, "load_app_data", return_value=app()), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=lambda *a, **k: json.dumps({
                    "items": [{"chat": "Семинар", "message_id": 9, "summary": "задание", "why": "срок"}]})), \
                patch.object(jarvis, "load_subscribers", return_value={"chat_ids": [42], "offset": 0}):
            result = jarvis.study_chats_scan(slot="2026-11-13 20:00")
        self.assertFalse(result["ok"])
        state = study_skills.state_load()
        self.assertNotIn("telegram:5", state.get("chatCursors") or {})
        self.assertIn("telegram:5", state["chatPending"])
        self.assertNotIn("lastScanTs", state)

    def test_one_unreadable_chat_does_not_close_the_day(self):
        data = app(studyChats={"enabled": True, "time": "20:00", "folder": "", "maxFolder": "",
                                "notifyEmpty": False, "chats": [{"id": 1, "title": "А"}, {"id": 2, "title": "Б"}]})

        def scan_chat(peer, since):
            if int(peer) == 2:
                raise RuntimeError("flood")
            return {"chat": "А", "chat_id": 1, "me": {"name": "Миша"},
                    "messages": [{"id": 3, "from": "Анна", "text": "привет", "date": "12:00"}]}

        result = self._run(data, scan_chat, slot="2026-11-13 20:00")
        self.assertFalse(result["ok"])
        self.assertTrue(self.sent)
        self.assertIn("flood", self.sent[0][1])
        state = study_skills.state_load()
        self.assertNotEqual(state.get("lastDoneSlot"), "2026-11-13 20:00")
        self.assertIn("telegram:2", state["chatPending"])
        self.assertNotIn("telegram:1", state.get("chatCursors") or {})

    def test_missing_study_route_explains_why_telegram_stays_silent(self):
        def scan_chat(peer, since):
            raise AssertionError("чаты не должны читаться без адресата")

        result = self._run(app(settings={"notifyRouting": {}}), scan_chat, slot="2026-11-13 20:00")
        self.assertFalse(result["ok"])
        self.assertEqual(self.sent, [])
        self.assertIn("Маршрутизация", result["error"])
        self.assertIn("Маршрутизация", study_skills.state_load()["lastError"])

    def test_max_and_telegram_items_are_both_delivered(self):
        def scan_chat(peer, since):
            return {"chat": "Семинар", "chat_id": peer, "me": {"name": "Миша", "username": "m"},
                    "messages": [{"id": 9, "from": "Анна", "text": "задание в телеграме", "date": "12:00"}]}

        def max_scan(peer, since):
            return {"chat": "Семинар", "chat_id": 501, "me": {"name": "Миша"},
                    "messages": [{"id": 77, "from": "Лена", "text": "реферат в максе", "date": "12:00"}]}

        payload = json.dumps({"items": [
            {"chat": "Семинар (MAX)", "message_id": 77, "summary": "реферат", "why": "срок", "urgency": "high"},
            {"chat": "Семинар", "message_id": 9, "summary": "задание", "why": "вопрос"},
        ]}, ensure_ascii=False)
        data = app(studyChats={"enabled": True, "time": "20:00", "folder": "", "maxFolder": "Учеба",
                                "notifyEmpty": False, "chats": [{"id": 5, "title": "Семинар"}]})
        self._install(scan_chat, llm=lambda *a, **k: payload)
        with patch.object(jarvis, "load_app_data", return_value=data), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=lambda *a, **k: payload), \
                patch.object(jarvis, "load_subscribers", return_value={"chat_ids": [42], "offset": 0}), \
                patch.object(jarvis.max_tools, "study_folder_targets", return_value=[{"id": 501, "title": "Семинар", "source": "max"}]), \
                patch.object(jarvis.max_tools, "scan_chat", side_effect=max_scan), \
                patch.object(jarvis.max_user, "connected", return_value=True), \
                patch.object(jarvis.max_tools, "skill_on", return_value=True):
            result = jarvis.study_chats_scan(42, manual=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sent"], 1)
        self.assertGreaterEqual(result["messages"], 2)
        self.assertIn("реферат", self.sent[0][1])
        self.assertIn("задание", self.sent[0][1])
        self.assertIn("max:501", study_skills.state_load().get("notified", [""])[-1] + "".join(study_skills.state_load()["notified"]))

    def test_settings_page_explains_silence(self):
        html = Path(__file__).with_name("index (9).html").read_text(encoding="utf-8")
        self.assertIn("в бот не писал: нечего сообщать", html)
        self.assertIn("Повтор после ошибки", html)
        self.assertIn("Проверять учебные чаты", html)

    def test_telethon_chat_id_is_int_and_still_resolves(self):
        """id диалога из Telethon — int. Раньше _resolve вызывал .strip() и чат не читался."""
        seen = []

        class Client:
            async def get_entity(self, peer):
                seen.append(peer)
                return {"id": peer}

        user_id, group_id = 123456789, -1001234567890
        self.assertEqual(asyncio.run(tg_tools._resolve(Client(), user_id)), {"id": user_id})
        self.assertEqual(asyncio.run(tg_tools._resolve(Client(), group_id)), {"id": group_id})
        self.assertEqual(asyncio.run(tg_tools._resolve(Client(), "  -1001234567890  ")), {"id": group_id})
        self.assertEqual(seen, [user_id, group_id, group_id])

    def test_poisoned_global_cursor_does_not_hide_an_unread_chat(self):
        seen = []

        def scan_chat(peer, since):
            seen.append((peer, since))
            return {"chat": "Михаил", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 3, "from": "Анна", "text": "Миша, где файл?", "mentioned": True,
                                  "date": "2026-11-12 18:00"}]}

        study_skills.state_update(lastScanTs=time.time(), lastResult={"errors": ["Михаил: 'int' object has no attribute 'strip'"]})
        result = self._run(app(), scan_chat, slot="2026-11-13 20:00",
                           llm=lambda *a, **k: json.dumps({"items": []}))
        self.assertEqual(seen[0][0], 5)
        self.assertLess(seen[0][1], time.time() - 2 * 86400)
        self.assertTrue(result["ok"], result)
        self.assertIn("где файл", self.sent[0][1])
