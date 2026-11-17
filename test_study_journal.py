"""Журналы предметов: память, баллы, зачёты. Модель и Telegram подменены."""

import json
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import jarvis  # noqa: E402
import study_journal  # noqa: E402
import study_skills  # noqa: E402


def app(**extra):
    base = {
        "telegramSkills": {},
        "studyChats": {"enabled": True, "time": "20:00", "folder": "", "chats": [{"id": 5, "title": "Матан"}],
                       "notifyEmpty": False, "maxFolder": ""},
        "studySubjects": [{"id": "sub-matan", "name": "Матан"}],
        "studyTasks": [{"id": "t1", "subjectId": "sub-matan", "title": "Реферат", "status": "todo"}],
        "knowledgeNotes": [],
        "settings": {"notifyRouting": {"42": ["study"]}},
    }
    base.update(extra)
    return base


SUMMARY = "Реферат по матану сдавать до 20 ноября, оформление по ГОСТ"


def triage(message_id, summary=SUMMARY, grade=None, exam=None, why="срок"):
    item = {"chat": "Матан", "message_id": message_id, "summary": summary, "why": why,
            "urgency": "normal", "subject": "Матан"}
    if grade is not None:
        item["grade"] = grade
    if exam is not None:
        item["exam"] = exam
    return json.dumps({"items": [item]}, ensure_ascii=False)


GRADE = {"points": 42, "points_max": 100, "need": "60 баллов", "passed": False}
EXAM = {"date": "2026-12-20", "time": "10:00", "place": "ауд. 214", "format": "письменно",
        "deadline": "2026-12-18", "note": "принести зачётку"}


class Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._payload


class StudyJournalTest(unittest.TestCase):
    def setUp(self):
        if study_journal.STORE_FILE.exists():
            study_journal.STORE_FILE.unlink()
        if study_skills.STATE_FILE.exists():
            study_skills.STATE_FILE.unlink()
        if jarvis._study_scan_lock.locked():
            jarvis._study_scan_lock.release()
        self.sent = []
        self.seen_tools = []
        self.seen_prompt = []
        self._saved = (jarvis.tg_bot, jarvis.tg_user, jarvis.tg_tools)

    def tearDown(self):
        jarvis.tg_bot, jarvis.tg_user, jarvis.tg_tools = self._saved
        if jarvis._study_scan_lock.locked():
            jarvis._study_scan_lock.release()

    def test_assistant_writes_updates_and_deletes_without_a_second_grade(self):
        data = app()
        added = jarvis.execute_assistant_tool(
            "study_journal_write",
            {"kind": "grade", "subject": "Матан", "points": 42, "points_max": 100, "need": "60 баллов", "passed": False},
            data, "")
        self.assertTrue(added.get("ok"), added)
        again = jarvis.execute_assistant_tool(
            "study_journal_write",
            {"kind": "grade", "subject": "матан", "points": 55, "passed": False},
            data, "")
        self.assertEqual(again["entry"]["id"], added["entry"]["id"])
        self.assertEqual(again["entry"]["points"], 55)
        self.assertEqual(len([e for e in study_journal.list_entries() if e["kind"] == "grade"]), 1)
        fact = jarvis.execute_assistant_tool(
            "study_journal_write", {"kind": "fact", "subject": "Матан", "text": SUMMARY}, data, "")
        exam = jarvis.execute_assistant_tool(
            "study_journal_write",
            {"kind": "exam", "subject": "Матан", "date": "20 декабря", "place": "ауд. 214",
             "format": "письменно", "deadline": "18 декабря", "note": "зачётка"},
            data, "")
        self.assertEqual(exam["entry"]["date"], "2026-12-20")
        self.assertEqual(exam["entry"]["deadline"], "2026-12-18")
        listed = jarvis.execute_assistant_tool("study_journal_list", {"subject": "Матан"}, data, "")
        self.assertEqual(listed["journal"]["grade"]["points"], 55)
        self.assertEqual(len(listed["journal"]["exams"]), 1)
        self.assertTrue(listed["journal"]["facts"])
        gone = jarvis.execute_assistant_tool("study_journal_delete", {"id": fact["entry"]["id"]}, data, "")
        self.assertTrue(gone.get("ok"), gone)
        self.assertFalse(any(e["id"] == fact["entry"]["id"] for e in study_journal.list_entries()))

    def test_skill_off_hides_tools_prompt_and_refuses(self):
        off = app(telegramSkills={"study_journal": False})
        names = {t["name"] for t in jarvis._assistant_tool_list(off, False)}
        self.assertFalse(names & study_journal.TOOL_NAMES)
        prompt = jarvis.build_assistant_system_prompt(off, False)
        self.assertNotIn("ЖУРНАЛЫ ПРЕДМЕТОВ", prompt)
        self.assertIn("выключен", jarvis.execute_assistant_tool("study_journal_list", {}, off, "")["error"])
        on = jarvis.build_assistant_system_prompt(app(), False)
        self.assertIn("ЖУРНАЛЫ ПРЕДМЕТОВ", on)
        self.assertIn("study_journal_write", {t["name"] for t in jarvis._assistant_tool_list(app(), False)})
        snap = jarvis.execute_assistant_tool("get_data", {"domain": "study"}, app(), "")
        self.assertIn("journal", snap)

    def test_conversation_records_a_grade(self):
        data = app()
        steps = [
            {"type": "tool_use", "id": "t1", "name": "study_journal_write",
             "input": {"kind": "grade", "subject": "Матан", "points": 42, "points_max": 100,
                       "need": "60 для зачёта", "passed": False}},
            {"type": "text", "text": "Записал 42 балла по матану."},
        ]

        def post(url, headers=None, json=None, timeout=None, **kw):
            body = json or {}
            tools = body.get("tools") or []
            self.seen_tools.append({t.get("name") for t in tools})
            system = body.get("system") or ""
            if isinstance(system, list):
                system = "\n".join(b.get("text", "") for b in system)
            self.seen_prompt.append(system)
            step = steps.pop(0)
            if step["type"] == "tool_use":
                content = [step]
                stop = "tool_use"
            else:
                content = [step]
                stop = "end_turn"
            return Resp({"content": content, "stop_reason": stop, "usage": {}})

        with patch.object(jarvis.requests, "post", side_effect=post):
            text, _links = jarvis._assistant_loop_claude(
                [{"role": "user", "content": "По матану у меня 42 балла из 100, для зачёта нужно 60, я ещё не сдал"}],
                "k", "claude-test", data, "", False)
        self.assertIn("42", text)
        self.assertIn("study_journal_write", self.seen_tools[0])
        self.assertIn("ЖУРНАЛЫ ПРЕДМЕТОВ", self.seen_prompt[0])
        rows = study_journal.list_entries("sub-matan")
        grade = next(e for e in rows if e["kind"] == "grade")
        self.assertEqual(grade["points"], 42)
        self.assertEqual(grade["pointsMax"], 100)
        self.assertFalse(grade["passed"])
        self.assertTrue(grade["acked"])

    def _scan(self, llm, manual=False):
        def scan_chat(peer, since):
            return {"chat": "Матан", "chat_id": peer, "me": {"name": "Миша"},
                    "messages": [{"id": 1, "date": "2026-11-17 12:00", "from": "Анна", "text": "см. модель"}]}

        jarvis.tg_user = SimpleNamespace(is_owner_chat=lambda cid: int(cid) == 42,
                                         _load_cfg=lambda: {"session": "ok", "user_id": 42})
        jarvis.tg_tools = SimpleNamespace(scan_chat=scan_chat, list_dialogs=lambda **k: {"dialogs": []})
        jarvis.tg_bot = SimpleNamespace(
            send_text=lambda token, cid, text: self.sent.append((cid, text)) or [1],
            remember_anchor=lambda *a, **k: None,
            seed_dialog=lambda *a, **k: None,
        )
        with patch.object(jarvis, "load_app_data", return_value=app()), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=llm), \
                patch.object(jarvis, "load_subscribers", return_value={"chat_ids": [42], "offset": 0}):
            return jarvis.study_chats_scan(manual=manual, slot="" if manual else "2026-11-17 20:00")

    def test_scan_writes_journal_and_repeat_is_a_reminder_not_news(self):
        calls = {"n": 0}

        def llm(*args, **kwargs):
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                return triage(11, grade=GRADE, exam=EXAM)
            if n == 2:
                return triage(22, grade=GRADE, exam=EXAM)
            if n == 3:
                return triage(33, grade=GRADE, exam=EXAM)
            return triage(44, grade={**GRADE, "points": 70}, exam=EXAM)

        first = self._scan(llm)
        self.assertTrue(first["ok"], first)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("нужно внимание", self.sent[0][1])
        self.assertNotIn("Уже есть в журнале", self.sent[0][1])
        rows = study_journal.list_entries("sub-matan")
        self.assertTrue(any(e["kind"] == "grade" and e["points"] == 42 and e["announced"] for e in rows))
        self.assertTrue(any(e["kind"] == "exam" and e["date"] == "2026-12-20" and e["place"] == "ауд. 214" for e in rows))
        self.assertTrue(any(e["kind"] == "fact" and "ГОСТ" in e["text"] for e in rows) or any(e["kind"] == "exam" for e in rows))

        self.sent.clear()
        second = self._scan(llm)
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("нужно внимание", self.sent[0][1])
        self.assertIn("Уже есть в журнале", self.sent[0][1])
        self.assertIn("Обратили внимание", self.sent[0][1])
        self.assertIn("Матан", self.sent[0][1])

        self.sent.clear()
        third = self._scan(llm)
        self.assertTrue(third["ok"], third)
        self.assertEqual(self.sent, [])

        self.sent.clear()
        fourth = self._scan(llm)
        self.assertTrue(fourth["ok"], fourth)
        self.assertIn("нужно внимание", self.sent[0][1])
        grade = next(e for e in study_journal.list_entries("sub-matan") if e["kind"] == "grade")
        self.assertEqual(grade["points"], 70)

    def test_heuristic_grade_and_exam_without_structured_fields(self):
        def llm(*args, **kwargs):
            return triage(7, summary="По матану 42 из 100, для зачёта нужно 60, пока не сдал. "
                          "Зачёт 20 декабря в 10:00, ауд. 214, письменно, допуск до 18 декабря.",
                          grade=None, exam=None)

        result = self._scan(llm)
        self.assertTrue(result["ok"], result)
        rows = study_journal.list_entries("sub-matan")
        grade = next(e for e in rows if e["kind"] == "grade")
        exam = next(e for e in rows if e["kind"] == "exam")
        self.assertEqual(grade["points"], 42)
        self.assertEqual(grade["pointsMax"], 100)
        self.assertFalse(grade["passed"])
        self.assertEqual(exam["date"], "2026-12-20")
        self.assertEqual(exam["deadline"], "2026-12-18")
        self.assertIn("214", exam["place"])

    def test_mention_stays_news_even_if_the_fact_is_known(self):
        study_journal.write_entry(app(), {"kind": "fact", "subject": "Матан", "text": "Миша, где файл по матану"}, source="scan")
        study_journal.mark_sent([e["id"] for e in study_journal.list_entries()], [])

        def llm(*args, **kwargs):
            return triage(8, summary="Миша, где файл по матану", why="Вас упомянули")

        result = self._scan(llm)
        self.assertEqual(result["sent"], 1)
        self.assertIn("нужно внимание", self.sent[0][1])

    def test_skill_off_scan_does_not_write(self):
        def llm(*args, **kwargs):
            return triage(9, grade=GRADE, exam=EXAM)

        with patch.object(jarvis, "load_app_data", return_value=app(telegramSkills={"study_journal": False})):
            # _scan patches load_app_data itself; call prepare path via a dedicated run
            pass
        jarvis.tg_user = SimpleNamespace(is_owner_chat=lambda cid: True, _load_cfg=lambda: {"session": "ok"})
        jarvis.tg_tools = SimpleNamespace(
            scan_chat=lambda peer, since: {"chat": "Матан", "chat_id": peer, "me": {"name": "Миша"},
                                           "messages": [{"id": 9, "from": "Анна", "text": "x", "date": "12:00"}]},
            list_dialogs=lambda **k: {"dialogs": []})
        jarvis.tg_bot = SimpleNamespace(send_text=lambda *a, **k: self.sent.append(a) or [1],
                                        remember_anchor=lambda *a, **k: None, seed_dialog=lambda *a, **k: None)
        with patch.object(jarvis, "load_app_data", return_value=app(telegramSkills={"study_journal": False})), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=llm), \
                patch.object(jarvis, "load_subscribers", return_value={"chat_ids": [42], "offset": 0}):
            jarvis.study_chats_scan(slot="2026-11-17 20:00")
        self.assertEqual(study_journal.list_entries(), [])
        self.assertTrue(self.sent)
        self.assertIn("нужно внимание", self.sent[0][2])

    def test_user_can_edit_and_backup_includes_the_file(self):
        saved = study_journal.user_apply(app(), {
            "action": "add", "subjectId": "sub-matan", "kind": "fact", "text": "Кабинет 214",
        })
        self.assertTrue(saved.get("ok"), saved)
        edited = study_journal.user_apply(app(), {
            "action": "update", "id": saved["entry"]["id"], "kind": "fact", "text": "Кабинет 215",
        })
        self.assertEqual(edited["entry"]["text"], "Кабинет 215")
        blob = jarvis.build_backup_zip()
        names = zipfile.ZipFile(BytesIO(blob)).namelist()
        self.assertIn("data/jarvis_study_journals.json", names)

    def test_failed_send_does_not_mark_the_fact_as_announced(self):
        def llm(*args, **kwargs):
            return triage(15, grade=None, exam=None)

        def boom(*a, **k):
            raise RuntimeError("telegram down")

        jarvis.tg_user = SimpleNamespace(is_owner_chat=lambda cid: True, _load_cfg=lambda: {"session": "ok"})
        jarvis.tg_tools = SimpleNamespace(
            scan_chat=lambda peer, since: {"chat": "Матан", "chat_id": peer, "me": {"name": "Миша"},
                                           "messages": [{"id": 15, "from": "Анна", "text": SUMMARY, "date": "12:00"}]},
            list_dialogs=lambda **k: {"dialogs": []})
        jarvis.tg_bot = SimpleNamespace(send_text=boom, remember_anchor=lambda *a, **k: None, seed_dialog=lambda *a, **k: None)
        with patch.object(jarvis, "load_app_data", return_value=app()), \
                patch.object(jarvis, "get_token", return_value="BOT"), \
                patch.object(jarvis, "_ai_conf", return_value=("openai", "k", "m", "", "", "triage")), \
                patch.object(jarvis, "_llm_text", side_effect=llm), \
                patch.object(jarvis, "load_subscribers", return_value={"chat_ids": [42], "offset": 0}):
            result = jarvis.study_chats_scan(slot="2026-11-17 20:00")
        self.assertFalse(result["ok"])
        rows = study_journal.list_entries()
        self.assertTrue(rows)
        self.assertFalse(any(e.get("announced") for e in rows))


if __name__ == "__main__":
    unittest.main()
