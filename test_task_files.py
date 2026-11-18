"""Вложения обычных задач «Дела → Задачи»: тот же диск и те же права, что у учёбы."""

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import jarvis
import tool_groups


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def board():
    return {
        "studySubjects": [{"id": "s1", "name": "Менеджмент"}],
        "studyTasks": [{
            "id": "t1", "subjectId": "s1", "title": "Эссе", "status": "todo", "attachments": [],
        }],
        "kanban": {"columns": [
            {"id": "col-1", "title": "К выполнению", "tasks": [
                {"id": "k1", "title": "Купить молоко", "attachments": []},
                {"id": "k2", "title": "Молоко домой", "attachments": []},
            ]},
            {"id": "col-2", "title": "Готово", "tasks": []},
        ]},
    }


class TaskFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data = jarvis.DATA_DIR
        self._app = jarvis.APP_DATA_FILE
        jarvis.DATA_DIR = Path(self.tmp.name)
        jarvis.APP_DATA_FILE = jarvis.DATA_DIR / "jarvis_app_data.json"
        self.app = board()

    def tearDown(self):
        jarvis.DATA_DIR = self._data
        jarvis.APP_DATA_FILE = self._app
        self.tmp.cleanup()

    def task(self, tid="k1"):
        found, _col = jarvis._find_kanban_task(self.app, tid)
        return found

    def test_attach_read_and_kind(self):
        made = jarvis.kanban_attach_file(self.app, {
            "task_id": "k1", "filename": "список.txt", "content": "молоко и хлеб",
        })
        self.assertTrue(made.get("ok"), made)
        att = self.task()["attachments"][0]
        self.assertEqual(att["name"], "список.txt")
        self.assertEqual(att["store"], "files")
        self.assertEqual(att["kind"], "file")
        self.assertTrue((jarvis.DATA_DIR / "files" / att["filename"]).is_file())
        got = jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "action": "read"})
        self.assertIn("молоко и хлеб", got["text"])
        pic = jarvis.kanban_attach_file(self.app, {
            "task_title": "Купить молоко", "filename": "фото.png",
            "content_base64": base64.b64encode(PNG).decode(),
        })
        self.assertEqual(pic.get("file"), "фото.png", pic)
        self.assertEqual(self.task()["attachments"][1]["kind"], "image")
        denied = jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "attachment": "фото.png", "action": "read"})
        self.assertIn("изображение", denied["error"])
        listed = jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "action": "read"})
        self.assertEqual(len(listed["attachments"]), 2)

    def test_ambiguous_and_bad_format_write_nothing(self):
        before = list((jarvis.DATA_DIR / "files").glob("*")) if (jarvis.DATA_DIR / "files").exists() else []
        amb = jarvis.kanban_attach_file(self.app, {"task_title": "молок", "filename": "a.txt", "content": "x"})
        self.assertIn("несколько", amb["error"])
        self.assertEqual(self.task()["attachments"], [])
        bad = jarvis.kanban_attach_file(self.app, {"task_id": "k1", "filename": "вирус.exe", "content": "x"})
        self.assertIn("не поддерживается", bad["error"])
        empty = jarvis.kanban_attach_file(self.app, {"task_id": "k1", "filename": "пусто.txt", "content": "   "})
        self.assertIn("не удалось собрать", empty["error"])
        after = list((jarvis.DATA_DIR / "files").glob("*")) if (jarvis.DATA_DIR / "files").exists() else []
        self.assertEqual(before, after)

    def test_update_cannot_replace_attachments(self):
        jarvis.kanban_attach_file(self.app, {"task_id": "k1", "filename": "a.txt", "content": "один"})
        kept = list(self.task()["attachments"])
        res = jarvis.kanban_update_task(self.app, "k1", {"title": "Другое", "attachments": []})
        self.assertIn("kanban_attach_file", res["error"])
        self.assertEqual(self.task()["title"], "Купить молоко")
        self.assertEqual(self.task()["attachments"], kept)
        ok = jarvis.kanban_update_task(self.app, "k1", {"title": "Другое"})
        self.assertTrue(ok.get("ok"))
        self.assertEqual(self.task()["attachments"], kept)

    def test_delete_removes_own_file_and_keeps_shared(self):
        jarvis.kanban_attach_file(self.app, {"task_id": "k1", "filename": "своё.txt", "content": "только задача"})
        own = self.task()["attachments"][0]["filename"]
        shared = "same-file.txt"
        (jarvis.DATA_DIR / "files").mkdir(parents=True, exist_ok=True)
        (jarvis.DATA_DIR / "files" / shared).write_bytes("общий".encode())
        meta = {"id": "s", "store": "files", "filename": shared, "name": "общий.txt", "kind": "file", "size": 6}
        self.task()["attachments"].append(dict(meta))
        self.app["studyTasks"][0]["attachments"] = [dict(meta, id="study")]
        self.app["bodyModes"] = [{"id": "m", "workoutFile": {"filename": own}}]
        # своё.txt ещё и файл режима — удаление задачи его не трогает
        jarvis.kanban_delete_task(self.app, "k1")
        self.assertFalse(jarvis._find_kanban_task(self.app, "k1")[0])
        self.assertTrue((jarvis.DATA_DIR / "files" / own).is_file())
        self.assertTrue((jarvis.DATA_DIR / "files" / shared).is_file())
        text = jarvis.study_get_attachment(self.app, {"task_id": "t1", "action": "read"})
        self.assertIn("общий", text["text"])
        # режим больше не ссылается — файл задачи стирается, учебный остаётся
        self.app["bodyModes"] = []
        jarvis.kanban_attach_file(self.app, {"task_id": "k2", "filename": "ещё.txt", "content": "второй"})
        other = jarvis._find_kanban_task(self.app, "k2")[0]["attachments"][0]["filename"]
        # вернём own только на k2 и удалим k2: own нигде больше нет
        jarvis._find_kanban_task(self.app, "k2")[0]["attachments"] = [
            {"id": "x", "store": "files", "filename": own, "name": "своё.txt", "kind": "file", "size": 1},
        ]
        jarvis.kanban_delete_task(self.app, "k2")
        self.assertFalse((jarvis.DATA_DIR / "files" / own).is_file())
        self.assertTrue((jarvis.DATA_DIR / "files" / shared).is_file())
        # «ещё.txt» сняли с задачи до удаления — этот вызов его не трогает.
        self.assertTrue((jarvis.DATA_DIR / "files" / other).is_file())

    def test_path_is_rejected(self):
        (jarvis.DATA_DIR / "files").mkdir(parents=True, exist_ok=True)
        secret = jarvis.DATA_DIR / "secret.txt"
        secret.write_text("нет", encoding="utf-8")
        self.task()["attachments"] = [{
            "id": "bad", "store": "files", "filename": "../secret.txt", "name": "secret.txt", "kind": "file", "size": 3,
        }]
        res = jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "action": "read"})
        self.assertIn("некорректн", res["error"])
        self.assertTrue(secret.is_file())

    def test_create_if_missing_and_execute_saves(self):
        made = jarvis.execute_assistant_tool("kanban_attach_file", {
            "task_title": "Новая задача", "create_if_missing": True, "column_id": "col-2",
            "filename": "заметка.txt", "content": "текст заметки",
        }, self.app, "")
        self.assertTrue(made.get("ok"), made)
        self.assertEqual(made["column"], "Готово")
        saved = json.loads(jarvis.APP_DATA_FILE.read_text(encoding="utf-8"))
        titles = [t["title"] for c in saved["kanban"]["columns"] for t in c["tasks"]]
        self.assertIn("Новая задача", titles)
        read = jarvis.execute_assistant_tool("kanban_get_attachment", {
            "task_title": "Новая задача", "action": "read",
        }, saved, "")
        self.assertIn("текст заметки", read["text"])

    def test_study_attachment_still_works(self):
        made = jarvis.study_attach_file(self.app, {
            "subject": "Менеджмент", "task_title": "Эссе", "filename": "ответ.txt", "content": "тезисы",
        })
        self.assertTrue(made.get("ok"), made)
        self.assertEqual(made["subject"], "Менеджмент")
        got = jarvis.study_get_attachment(self.app, {"task_id": "t1", "action": "read"})
        self.assertIn("тезисы", got["text"])
        self.assertEqual(jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "action": "read"})["error"], "у задачи нет вложений")
        names = {t["name"] for t in jarvis._assistant_tool_list({}, False)}
        self.assertTrue({"kanban_attach_file", "kanban_get_attachment", "study_attach_file", "study_get_attachment"} <= names)
        self.assertEqual(tool_groups.group_of("kanban_attach_file"), "house")
        self.assertEqual(tool_groups.group_of("kanban_get_attachment"), "house")
        self.assertEqual(tool_groups.group_of("study_attach_file"), "study")
        prompt = jarvis.build_assistant_system_prompt({})
        self.assertIn("kanban_attach_file", prompt)
        self.assertIn("study_attach_file", prompt)
        self.assertFalse(jarvis.route_is_public("/api/files/x.txt"))
        self.assertFalse(jarvis.route_is_public("/api/photos/x.jpg"))

    def test_send_needs_chat(self):
        jarvis.kanban_attach_file(self.app, {"task_id": "k1", "filename": "a.txt", "content": "да"})
        res = jarvis.kanban_get_attachment(self.app, {"task_id": "k1", "action": "send"})
        self.assertIn("Telegram", res["error"])


class TaskFileHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data = jarvis.DATA_DIR
        self._app = jarvis.APP_DATA_FILE
        jarvis.DATA_DIR = Path(self.tmp.name)
        jarvis.APP_DATA_FILE = jarvis.DATA_DIR / "jarvis_app_data.json"
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        jarvis.DATA_DIR = self._data
        jarvis.APP_DATA_FILE = self._app
        self.tmp.cleanup()

    def raw(self, method, path, body=None, cookie=False, headers=None):
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "")

    def test_guest_cannot_upload_or_read_task_or_study_file(self):
        payload = "секрет задачи".encode()
        code, body, _ = self.raw("POST", "/api/files?ext=txt", payload)
        self.assertEqual(code, 401)
        code, body, _ = self.raw("GET", "/api/files/whatever.txt")
        self.assertEqual(code, 401)
        code, body, ctype = self.raw("POST", "/api/files?ext=txt", payload, cookie=True)
        self.assertEqual(code, 200, body)
        name = json.loads(body)["filename"]
        self.assertTrue(name.endswith(".txt"))
        code, body, _ = self.raw("GET", f"/api/files/{name}")
        self.assertEqual(code, 401)
        code, body, ctype = self.raw("GET", f"/api/files/{name}", cookie=True)
        self.assertEqual(code, 200)
        self.assertEqual(body, payload)
        self.assertIn("text/plain", ctype)
        code, _, _ = self.raw("DELETE", f"/api/files/{name}")
        self.assertEqual(code, 401)
        self.assertTrue((jarvis.DATA_DIR / "files" / name).is_file())
        code, _, _ = self.raw("DELETE", f"/api/files/{name}", cookie=True)
        self.assertEqual(code, 200)
        self.assertFalse((jarvis.DATA_DIR / "files" / name).is_file())
        code, _, _ = self.raw("GET", "/api/files/../secret.txt", cookie=True)
        self.assertIn(code, (400, 404))


if __name__ == "__main__":
    unittest.main()
