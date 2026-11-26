"""Резидентское разрешение ассистент кладёт во вкладку «Разрешения», не в заметки бокса.

Модель и Telegram подменены. Напоминания — слоты самого разрешения.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import cars_parking  # noqa: E402
import jarvis  # noqa: E402
import tg_bot  # noqa: E402
import tool_groups  # noqa: E402

CAR = "11111111-1111-4111-8111-111111111111"
CAR2 = "22222222-2222-4222-8222-222222222222"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
UIN = "0355431001000027450720260"
NOTE = "зимняя резина в багажнике"


def app(**extra):
    base = {
        "cars": [
            {"id": CAR, "name": "Бусик", "plate": "Х758КС977", "notes": NOTE},
            {"id": CAR2, "name": "Камри", "plate": "А123ВС77", "notes": ""},
        ],
        "serviceRecords": [],
        "knowledgeNotes": [],
        "telegramSkills": {},
    }
    base.update(extra)
    return base


def card(**extra):
    body = {
        "car": "X758KC977",
        "district": "Таганский",
        "starts": "26.11.2026",
        "ends": "25.11.2027",
        "free_from": "20:00",
        "free_until": "08:00",
        "fee": "3 000 ₽",
        "uin": UIN,
        "reminders": [
            {"mode": "date", "date": "25.10.2027", "time": "10:00",
             "text": "Через месяц заканчивается резидентское разрешение, Таганский."},
            {"mode": "days", "daysBefore": 7, "time": "10:00",
             "text": "Через неделю заканчивается резидентское разрешение."},
        ],
        "use_received_file": "last",
    }
    body.update(extra)
    return body


class Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._payload


class ParkingSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        store = Path(self.tmp.name) / "jarvis_cars.json"
        if store.exists():
            store.unlink()
        tg_bot.set_ctx(None)

    def tearDown(self):
        tg_bot.set_ctx(None)
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def test_catalog_and_skill_switch(self):
        row = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == "parking_permits")
        self.assertEqual(row["risk"], "write")
        self.assertTrue(row["default"])
        self.assertEqual(set(row["tools"]), cars_parking.TOOL_NAMES)
        self.assertIn("Разрешения", row["desc"])
        self.assertIn("заметки", row["desc"])
        on = app()
        names = {t["name"] for t in jarvis._assistant_tool_list(on, True)}
        self.assertTrue(cars_parking.TOOL_NAMES <= names)
        prompt = jarvis.build_assistant_system_prompt(on, True)
        self.assertIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", prompt)
        self.assertIn("не пиши", prompt.lower())
        self.assertIn("plan_create", prompt)
        off = app(telegramSkills={"parking_permits": False})
        hidden = {t["name"] for t in jarvis._assistant_tool_list(off, True)}
        self.assertFalse(hidden & cars_parking.TOOL_NAMES)
        self.assertNotIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", jarvis.build_assistant_system_prompt(off, True))
        self.assertIn("выключен", jarvis.execute_assistant_tool("parking_permit_list", {}, off, "")["error"])
        quiet = jarvis.build_assistant_system_prompt(on, False, groups={"weather"})
        self.assertNotIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", quiet)
        loud = jarvis.build_assistant_system_prompt(on, False, groups={"parking"})
        self.assertIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", loud)
        self.assertEqual(tool_groups.unassigned(jarvis._assistant_tool_list(on, True)), [])
        self.assertIn("parking", tool_groups.pick("резидентское разрешение на Бусика"))
        self.assertIn("parking", tool_groups.pick("раздел Разрешения"))
        self.assertIn("parking", tool_groups.pick("УИН " + UIN))
        self.assertNotIn("parking", tool_groups.pick("какая погода завтра"))
        self.assertNotIn("parking", tool_groups.pick("уведомления разрешены"))
        self.assertEqual(tool_groups.group_of("parking_permit_create"), "parking")

    def test_owner_only(self):
        ctx = SimpleNamespace(chat_id=5)
        with patch.object(jarvis.tg_bot, "get_ctx", return_value=ctx), \
                patch.object(jarvis.tg_user, "is_owner_chat", return_value=False):
            names = {t["name"] for t in jarvis._assistant_tool_list(app(), True)}
            self.assertNotIn("parking_permit_create", names)
            denied = jarvis.execute_assistant_tool("parking_permit_list", {}, app(), "")
        self.assertIn("владельцу", denied["error"])
        self.assertEqual(cars_parking.view()["permits"], [])

    def test_create_by_plate_or_name_keeps_car_notes(self):
        data = app()
        saved = jarvis.execute_assistant_tool(
            "parking_permit_create", {k: v for k, v in card().items() if k != "use_received_file"}, data, "")
        self.assertTrue(saved.get("ok"), saved)
        permit = saved["permit"]
        self.assertEqual(permit["carId"], CAR)
        self.assertEqual(saved["car"]["name"], "Бусик")
        self.assertEqual(permit["zone"], "Таганский")
        self.assertEqual(permit["starts"], "2026-11-26")
        self.assertEqual(permit["ends"], "2027-11-25")
        self.assertEqual(permit["freeFrom"], "20:00")
        self.assertEqual(permit["freeUntil"], "08:00")
        self.assertEqual(permit["fee"], 3000)
        self.assertEqual(permit["uin"], UIN)
        self.assertEqual(permit["number"], "")
        self.assertEqual([r["firesAt"] for r in permit["reminders"]], ["2027-10-25 10:00", "2027-11-18 10:00"])
        self.assertEqual(data["cars"][0]["notes"], NOTE)
        self.assertNotIn("Таганский", data["cars"][0]["notes"])
        self.assertFalse((Path(self.tmp.name) / "jarvis_plan_tasks.json").exists())
        by_name = jarvis.execute_assistant_tool("parking_permit_list", {"car": "бусик"}, data, "")
        self.assertEqual(len(by_name["permits"]), 1)
        self.assertEqual(by_name["permits"][0]["plate"], "Х758КС977")
        disk = json.loads((Path(self.tmp.name) / "jarvis_cars.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["permits"][0]["uin"], UIN)
        self.assertNotIn(NOTE, json.dumps(disk, ensure_ascii=False))
        today = datetime(2027, 11, 25).date()
        self.assertNotEqual(cars_parking.public_permit(disk["permits"][0], today)["phase"], "expired")
        self.assertEqual(cars_parking.public_permit(disk["permits"][0], datetime(2027, 11, 26).date())["phase"], "expired")
        due, missed = cars_parking.partition(datetime(2027, 10, 25, 10, 0, tzinfo=cars_parking.MSK))
        self.assertEqual(missed, [])
        self.assertEqual([item["slot"] for item in due], ["2027-10-25 10:00"])
        cars_parking.mark_done(due[0]["permitId"], due[0]["reminderId"], due[0]["slot"])
        later, _ = cars_parking.partition(datetime(2027, 11, 18, 10, 0, tzinfo=cars_parking.MSK))
        self.assertEqual([item["slot"] for item in later], ["2027-11-18 10:00"])
        self.assertIn("неделю", later[0]["reminder"]["text"])

    def test_ambiguous_car_writes_nothing(self):
        data = app()
        data["cars"].append({"id": "33333333-3333-4333-8333-333333333333", "name": "Бусик", "plate": "О777ОО77"})
        result = jarvis.execute_assistant_tool("parking_permit_create", card(car="Бусик"), data, "")
        self.assertTrue(result.get("needs_clarification"), result)
        self.assertEqual(len(result["cars"]), 2)
        self.assertEqual(cars_parking.view()["permits"], [])
        self.assertEqual(data["cars"][0]["notes"], NOTE)

    def test_update_keeps_reminders_and_cancel_needs_confirmation(self):
        data = app()
        saved = jarvis.execute_assistant_tool(
            "parking_permit_create", {k: v for k, v in card().items() if k != "use_received_file"}, data, "")
        pid = saved["permit"]["id"]
        updated = jarvis.execute_assistant_tool(
            "parking_permit_update", {"car": "Бусик", "district": "Таганский", "fee": 3000}, data, "")
        self.assertTrue(updated.get("ok"), updated)
        self.assertEqual(updated["permit"]["id"], pid)
        self.assertEqual(len(updated["permit"]["reminders"]), 2)
        self.assertEqual(updated["permit"]["fee"], 3000)
        self.assertEqual(updated["permit"]["freeFrom"], "20:00")
        asked = jarvis.execute_assistant_tool("parking_permit_cancel", {"car": "Бусик"}, data, "")
        self.assertTrue(asked.get("needs_confirmation"), asked)
        self.assertEqual(len(cars_parking.view()["permits"]), 1)
        gone = jarvis.execute_assistant_tool("parking_permit_cancel", {"car": "Бусик", "confirmed": True}, data, "")
        self.assertTrue(gone.get("ok"), gone)
        self.assertEqual(cars_parking.view()["permits"], [])
        self.assertEqual(data["cars"][0]["notes"], NOTE)

    def test_received_pdf_is_attached_and_missing_file_still_saves(self):
        data = app()
        tg_bot.set_ctx(tg_bot.Ctx("tok", 7))
        tg_bot.remember_file(7, "разрешение.pdf", PDF)
        with patch.object(jarvis.tg_user, "is_owner_chat", return_value=True):
            saved = jarvis.execute_assistant_tool("parking_permit_create", card(), data, "")
        self.assertTrue(saved.get("ok"), saved)
        self.assertEqual(saved["file"]["name"], "разрешение.pdf")
        self.assertEqual(len(saved["permit"]["files"]), 1)
        opened = cars_parking.open_file(saved["permit"]["files"][0]["id"])
        self.assertEqual(opened[0].read_bytes(), PDF)
        self.assertEqual(data["cars"][0]["notes"], NOTE)
        with patch.object(jarvis.tg_user, "is_owner_chat", return_value=True):
            again = jarvis.execute_assistant_tool("parking_permit_create", card(use_received_file="нет-такого"), data, "")
        self.assertTrue(again.get("ok"), again)
        self.assertIn("не нашёл", again.get("file_error", ""))
        self.assertEqual(again["permit"]["id"], saved["permit"]["id"])
        self.assertEqual(len(cars_parking.view()["permits"]), 1)

    def test_conversation_writes_permit_not_notes(self):
        data = app()
        tg_bot.set_ctx(tg_bot.Ctx("tok", 9))
        tg_bot.remember_file(9, "permit.pdf", PDF)
        payload = {k: v for k, v in card().items() if k != "use_received_file"}
        payload["use_received_file"] = "permit.pdf"
        user = [{"role": "user", "content": "Добавь в систему поставь напоминания и отпишись что сделал"}]

        def run_claude():
            seen = []

            def post(url, headers=None, json=None, timeout=None, **kw):
                body = json or {}
                seen.append(body)
                if len(seen) == 1:
                    return Resp({"content": [{"type": "tool_use", "id": "t1", "name": "parking_permit_create", "input": payload}],
                                 "stop_reason": "tool_use", "usage": {}})
                return Resp({"content": [{"type": "text", "text": "Записал в разрешения."}], "stop_reason": "end_turn", "usage": {}})

            with patch.object(jarvis.requests, "post", side_effect=post):
                text, _links = jarvis._assistant_loop_claude(user, "k", "claude-test", data, "", True)
            return text, seen

        with patch.object(jarvis.tg_user, "is_owner_chat", return_value=True):
            text, seen = run_claude()
            self.assertEqual(text, "Записал в разрешения.")
            self.assertIn("parking_permit_create", {t["name"] for t in seen[0]["tools"]})
            system = seen[0]["system"]
            self.assertIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", system)
            self.assertIn("вкладка «Разрешения»", system)
            result = json.loads(seen[1]["messages"][-1]["content"][0]["content"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["permit"]["uin"], UIN)
            self.assertEqual(result["where"], "Автомобили → Разрешения")
            disk = json.loads((Path(self.tmp.name) / "jarvis_cars.json").read_text(encoding="utf-8"))
            self.assertEqual(disk["permits"][0]["zone"], "Таганский")
            self.assertEqual(disk["permits"][0]["fee"], 3000)
            self.assertEqual(len(disk["permits"][0]["files"]), 1)
            self.assertEqual(data["cars"][0]["notes"], NOTE)
            self.assertNotIn(UIN, data["cars"][0]["notes"])

            data["cars"][0]["notes"] = NOTE
            steps = [
                {"content": "", "tool_calls": [{"id": "p1", "type": "function", "function": {
                    "name": "parking_permit_update", "arguments": json.dumps({"car": "Бусик", "fee": 3000})}}],
                 "usage": {}, "effort": "", "truncated": False},
                {"content": "Поправил плату.", "tool_calls": [], "usage": {}, "effort": "", "truncated": False},
            ]
            with patch.object(jarvis, "_openai_step", side_effect=steps) as model:
                answer, _ = jarvis._assistant_loop_openai(user, "key", "m", data, "", True)
            self.assertEqual(answer, "Поправил плату.")
            messages = model.call_args_list[0].args[3]
            tools = model.call_args_list[0].args[4]
            self.assertIn("parking_permit_update", {t["function"]["name"] for t in tools})
            self.assertIn("РЕЗИДЕНТСКИЕ РАЗРЕШЕНИЯ", messages[0]["content"])
            self.assertEqual(data["cars"][0]["notes"], NOTE)
            self.assertEqual(cars_parking.view()["permits"][0]["fee"], 3000)


class ParkingToolHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self._paths = (jarvis.DATA_DIR, jarvis.APP_DATA_FILE)
        jarvis.DATA_DIR = Path(self.tmp.name)
        jarvis.APP_DATA_FILE = jarvis.DATA_DIR / "jarvis_app_data.json"
        jarvis.save_app_data(app())
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        jarvis.DATA_DIR, jarvis.APP_DATA_FILE = self._paths
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=False):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if cookie:
            headers["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_tools_route_and_create(self):
        code, _ = self.req("GET", "/api/cars/parking/tools")
        self.assertEqual(code, 401)
        code, body = self.req("GET", "/api/cars/parking/tools", cookie=True)
        self.assertEqual(code, 200)
        self.assertIn("parking_permit_create", {t["name"] for t in body["tools"]})
        self.assertIn("Разрешения", body["prompt"])
        code, created = self.req("POST", "/api/cars/parking/tool", {
            "name": "parking_permit_create",
            "input": {k: v for k, v in card().items() if k != "use_received_file"},
        }, cookie=True)
        self.assertEqual(code, 200, created)
        self.assertEqual(created["permit"]["zone"], "Таганский")
        self.assertEqual(created["permit"]["uin"], UIN)
        stored = json.loads(jarvis.APP_DATA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored["cars"][0]["notes"], NOTE)


if __name__ == "__main__":
    unittest.main()
