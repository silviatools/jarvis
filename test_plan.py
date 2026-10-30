"""Плановые задачи ассистента: подмена ИИ и Telegram — без настоящих сервисов."""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name
if "pyaes" not in sys.modules:
    try:
        import pyaes  # noqa: F401
    except ImportError:    # песочница без сборки pyaes: Telethon он нужен только для импорта
        sys.modules["pyaes"] = types.ModuleType("pyaes")

import jarvis      # noqa: E402
import plan_skill  # noqa: E402

fmt = "%Y-%m-%d %H:%M"


def in_(minutes):
    return (plan_skill.now_msk() + timedelta(minutes=minutes)).strftime(fmt)


class R:
    def __init__(self, j):
        self.ok, self._j, self.status_code = True, j, 200

    def json(self):
        return self._j


class PlanUnitTest(unittest.TestCase):
    def setUp(self):
        p = plan_skill.store_path()
        if p.exists():
            p.unlink()

    def test_remind_and_task_report_must_be_asked(self):
        r = plan_skill.execute("plan_create", {"kind": "remind", "text": "Позвонить маме", "in_minutes": 30}, {}, 77)
        self.assertTrue(r["ok"], r)
        r = plan_skill.execute("plan_create", {"kind": "task", "text": "Собери сводку", "in_minutes": 30}, {}, 77)
        self.assertTrue(r.get("needs_answer"))
        self.assertEqual(plan_skill.summary()["total"], 1)           # поручение без ответа про отчёт не поставлено
        r = plan_skill.execute("plan_create", {"kind": "task", "text": "Собери сводку", "in_minutes": 30,
                                               "report": False, "repeat": "daily"}, {}, 77)
        self.assertTrue(r["ok"] and r["report"] is False and r["repeat"] == "каждый день", r)

    def test_validation(self):
        for bad in ({"kind": "x", "text": "a", "in_minutes": 5}, {"kind": "remind", "text": "", "in_minutes": 5},
                    {"kind": "remind", "text": "a"}, {"kind": "remind", "text": "a", "when": "вчера"},
                    {"kind": "remind", "text": "a", "when": "2020-01-01 10:00"},
                    {"kind": "remind", "text": "a", "in_minutes": 5, "repeat": "hourly"}):
            self.assertIn("error", plan_skill.execute("plan_create", bad, {}), bad)
        self.assertEqual(plan_skill.summary()["total"], 0)

    def test_next_run_monthly_clamps_and_skips_past(self):
        after = datetime(2026, 10, 29, 12, 0, tzinfo=plan_skill.MSK)
        self.assertEqual(plan_skill.next_run("2026-01-31 09:00", "monthly", after), "2026-10-31 09:00")
        self.assertEqual(plan_skill.next_run("2027-02-28 09:00", "monthly", after, 31), "2027-03-31 09:00")   # якорь 31-е
        self.assertEqual(plan_skill.next_run("2026-10-01 09:00", "daily", after), "2026-10-30 09:00")
        self.assertEqual(plan_skill.next_run("2026-10-01 09:00", "weekly", after), "2026-11-05 09:00")

    def test_claim_finish_cycle(self):
        a = plan_skill.create({"kind": "remind", "text": "один раз", "in_minutes": 1})["id"]
        b = plan_skill.create({"kind": "remind", "text": "каждый день", "in_minutes": 1, "repeat": "daily"})["id"]
        far = plan_skill.create({"kind": "remind", "text": "потом", "in_minutes": 600})["id"]
        later = plan_skill.now_msk() + timedelta(minutes=5)
        got = {t["id"] for t in plan_skill.claim_due(later)}
        self.assertEqual(got, {a, b})
        self.assertEqual(plan_skill.claim_due(later), [])             # второй раз не берёт: уже «выполняется»
        plan_skill.finish(a, True, "ок")
        plan_skill.finish(b, False, "упало")
        by = {t["id"]: t for t in plan_skill.list_tasks()}
        self.assertEqual((by[a]["status"], by[a]["result"]), ("done", "ок"))
        self.assertEqual(by[b]["status"], "scheduled")                # повтор: ждёт следующий срок
        self.assertGreater(by[b]["run_at"], later.strftime(fmt))
        self.assertEqual(by[b]["runs"], 1)
        self.assertEqual(by[far]["status"], "scheduled")

    def test_overdue_and_stale(self):
        a = plan_skill.create({"kind": "remind", "text": "x", "in_minutes": 1})["id"]
        b = plan_skill.create({"kind": "remind", "text": "y", "in_minutes": 1, "repeat": "daily"})["id"]
        week = plan_skill.now_msk() + timedelta(days=7)
        self.assertEqual(plan_skill.claim_due(week), [])
        by = {t["id"]: t for t in plan_skill.list_tasks()}
        self.assertEqual(by[a]["status"], "missed")
        self.assertEqual(by[b]["status"], "scheduled")
        self.assertGreater(by[b]["run_at"], week.strftime(fmt))
        c = plan_skill.create({"kind": "remind", "text": "z", "in_minutes": 1})["id"]
        plan_skill.claim_due(plan_skill.now_msk() + timedelta(minutes=2))
        plan_skill.claim_due(plan_skill.now_msk() + timedelta(hours=1))
        self.assertEqual({t["id"]: t for t in plan_skill.list_tasks()}[c]["status"], "failed")

    def test_cancel_update_delete_list(self):
        a = plan_skill.create({"kind": "remind", "text": "Купить хлеб", "title": "Хлеб", "in_minutes": 60})["id"]
        r = plan_skill.execute("plan_update", {"title_match": "хлеб", "in_minutes": 120, "repeat": "weekly"}, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(plan_skill.execute("plan_list", {"status": "scheduled"}, {})["tasks"][0]["repeat_ru"], "каждую неделю")
        self.assertTrue(plan_skill.execute("plan_cancel", {"id": a}, {})["ok"])
        self.assertIn("error", plan_skill.execute("plan_cancel", {"id": a}, {}))
        self.assertEqual(plan_skill.claim_due(plan_skill.now_msk() + timedelta(days=1)), [])
        self.assertEqual(plan_skill.list_tasks()[0]["status"], "cancelled")
        plan_skill.delete(a)
        self.assertEqual(plan_skill.summary()["total"], 0)

    def test_run_now_and_switch(self):
        a = plan_skill.create({"kind": "remind", "text": "x", "in_minutes": 600})["id"]
        plan_skill.run_now(a)
        self.assertEqual([t["id"] for t in plan_skill.claim_due()], [a])
        self.assertIn("выключен", plan_skill.execute("plan_list", {}, {"plan": False})["error"])
        self.assertEqual(plan_skill.voice_payload({"plan": False}), {"tools": [], "prompt": ""})
        self.assertIn("plan_create", plan_skill.prompt_section({}))

    def test_unattended_cannot_schedule(self):
        r = plan_skill.execute("plan_create", {"kind": "remind", "text": "x", "in_minutes": 5}, {}, None, True)
        self.assertIn("нельзя", r["error"])


class PlanAssistantTest(unittest.TestCase):
    def setUp(self):
        p = plan_skill.store_path()
        if p.exists():
            p.unlink()
        self.app = {"telegramSkills": {}, "settings": {"aiPresets": [{"id": "a", "name": "n", "provider": "claude", "apiKey": "k", "model": ""}],
                                                      "aiAssignments": {"voiceAssistant": "a"}}}
        self.sent = []
        self._send, self._token, self._chat = jarvis.tg_bot.send_text, jarvis.get_token, jarvis._plan_chat
        self._load, self._post, self._owner = jarvis.load_app_data, jarvis.requests.post, jarvis.tg_user.is_owner_chat
        self._exec_tg = jarvis.execute_tg_tool
        jarvis.tg_bot.send_text = lambda token, chat, text, *a, **k: self.sent.append((chat, text))
        jarvis.get_token = lambda: "TOKEN"
        jarvis._plan_chat = lambda task: task.get("chat_id") or 555
        jarvis.load_app_data = lambda: self.app
        jarvis.tg_user.is_owner_chat = lambda c: True

    def tearDown(self):
        jarvis.tg_bot.send_text, jarvis.get_token, jarvis._plan_chat = self._send, self._token, self._chat
        jarvis.load_app_data, jarvis.requests.post, jarvis.tg_user.is_owner_chat = self._load, self._post, self._owner
        jarvis.execute_tg_tool = self._exec_tg

    def make(self, **kw):
        base = {"kind": "task", "text": "Проверь учебные чаты", "title": "Чаты", "in_minutes": 1, "report": True}
        base.update(kw)
        tid = plan_skill.create(base, 42)["id"]
        task = next(t for t in plan_skill.claim_due(plan_skill.now_msk() + timedelta(minutes=2)) if t["id"] == tid)
        return task

    def model(self, calls, final="Всё проверил: новостей нет."):
        seen = []

        def claude(url, headers=None, json=None, timeout=None):
            seen.append(json)
            n = len(seen)
            if n <= len(calls):
                return R({"content": [{"type": "tool_use", "id": f"t{n}", **calls[n - 1]}]})
            return R({"content": [{"type": "text", "text": final}], "stop_reason": "end_turn"})
        jarvis.requests.post = claude
        return seen

    def test_remind_goes_to_chat(self):
        task = self.make(kind="remind", text="Выпить воду", report=None)
        jarvis._plan_worker(task)
        self.assertEqual(self.sent, [(42, "⏰ <b>Напоминание</b>\n\nВыпить воду")])
        self.assertEqual(plan_skill.list_tasks()[0]["status"], "done")

    def test_task_report_yes(self):
        seen = self.model([])
        jarvis._plan_worker(self.make())
        t = plan_skill.list_tasks()[0]
        self.assertEqual((t["status"], t["result"]), ("done", "Всё проверил: новостей нет."))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Плановая задача выполнена", self.sent[0][1])
        self.assertIn("Всё проверил", self.sent[0][1])
        sys_prompt = seen[0]["system"]
        if isinstance(sys_prompt, list):    # Claude: системный промпт блоками (кэш)
            sys_prompt = "\n".join(b["text"] for b in sys_prompt)
        self.assertIn("РЕЖИМ ПЛАНОВОЙ ЗАДАЧИ", sys_prompt)
        self.assertIn("Проверь учебные чаты", seen[0]["messages"][0]["content"])

    def test_task_report_no_is_silent_but_failure_is_not(self):
        self.model([])
        jarvis._plan_worker(self.make(report=False))
        self.assertEqual(self.sent, [])
        self.assertEqual(plan_skill.list_tasks()[0]["status"], "done")

        def boom(*a, **k):
            raise RuntimeError("нет сети")
        jarvis.requests.post = boom
        jarvis._plan_worker(self.make(report=False, title="Упадёт"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("не выполнена", self.sent[0][1])
        self.assertEqual(plan_skill.list_tasks(status="failed")[0]["title"], "Упадёт")

    def test_confirmation_cannot_be_faked_in_unattended_run(self):
        got = []
        jarvis.execute_tg_tool = lambda name, inp, state=None: got.append(dict(inp)) or {"needs_confirmation": True}
        self.model([{"name": "tg_send_message", "input": {"peer": "@x", "text": "привет", "confirmed": True}},
                    {"name": "plan_create", "input": {"kind": "remind", "text": "ещё", "in_minutes": 5}}])
        jarvis._plan_worker(self.make(report=False))
        self.assertEqual(got, [{"peer": "@x", "text": "привет"}])     # confirmed отрезан
        self.assertEqual(plan_skill.summary()["total"], 1)             # новых задач не наплодил
        self.assertFalse(getattr(jarvis._plan_scope, "on", False))

    def test_skill_catalog_switch_and_tools(self):
        self.assertIn("plan", [s["id"] for s in jarvis.assistant_skills_catalog()])
        names = {t["name"] for t in jarvis._assistant_tool_list(self.app, False)}
        self.assertTrue({"plan_create", "plan_list", "plan_update", "plan_cancel"} <= names)
        off = {"telegramSkills": {"plan": False}}
        self.assertFalse({t["name"] for t in jarvis._assistant_tool_list(off, False)} & plan_skill.TOOL_NAMES)
        self.assertNotIn("ПЛАНИРОВАНИЕ", jarvis.build_assistant_system_prompt(off, False))
        self.assertIn("ПЛАНИРОВАНИЕ", jarvis.build_assistant_system_prompt(self.app, False))
        self.assertIn("выключен", jarvis.execute_assistant_tool("plan_list", {}, off, "")["error"])

    def test_chat_creates_task_end_to_end(self):
        """Диалог: модель ставит напоминание из бота — задача появляется с чатом и временем."""
        ctx = jarvis.tg_bot.Ctx("TOKEN", 42)
        jarvis.tg_bot.set_ctx(ctx)
        try:
            self.model([{"name": "plan_create", "input": {"kind": "remind", "text": "Позвонить", "in_minutes": 20}}],
                       final="Поставил.")
            text, _ = jarvis._assistant_loop_claude([{"role": "user", "content": "напомни через 20 минут позвонить"}],
                                                    "k", "m", self.app, "")
        finally:
            jarvis.tg_bot.set_ctx(None)
        self.assertEqual(text, "Поставил.")
        t = plan_skill.list_tasks()[0]
        self.assertEqual((t["kind"], t["status"], t["source"]), ("remind", "scheduled", "bot"))
        self.assertEqual(plan_skill._load()[0]["chat_id"], 42)


if __name__ == "__main__":
    unittest.main()
