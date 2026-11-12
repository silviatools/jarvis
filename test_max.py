"""Личный MAX: подмена клиента вместо pymax, без сети и без настоящей переписки."""

import base64
import contextlib
import io
import json
import os
import tempfile
import time
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import jarvis  # noqa: E402
import max_tools  # noqa: E402
import max_user  # noqa: E402
import tool_groups  # noqa: E402

PHONE = "+79990001122"
CODE = "1357"
PASSWORD = "right-password"
STUDY_TEXT = "Миша, РЕФЕРАТ-УЧЕБА до пятницы"
WORK_TEXT = "РАБОЧИЙ-СЕКРЕТ не для учёбы"
NOW = int(time.time())
BLOB = b"full-session-blob-not-a-token"
BOX = {"calls": [], "lookups": [], "fail": "", "closed": 0}


def person(uid, name):
    return SimpleNamespace(
        id=uid,
        contact=SimpleNamespace(id=uid, names=[SimpleNamespace(name=name, first_name=name, last_name="")]),
    )


def message(**kw):
    base = {"attaches": [], "link": None, "elements": []}
    base.update(kw)
    return SimpleNamespace(**base)


ME = person(7, "Миша")
CHATS = [
    SimpleNamespace(id=301, type="DIALOG", title=None, participants={7: 0, 99: 0},
                    new_messages=1, last_event_time=NOW, last_message=None, options={}),
    SimpleNamespace(id=501, type="CHAT", title="Семинар", participants={},
                    new_messages=2, last_event_time=NOW, last_message=None, options={}),
    SimpleNamespace(id=502, type="CHAT", title="Работа", participants={},
                    new_messages=0, last_event_time=NOW, last_message=None, options={"mute": -1}),
]
FOLDERS = [
    SimpleNamespace(id="study", title="Учеба", include=[501]),
    SimpleNamespace(id="work", title="Работа", include=[502]),
]
HISTORY = {
    501: [
        message(id=77, sender=99, text=STUDY_TEXT, time=NOW * 1000, link=SimpleNamespace(message_id=78)),
        message(id=78, sender=7, text="принял", time=NOW),
    ],
    502: [message(id=90, sender=99, text=WORK_TEXT, time=NOW * 1000)],
    301: [message(id=3, sender=99, text="привет", time=NOW)],
}


class FakeClient:
    def __init__(self, phone, work, code_provider, password_provider):
        self.phone = phone
        self.work = Path(work)
        self.code_provider = code_provider
        self.password_provider = password_provider
        self.is_connected = False
        self.me = None
        self.users = {99: person(99, "Лена"), 7: ME}

    async def connect(self):
        self.work.mkdir(parents=True, exist_ok=True)
        db = self.work / "session.db"
        if db.exists() and db.read_bytes().startswith(b"full"):
            self.is_connected = True
            self.me = ME
            return
        db.write_bytes(b"half-session")
        os.chmod(db, 0o600)
        code = await self.code_provider.get_code(self.phone)
        if code != CODE:
            raise RuntimeError("bad code")
        password = await self.password_provider.get_password("hint")
        if password != PASSWORD:
            raise RuntimeError("bad password")
        db.write_bytes(BLOB)
        os.chmod(db, 0o600)
        self.is_connected = True
        self.me = ME

    async def close(self):
        self.is_connected = False
        BOX["closed"] += 1

    async def fetch_chats(self):
        return list(CHATS)

    async def get_folders(self):
        return SimpleNamespace(folders=list(FOLDERS))

    async def get_users(self, ids):
        BOX["lookups"].extend(int(i) for i in ids)
        return [self.users[i] for i in ids if i in self.users]

    async def fetch_history(self, chat_id, backward=40, interactive=False, from_time=None, from_=None, **kw):
        BOX["calls"].append({
            "chat_id": int(chat_id), "backward": backward, "interactive": interactive,
            "from_time": from_ if from_ is not None else from_time,
        })
        if BOX["fail"]:
            raise RuntimeError(BOX["fail"])
        return list(HISTORY.get(int(chat_id), []))


def factory(phone, work, code_provider, password_provider):
    return FakeClient(phone, work, code_provider, password_provider)


def secret_path():
    return Path(os.environ["DATA_DIR"]) / "jarvis_max_user.json"


def session_path():
    return Path(os.environ["DATA_DIR"]) / "jarvis_max" / "session.db"


def mode(path):
    return path.stat().st_mode & 0o777


def login_fresh():
    """Полный вход с проверкой, что половинчатый session.db ещё не «подключён»."""
    max_user.logout()
    state = max_user.login_state()
    started = max_user.auth_start(PHONE)
    if id(max_user.login_state()) != id(state):
        raise AssertionError("состояние входа подменили другим dict")
    if started.get("step") != "code":
        raise AssertionError(started)
    if not session_path().exists() or not session_path().read_bytes().startswith(b"half"):
        raise AssertionError("session.db должен появиться до подтверждения кода")
    mid = max_user.status()
    if mid.get("connected") or "session" in mid or PHONE[1:] in json.dumps(mid):
        raise AssertionError(mid)
    step = max_user.auth_code(CODE)
    if step.get("step") != "password":
        raise AssertionError(step)
    done = max_user.auth_password(PASSWORD)
    if not done.get("ok") or id(max_user.login_state()) != id(state):
        raise AssertionError(done)
    raw = secret_path().read_text(encoding="utf-8")
    meta = json.loads(raw)
    rest = dict(meta)
    blob = rest.pop("session")
    if PASSWORD in raw or CODE in json.dumps(rest):
        raise AssertionError("пароль или SMS-код попали в файл сессии")
    if base64.b64decode(blob) != session_path().read_bytes():
        raise AssertionError("в файле не полный blob сессии")
    if mode(secret_path()) != 0o600 or mode(session_path()) != 0o600:
        raise AssertionError("секрет или session.db не 600")
    if mode(session_path().parent) != 0o700:
        raise AssertionError("каталог сессии не 700")
    if not max_user.connected():
        raise AssertionError("после пароля аккаунт не подключён")
    return done


def ensure_connected():
    if not max_user.connected():
        login_fresh()
    if jarvis.tg_bot and hasattr(jarvis.tg_bot, "set_ctx"):
        jarvis.tg_bot.set_ctx(None)
    jarvis._brief_scope.allow_max = False
    jarvis._brief_scope.peers = None


def fresh_cache():
    BOX["fail"] = ""
    BOX["calls"].clear()
    BOX["lookups"].clear()
    max_user._cache.clear()
    max_user._cool.clear()


def expire_history():
    for key, (ts, val) in list(max_user._cache.items()):
        if str(key).startswith("history:"):
            max_user._cache[key] = (0.0, val)


class MaxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        max_user.set_client_factory(factory)
        login_fresh()

    def setUp(self):
        ensure_connected()
        fresh_cache()

    def test_login_hides_code_and_keeps_one_state_dict(self):
        done = login_fresh()
        self.assertEqual(done["me"]["id"], 7)
        st = max_user.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["me"]["name"], "Миша")
        self.assertNotIn("session", st)
        self.assertNotIn(PHONE[1:], json.dumps(st))
        self.assertTrue(st["phone"].startswith("+7"))
        self.assertNotEqual(st["phone"], PHONE)

    def test_half_session_is_not_connected(self):
        max_user.logout()
        work = session_path().parent
        work.mkdir(parents=True, exist_ok=True)
        session_path().write_bytes(BLOB)
        secret_path().write_text(json.dumps({
            "phone": PHONE, "user_id": 7, "name": "Миша", "stage": "",
            "session": base64.b64encode(BLOB).decode("ascii"),
        }), encoding="utf-8")
        self.assertFalse(max_user.connected())
        self.assertIn("не подключён", max_tools.execute("max_list_dialogs", {})["error"])
        login_fresh()

    def test_dialogs_folder_history_and_search(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dialogs = max_tools.list_dialogs()
            study = max_tools.list_dialogs(folder="Учёба")
            found = max_tools.search_messages("реферат", folder="Учеба")
            missed = max_tools.search_messages("реферат", folder="Работа")
            history = max_tools.read_messages("501", limit=10)
        log = buf.getvalue()
        titles = {d["title"] for d in dialogs["dialogs"]}
        self.assertIn("Лена", titles)
        self.assertEqual([d["title"] for d in study["dialogs"]], ["Семинар"])
        self.assertIn(99, BOX["lookups"])
        self.assertEqual([m["text"] for m in found["messages"]], [STUDY_TEXT])
        self.assertEqual(missed["messages"], [])
        self.assertNotIn(WORK_TEXT, json.dumps(found, ensure_ascii=False))
        dates = [m["date"] for m in history["messages"]]
        self.assertEqual(len(dates), 2)
        self.assertEqual(dates[0], dates[1])
        self.assertTrue(dates[0])
        self.assertTrue(BOX["calls"])
        self.assertTrue(all(c["interactive"] is True and 1 <= c["backward"] <= 200 for c in BOX["calls"]))
        before = len(BOX["calls"])
        max_tools.read_messages("501", limit=5, date_to="2026-11-12")
        self.assertGreater(BOX["calls"][before]["from_time"], 10 ** 12)
        self.assertNotIn(STUDY_TEXT, log)
        self.assertNotIn(WORK_TEXT, log)
        targets = max_tools.study_folder_targets("Учёба")
        self.assertEqual([(t["id"], t["source"]) for t in targets], [(501, "max")])

    def test_rate_limit_keeps_client_other_errors_close_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            first = max_tools.read_messages("Семинар")
        self.assertIn(STUDY_TEXT, json.dumps(first, ensure_ascii=False))
        closed = BOX["closed"]
        BOX["fail"] = "too.many.requests"
        expire_history()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            stale = max_tools.read_messages("Семинар")
        self.assertTrue(stale.get("stale"))
        self.assertIn("ограничил", stale.get("note") or "")
        self.assertIn(STUDY_TEXT, json.dumps(stale, ensure_ascii=False))
        self.assertEqual(BOX["closed"], closed)
        self.assertNotIn(STUDY_TEXT, buf.getvalue())
        BOX["fail"] = "connection reset"
        max_user._cool.clear()
        expire_history()
        with self.assertRaises(max_user.MaxError):
            max_tools.read_messages("Семинар")
        self.assertGreater(BOX["closed"], closed)
        BOX["fail"] = ""
        max_user._cache.clear()
        max_user._cool.clear()
        again = max_tools.read_messages("Семинар")
        self.assertIn(STUDY_TEXT, json.dumps(again, ensure_ascii=False))
        self.assertTrue(BOX["calls"][-1]["interactive"])

    def test_keywords_default_to_telegram(self):
        self.assertNotIn("max_read", tool_groups.pick("что мне написали в чате"))
        self.assertNotIn("max_read", tool_groups.pick("максимальная скидка"))
        self.assertNotIn("max_read", tool_groups.pick("Максим написал в чат"))
        self.assertNotIn("max_read", tool_groups.pick("какие пары завтра"))
        self.assertIn("max_read", tool_groups.pick("найди в максе про реферат"))
        self.assertIn("max_read", tool_groups.pick("что в MAX"))
        self.assertIn("max_read", tool_groups.pick("посмотри учебные чаты"))
        self.assertIn("max_read", tool_groups.pick("", anchor="Учебные чаты · 12 ноября"))
        app = {"telegramSkills": {}}
        quiet = {t["name"] for t in tool_groups.filter_tools(
            jarvis._assistant_tool_list(app, True), tool_groups.pick("что мне написали в чате"))}
        loud = {t["name"] for t in tool_groups.filter_tools(
            jarvis._assistant_tool_list(app, True), tool_groups.pick("найди в максе"))}
        study = {t["name"] for t in tool_groups.filter_tools(
            jarvis._assistant_tool_list(app, True), tool_groups.pick("посмотри учебные чаты"))}
        self.assertNotIn("max_search_messages", quiet)
        self.assertIn("max_search_messages", loud)
        self.assertIn("max_search_messages", study)
        self.assertEqual(tool_groups.unassigned(jarvis._assistant_tool_list(app, True)), [])

    def test_skill_off_removes_tools_and_explicit_max_runs(self):
        app_on = {"telegramSkills": {}, "settings": {"aiPresets": [{"id": "a", "provider": "openai", "apiKey": "k", "model": "m"}],
                                                      "aiAssignments": {"voiceAssistant": "a"}}}
        app_off = dict(app_on, telegramSkills={"max_read": False})
        off_names = {t["name"] for t in jarvis._assistant_tool_list(app_off, True)}
        self.assertNotIn("max_search_messages", off_names)
        self.assertIn("max_read", {s["id"] for s in jarvis.assistant_skills_catalog()})
        groups = tool_groups.pick("найди в максе про реферат")
        chat_groups = tool_groups.pick("что мне написали в чате")

        def openai(app, groups, calls):
            steps = list(calls)

            def step(*args, **kwargs):
                return steps.pop(0)
            with patch.object(jarvis, "_openai_step", side_effect=step) as model:
                answer, _ = jarvis._assistant_loop_openai(
                    [{"role": "user", "content": "x"}], "k", "m", app, "", True, groups=groups)
            names = {t["function"]["name"] for t in model.call_args_list[0].args[4]}
            second = str(model.call_args_list[1].args[3]) if len(model.call_args_list) > 1 else ""
            return answer, names, second

        def claude(app, groups, first_tools):
            payloads = []

            def post(url, json=None, **kw):
                payloads.append(json)
                if len(payloads) == 1 and first_tools:
                    content = [{"type": "tool_use", "id": "m1", "name": "max_search_messages",
                                "input": {"query": "реферат", "folder": "Учеба"}}]
                else:
                    content = [{"type": "text", "text": "Нашёл в Максе" if first_tools else "Только телеграм"}]
                return SimpleNamespace(ok=True, status_code=200, json=lambda: {"content": content, "usage": {}})

            with patch.object(jarvis.requests, "post", side_effect=post):
                answer, _ = jarvis._assistant_loop_claude(
                    [{"role": "user", "content": "x"}], "k", "m", app, "", True, groups=groups)
            names = {t["name"] for t in payloads[0]["tools"]}
            second = json.dumps(payloads[1], ensure_ascii=False) if len(payloads) > 1 else ""
            return answer, names, second

        tool_call = {"content": "", "tool_calls": [{"id": "m1", "type": "function", "function": {
            "name": "max_search_messages", "arguments": json.dumps({"query": "реферат", "folder": "Учеба"})}}],
            "usage": {}, "effort": "", "truncated": False}
        done = {"content": "Нашёл в Максе", "tool_calls": [], "usage": {}, "effort": "", "truncated": False}
        text = {"content": "Только телеграм", "tool_calls": [], "usage": {}, "effort": "", "truncated": False}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            answer, names, second = openai(app_on, groups, [tool_call, done])
            self.assertEqual(answer, "Нашёл в Максе")
            self.assertIn("max_search_messages", names)
            self.assertIn(STUDY_TEXT, second)
            answer, names, second = claude(app_on, groups, True)
            self.assertEqual(answer, "Нашёл в Максе")
            self.assertIn("max_search_messages", names)
            self.assertIn(STUDY_TEXT, second)
            for runner in (lambda: openai(app_off, groups, [text]), lambda: claude(app_off, groups, False),
                           lambda: openai(app_on, chat_groups, [text])):
                answer, names, _ = runner()
                self.assertNotIn("max_search_messages", names)
        self.assertNotIn(STUDY_TEXT, buf.getvalue())

    def test_brief_scope_and_non_owner(self):
        peers = [
            {"id": 501, "title": "Семинар", "source": "max", "folder": "Учеба"},
            {"id": 5, "title": "ТГ семинар", "source": "telegram"},
        ]
        jarvis._brief_scope.allow_max = True
        jarvis._brief_scope.peers = peers
        try:
            blocked = jarvis.execute_assistant_tool("max_search_messages", {"query": "реферат"}, {}, "", True)
            self.assertIn("Учеба", blocked["error"])
            other = jarvis.execute_assistant_tool("max_search_messages", {"query": "реферат", "peer": "502"}, {}, "", True)
            self.assertIn("Учеба", other["error"])
            allowed = jarvis.execute_assistant_tool(
                "max_search_messages", {"query": "реферат", "folder": "Учёба"}, {}, "", True)
            self.assertEqual([m["text"] for m in allowed["messages"]], [STUDY_TEXT])
            read = jarvis.execute_assistant_tool("max_read_messages", {"peer": "501"}, {}, "", True)
            self.assertEqual(read["chat"], "Семинар")
            with patch.object(jarvis, "TG_TOOL_NAMES", {"tg_search_messages"}):
                tg = jarvis.execute_assistant_tool("tg_search_messages", {"peer": "501", "query": "реферат"}, {}, "", True)
            self.assertIn("учебн", tg["error"])
        finally:
            jarvis._brief_scope.allow_max = False
            jarvis._brief_scope.peers = None
        if jarvis.tg_bot and jarvis.tg_user:
            jarvis.tg_bot.set_ctx(jarvis.tg_bot.Ctx("TOKEN", 999))
            self.addCleanup(jarvis.tg_bot.set_ctx, None)
            with patch.object(jarvis.tg_user, "is_owner_chat", return_value=False):
                denied = jarvis.execute_assistant_tool("max_list_folders", {}, {}, "", False)
            self.assertIn("недоступен", denied["error"])

    def test_study_scan_reads_max_folder_with_telegram(self):
        app = {
            "telegramSkills": {},
            "studyChats": {"enabled": True, "folder": "", "chats": [{"id": 5, "title": "ТГ семинар"}],
                           "maxFolder": "Учеба", "notifyEmpty": False},
            "studyTasks": [],
            "knowledgeNotes": [],
            "settings": {},
        }
        prompts = []

        def llm(*args, **kwargs):
            prompts.append(args[2] if len(args) > 2 else kwargs.get("user"))
            return json.dumps({"items": [
                {"chat": "Семинар (MAX)", "message_id": 77, "who": "Лена", "summary": "реферат", "why": "срок", "urgency": "high"},
                {"chat": "ТГ семинар", "message_id": 9, "who": "Анна", "summary": "задание", "why": "вопрос", "urgency": "normal"},
            ]}, ensure_ascii=False)

        def scan_chat(peer, since):
            return {"chat": "ТГ семинар", "chat_id": peer, "me": {"name": "Миша", "username": ""},
                    "messages": [{"id": 9, "date": "12:00", "from": "Анна", "text": "телеграм-задание"}]}

        anchors = []
        old_user, old_tools, old_bot = jarvis.tg_user, jarvis.tg_tools, jarvis.tg_bot
        jarvis.tg_user = SimpleNamespace(is_owner_chat=lambda cid: int(cid) == 42, _load_cfg=lambda: {})
        jarvis.tg_tools = SimpleNamespace(scan_chat=scan_chat, list_dialogs=lambda **k: {"dialogs": []})
        jarvis.tg_bot = SimpleNamespace(
            send_text=lambda token, cid, text: [1],
            remember_anchor=lambda cid, ids, title, user_text, asst_text: anchors.append(asst_text),
            seed_dialog=lambda *a, **k: None,
        )
        buf = io.StringIO()
        try:
            with patch.object(jarvis, "load_app_data", return_value=app), \
                    patch.object(jarvis, "get_token", return_value="BOT"), \
                    patch.object(jarvis, "_ai_conf", side_effect=lambda *a, **k: ("openai", "k", "m", "")), \
                    patch.object(jarvis, "_llm_text", side_effect=llm), \
                    contextlib.redirect_stdout(buf):
                result = jarvis.study_chats_scan(42, manual=True)
        finally:
            jarvis.tg_user, jarvis.tg_tools, jarvis.tg_bot = old_user, old_tools, old_bot
        self.assertTrue(result.get("ok"), result)
        self.assertGreaterEqual(result["messages"], 2)
        self.assertEqual(result["sent"], 1)
        self.assertTrue(prompts)
        self.assertIn(STUDY_TEXT, prompts[0])
        self.assertIn("(MAX)", prompts[0])
        self.assertIn("телеграм-задание", prompts[0])
        self.assertNotIn(WORK_TEXT, prompts[0])
        self.assertTrue(anchors and "max:501" in anchors[0])
        self.assertNotIn(STUDY_TEXT, buf.getvalue())
        empty = {"studyChats": {"maxFolder": ""}, "knowledgeNotes": [], "telegramSkills": {}}
        targets, _ = jarvis._study_targets(empty, None, False)
        self.assertFalse(any(t.get("source") == "max" for t in targets))
        targets, _ = jarvis._study_targets({"studyChats": {}, "knowledgeNotes": [], "telegramSkills": {}}, None, False)
        self.assertIn(("max", "Семинар"), {(t["source"], t["title"]) for t in targets})
        self.assertNotIn("Работа", {t["title"] for t in targets})

    def test_backup_and_http_do_not_leak_the_session(self):
        code = Path(_TMP.name) / "code-root"
        code.mkdir(exist_ok=True)
        (code / "hello.py").write_text("print(1)\n", encoding="utf-8")
        (code / "jarvis_max_user.json").write_text('{"session":"SECRET-BLOB"}', encoding="utf-8")
        nested = code / "jarvis_max"
        nested.mkdir(exist_ok=True)
        (nested / "session.db").write_bytes(b"SECRET-BLOB")
        with patch.object(jarvis, "DIR", code), patch.object(jarvis, "DATA_DIR", code):
            blob = jarvis.build_backup_zip()
        z = zipfile.ZipFile(io.BytesIO(blob))
        self.assertIn("code/hello.py", z.namelist())
        self.assertFalse(any("max" in n or "session" in n for n in z.namelist()))
        self.assertNotIn(b"SECRET-BLOB", blob)

        self.assertIn("…", max_user.redact("Bearer supersecretvalue1234567890"))
        self.assertNotIn("supersecretvalue", max_user.redact("Bearer supersecretvalue1234567890"))

        root = Path(os.environ["DATA_DIR"])
        marker = secret_path().read_text(encoding="utf-8")
        self.assertIn("session", marker)
        old = os.getcwd()
        os.chdir(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        from http.client import HTTPConnection
        client = HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(client.close)

        def call(method, path, body=None, cookie=False):
            headers = {}
            if cookie:
                headers["Cookie"] = jarvis.AUTH_COOKIE_NAME + "=" + jarvis.AUTH_COOKIE_VALUE
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            else:
                data = None
            client.request(method, path, data, headers)
            response = client.getresponse()
            return response.status, response.read()

        try:
            status, body = call("GET", "/jarvis_max_user.json", cookie=True)
            self.assertEqual(status, 404)
            self.assertNotIn(b"full-session", body)
            self.assertNotIn(marker.encode()[:40], body)
            status, body = call("GET", "/jarvis_max/session.db", cookie=True)
            self.assertEqual(status, 404)
            self.assertNotIn(BLOB, body)
            status, body = call("GET", "/jarvis_max%2Fsession.db", cookie=True)
            self.assertEqual(status, 404)
            self.assertNotIn(BLOB, body)
            status, body = call("GET", "/api/max/status")
            self.assertEqual(status, 401)
            status, body = call("GET", "/api/max/status", cookie=True)
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertTrue(payload["connected"])
            self.assertNotIn("session", payload)
            self.assertNotIn("token", payload)
            self.assertNotIn(PHONE[1:], body.decode())
            status, body = call("POST", "/api/max/tool", {"name": "max_search_messages", "input": {"query": "реферат", "folder": "Учеба"}}, cookie=True)
            self.assertEqual(status, 200)
            found = json.loads(body)
            self.assertEqual([m["text"] for m in found["messages"]], [STUDY_TEXT])
            self.assertNotIn(b"full-session", body)
            self.assertNotIn(PASSWORD.encode(), body)
        finally:
            os.chdir(old)

    def test_ui_and_ignore_list_the_secret(self):
        root = Path(__file__).parent
        html = (root / "index (9).html").read_text(encoding="utf-8")
        self.assertIn('data-testid="settings-tab-max"', html)
        self.assertIn("function MaxSettings", html)
        self.assertIn("loadMaxTools", html)
        self.assertIn("maxFolder", html)
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("jarvis_max_user.json", ignore)
        self.assertIn("jarvis_max/", ignore)
        self.assertIn("maxapi-python", (root / "requirements.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
