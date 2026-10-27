"""Временные пуши из личного Telegram: подмена Telethon и Web Push, без живого Telegram и Apple."""

import contextlib
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path

import jarvis
import tg_watch_skill
import webpush

AUTH = "SuperAuthSecret99"
ENDPOINT = "https://push.example/send/EndpointSecret99"
FULL = "Привет, это полное сообщение без сокращений и без обрезки."


def _dialogs():
    return [
        {"id": 1, "title": "Светлана", "username": "sveta", "type": "user"},
        {"id": 2, "title": "Светлана, менеджер по OZON", "username": "ozon_sveta", "type": "user"},
        {"id": 3, "title": "Склад OZON", "username": "", "type": "group"},
    ]


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self._search = tg_watch_skill.live_search
        self._latest = tg_watch_skill.live_latest_id
        self._deliver = webpush._deliver_impl
        webpush._logs.clear()
        tg_watch_skill._logs.clear()
        self.latest = {1: 10, 2: 20, 3: 30}
        tg_watch_skill.live_latest_id = lambda peer: self.latest.get(int(peer), 0)

    def tearDown(self):
        tg_watch_skill.live_search = self._search
        tg_watch_skill.live_latest_id = self._latest
        webpush.set_deliver(self._deliver)
        if self._old_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_dir
        self.tmp.cleanup()

    def _search_as(self, dialogs):
        tg_watch_skill.live_search = lambda query: {"dialogs": dialogs, "matched": len(dialogs)}

    def _sub(self, endpoint=ENDPOINT, auth=AUTH):
        return webpush.save_subscription({
            "endpoint": endpoint,
            "keys": {"p256dh": "B" + "A" * 80, "auth": auth},
        })

    def test_pick_exact_partial_and_ambiguous(self):
        rows = _dialogs()
        self.assertEqual(tg_watch_skill.pick_dialog("Светлана, менеджер по OZON", rows)["dialog"]["id"], 2)
        self.assertEqual(tg_watch_skill.pick_dialog("@ozon_sveta", rows)["dialog"]["id"], 2)
        sklad = tg_watch_skill.pick_dialog("Склад", rows)
        self.assertFalse(sklad["ok"])
        self.assertTrue(sklad["ambiguous"])
        self.assertEqual([d["id"] for d in sklad["matches"]], [3])
        self.assertEqual(tg_watch_skill.pick_dialog("Склад", rows, allow_partial=True)["dialog"]["id"], 3)
        amb = tg_watch_skill.pick_dialog("Свет", rows)
        self.assertTrue(amb["ambiguous"])
        self.assertGreater(len(amb["matches"]), 1)
        sveta = tg_watch_skill.pick_dialog("Света", rows)
        self.assertTrue(sveta["ambiguous"])
        self.assertGreater(len(sveta["matches"]), 1)
        same = [
            {"id": 8, "title": "Анна", "username": "", "type": "user"},
            {"id": 9, "title": "Анна", "username": "anna2", "type": "user"},
        ]
        self.assertTrue(tg_watch_skill.pick_dialog("Анна", same)["ambiguous"])
        self.assertFalse(tg_watch_skill.pick_dialog("нет такого", rows)["ok"])

    def test_start_requires_subscription_and_does_not_guess(self):
        self._search_as(_dialogs())
        blocked = tg_watch_skill.start("Светлана, менеджер по OZON", now=1_000)
        self.assertFalse(blocked["ok"])
        self.assertFalse(blocked["deliverable"])
        self.assertIn("некуда доставить", blocked["message"])
        self.assertEqual(tg_watch_skill._load(), {})

        self._sub()
        amb = tg_watch_skill.start("Свет", now=1_000)
        self.assertTrue(amb["ambiguous"])
        self.assertFalse(amb["ok"])
        self.assertEqual(tg_watch_skill._load(), {})
        self.assertIn("Светлана, менеджер по OZON", amb["message"])

    def test_start_default_half_hour_and_named_span(self):
        self._sub()
        self._search_as(_dialogs())
        now = 1_700_000_000
        res = tg_watch_skill.start("Светлана, менеджер по OZON", now=now)
        self.assertTrue(res["ok"])
        self.assertEqual(res["minutes"], 30)
        self.assertIn("не менял", res["message"])
        watch = tg_watch_skill._load()["2"]
        self.assertEqual(watch["until"], now + 30 * 60)
        self.assertEqual(watch["last_id"], 20)
        self.assertEqual(watch["title"], "Светлана, менеджер по OZON")

        again = tg_watch_skill.start("Склад OZON", 45, now=now)
        self.assertTrue(again["ok"])
        self.assertEqual(len(tg_watch_skill._load()), 2)
        self.assertEqual(tg_watch_skill._load()["3"]["minutes"], 45)
        bad = tg_watch_skill.start("Склад OZON", 0, now=now)
        self.assertFalse(bad["ok"])

    def test_blurry_name_asks_then_alias_enables_by_id(self):
        self._sub()
        calls = {"n": 0}

        def search(query):
            calls["n"] += 1
            return {"dialogs": _dialogs(), "matched": len(_dialogs())}

        tg_watch_skill.live_search = search
        now = 1_700_000_000
        vague = tg_watch_skill.start("менеджер", now=now)
        self.assertTrue(vague["ask"])
        self.assertFalse(vague["ok"])
        self.assertIn("— Светлана, менеджер по OZON — личный", vague["message"])
        self.assertNotIn("Склад", vague["message"])
        self.assertEqual(tg_watch_skill._load(), {})
        self.assertEqual(tg_watch_skill.alias_list()["aliases"], [])

        one = tg_watch_skill.start("Склад", now=now)
        self.assertTrue(one["ask"])
        self.assertIn("— Склад OZON — группа", one["message"])
        self.assertEqual(tg_watch_skill._load(), {})

        missing = tg_watch_skill.start("нет такого человека", now=now)
        self.assertTrue(missing["ask"])
        self.assertEqual(missing["matches"], [])
        self.assertEqual(tg_watch_skill._load(), {})

        channel = tg_watch_skill._ask("новости", [{"id": 5, "title": "Новости", "type": "channel"}])
        self.assertIn("— Новости — канал", channel["message"])

        asked = tg_watch_skill.start("Света", now=now)
        self.assertTrue(asked["ask"])
        self.assertFalse(asked["ok"])
        self.assertGreaterEqual(len(asked["matches"]), 2)
        self.assertTrue(all(m["type_label"] == "личный" for m in asked["matches"]))
        self.assertEqual(tg_watch_skill._load(), {})
        self.assertEqual(tg_watch_skill.alias_list()["aliases"], [])

        saved = tg_watch_skill.start("Света", peer_id=2, now=now)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["peer_id"], 2)
        self.assertEqual(saved["alias"], "Света")
        self.assertEqual(tg_watch_skill._load()["2"]["peer_id"], 2)
        self.assertEqual(tg_watch_skill.alias_list()["aliases"][0]["peer_id"], 2)

        tg_watch_skill.live_search = lambda query: (_ for _ in ()).throw(AssertionError("поиск по имени"))
        again = tg_watch_skill.start("Света", now=now + 10)
        self.assertTrue(again["ok"])
        self.assertEqual(again["peer_id"], 2)

        tg_watch_skill.live_search = search
        renamed = tg_watch_skill.alias_rename("Света", "менеджер OZON")
        self.assertTrue(renamed["ok"])
        calls["n"] = 0
        by_new = tg_watch_skill.start("менеджер OZON", now=now + 20)
        self.assertTrue(by_new["ok"])
        self.assertEqual(calls["n"], 0)
        self.assertEqual(by_new["peer_id"], 2)

        deleted = tg_watch_skill.execute(
            "tg_watch_alias_delete", {"alias": "менеджер OZON"}, {tg_watch_skill.SKILL_ID: True})
        self.assertTrue(deleted["ok"])
        self.assertEqual(tg_watch_skill.alias_list()["aliases"], [])
        calls["n"] = 0
        after = tg_watch_skill.start("Света", now=now + 30)
        self.assertTrue(after["ask"])
        self.assertFalse(after["ok"])
        self.assertGreater(calls["n"], 0)

        named = tg_watch_skill.alias_save("Светлана")
        self.assertTrue(named["ok"])
        self.assertEqual(named["alias"]["peer_id"], 1)
        self.assertIn("не включал", named["message"])
        self.assertNotIn("1", tg_watch_skill._load())
        blurry_save = tg_watch_skill.alias_save("Света")
        self.assertTrue(blurry_save["ask"])
        self.assertFalse(any(a.get("alias") == "Света" for a in tg_watch_skill.alias_list()["aliases"]))

    def test_stop_and_list(self):
        self._sub()
        self._search_as(_dialogs())
        now = 1_700_000_000
        tg_watch_skill.start("Светлана, менеджер по OZON", 30, now=now)
        tg_watch_skill.start("Склад OZON", 10, now=now)
        listed = tg_watch_skill.list_watches(now=now + 60)
        titles = {w["title"] for w in listed["watches"]}
        self.assertEqual(titles, {"Светлана, менеджер по OZON", "Склад OZON"})
        both = tg_watch_skill.stop("", now=now + 60)
        self.assertTrue(both["ambiguous"])
        self.assertEqual(len(tg_watch_skill._load()), 2)
        one = tg_watch_skill.stop("Склад", now=now + 60)
        self.assertTrue(one["ok"])
        self.assertEqual(list(tg_watch_skill._load()), ["2"])
        tg_watch_skill.stop("Светлана, менеджер по OZON", now=now + 60)
        self.assertEqual(tg_watch_skill._load(), {})

    def test_window_expires_and_own_message_is_silent(self):
        self._sub()
        self._search_as(_dialogs())
        now = 5_000
        tg_watch_skill.start("Светлана", 30, now=now)
        calls = []

        def fetch(peer, after):
            calls.append(peer)
            return [
                {"id": after + 1, "out": True, "text": "моё " + FULL, "media": "", "sender": "я"},
                {"id": after + 2, "out": False, "text": FULL, "media": "", "sender": "Светлана"},
            ]

        sent = []
        tg_watch_skill.poll(now=now + 10, enabled=True, fetch_new=fetch, send=lambda title, body, tag="": sent.append((title, body, tag)) or {"ok": True})
        self.assertEqual(calls, [1])
        self.assertEqual(sent, [("Светлана", FULL, "tg-1-12")])
        self.assertEqual(tg_watch_skill._load()["1"]["last_id"], 12)

        sent.clear()
        calls.clear()
        expired = tg_watch_skill.poll(now=now + 30 * 60, enabled=True, fetch_new=fetch, send=lambda title, body, tag="": sent.append(body) or {"ok": True})
        self.assertEqual(calls, [])
        self.assertEqual(sent, [])
        self.assertIn("Светлана", expired["expired"])
        self.assertEqual(tg_watch_skill._load(), {})

    def test_incoming_text_media_and_group_sender(self):
        self.assertIsNone(tg_watch_skill.format_push("Чат", {"out": True, "text": FULL}))
        text = tg_watch_skill.format_push("Светлана", {"out": False, "text": FULL, "media": ""})
        self.assertEqual(text["title"], "Светлана")
        self.assertEqual(text["body"], FULL)
        photo = tg_watch_skill.format_push("Светлана", {"out": False, "text": "", "media": "photo"})
        self.assertEqual(photo["body"], "Фото")
        voice = tg_watch_skill.format_push("Светлана", {"out": False, "text": "перезвони", "media": "voice"})
        self.assertEqual(voice["body"], "Голосовое — перезвони")
        doc = tg_watch_skill.format_push("Склад", {"out": False, "text": "", "media": "file", "file_name": "счёт.pdf"}, "group")
        self.assertEqual(doc["body"], "Файл: счёт.pdf")
        group = tg_watch_skill.format_push("Склад", {"out": False, "text": FULL, "media": "", "sender": "Ира"}, "group")
        self.assertEqual(group["body"], "Ира: " + FULL)

    def test_two_dialogs_only_the_matching_one_pushes(self):
        self._sub()
        self._search_as(_dialogs())
        now = 9_000
        tg_watch_skill.start("Светлана", 30, now=now)
        tg_watch_skill.start("Склад OZON", 30, now=now)
        sent = []

        def fetch(peer, after):
            if int(peer) == 3:
                return [{"id": after + 1, "out": False, "text": "на складе", "media": "", "sender": "Ира"}]
            return [{"id": after + 1, "out": False, "text": FULL, "media": "", "sender": "Светлана"}]

        tg_watch_skill.poll(
            now=now + 5, enabled=True, fetch_new=fetch,
            send=lambda title, body, tag="": sent.append((title, body)) or {"ok": True},
        )
        self.assertEqual(sorted(sent), sorted([("Светлана", FULL), ("Склад OZON", "Ира: на складе")]))

    def test_disabled_skill_does_not_poll_or_execute(self):
        self._sub()
        self._search_as(_dialogs())
        now = 8_000
        tg_watch_skill.start("Светлана", 30, now=now)
        calls = []
        tg_watch_skill.poll(now=now + 1, enabled=False, fetch_new=lambda peer, after: calls.append(peer) or [], send=lambda *a, **k: {"ok": True})
        self.assertEqual(calls, [])
        tg_watch_skill.live_search = lambda query: (_ for _ in ()).throw(AssertionError("search"))
        off = tg_watch_skill.execute("tg_watch_start", {"name": "Светлана"}, {tg_watch_skill.SKILL_ID: False})
        self.assertIn("выключен", off["error"])

    def test_secrets_stay_out_of_logs(self):
        saved = self._sub()
        self.assertTrue(saved["ok"])
        self.assertNotIn(AUTH, json.dumps(saved))
        self.assertNotIn(ENDPOINT, json.dumps(saved))
        status = webpush.public_status()
        blob = json.dumps(status)
        self.assertNotIn(AUTH, blob)
        self.assertNotIn(ENDPOINT, blob)
        self.assertNotIn("BEGIN", blob)
        private = webpush._load()["private_key"]
        public = status["publicKey"]
        self.assertTrue(private)
        self.assertNotIn(private, blob)
        mode = webpush.store_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

        def boom(sub, data, pem):
            raise RuntimeError(pem + " " + sub["auth"] + " " + sub["endpoint"] + " " + public)

        webpush.set_deliver(boom)
        buf = io.StringIO()
        handler = logging.Handler()
        handler.emit = lambda record: buf.write(record.getMessage() + "\n")
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with contextlib.redirect_stdout(buf):
                result = webpush.send_text("Светлана", FULL)
        finally:
            root.removeHandler(handler)
        self.assertFalse(result["ok"])
        leaked = buf.getvalue() + "\n" + "\n".join(webpush._logs) + "\n" + "\n".join(tg_watch_skill._logs)
        for secret in (private, public, AUTH, ENDPOINT, "BEGIN PRIVATE"):
            self.assertNotIn(secret, leaked)
        raw = webpush.store_path().read_text(encoding="utf-8")
        self.assertIn(private, raw)

    def test_short_text_is_delivered_whole_and_library_does_not_log_the_key(self):
        self._sub(endpoint="https://push.example/send/other-token", auth="auth-other-token")
        seen = {}

        def grab(sub, data, pem):
            seen["data"] = data
            seen["pem"] = pem

        webpush.set_deliver(grab)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = webpush.send_text("Светлана", FULL, tag="tg-1-3")
        self.assertTrue(res["ok"])
        payload = json.loads(seen["data"])
        self.assertEqual(payload["title"], "Светлана")
        self.assertEqual(payload["body"], FULL)
        self.assertNotIn(seen["pem"], buf.getvalue())
        self.assertNotIn(seen["pem"], "\n".join(webpush._logs))

        huge = "Я" * 8000
        webpush.send_text("Светлана", huge)
        packed = json.loads(seen["data"])
        self.assertLessEqual(len(seen["data"].encode()), 2800)
        self.assertTrue(packed["body"].startswith("Я"))
        self.assertNotEqual(packed["body"], huge)

    def test_real_push_library_reaches_local_endpoint_without_leaking_the_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from http.server import BaseHTTPRequestHandler, HTTPServer

        got = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got["body"] = self.rfile.read(n)
                got["authorization"] = self.headers.get("Authorization") or ""
                got["crypto"] = self.headers.get("Crypto-Key") or ""
                self.send_response(201)
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        key = ec.generate_private_key(ec.SECP256R1())
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        p256dh = webpush._b64(raw)
        auth = webpush._b64(os.urandom(16))
        endpoint = f"http://127.0.0.1:{port}/push"
        webpush.set_deliver(None)
        saved = webpush.save_subscription({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}})
        self.assertTrue(saved["ok"])
        private = webpush._load()["private_key"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = webpush.send_text("Светлана", FULL)
        srv.shutdown()
        self.assertTrue(res["ok"], res)
        self.assertTrue(got.get("body"))
        self.assertIn("vapid", got["authorization"].lower())
        joined = buf.getvalue() + got["authorization"] + got["crypto"] + got["body"].decode("latin1", "replace")
        self.assertNotIn(private, joined)
        self.assertNotIn(auth, joined)
        self.assertNotIn(private, "\n".join(webpush._logs))


class AssistantLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_dir
        self.tmp.cleanup()

    def _run(self, provider, skills, with_tg=True):
        captured = {}

        class Resp:
            ok = True
            status_code = 200

            def __init__(self, url):
                self.url = url

            def json(self):
                if "anthropic" in self.url:
                    return {"content": [{"type": "text", "text": "готово"}], "stop_reason": "end_turn"}
                return {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return Resp(url)

        orig = jarvis.requests.post
        jarvis.requests.post = fake_post
        app = {"telegramSkills": skills, "knowledgeNotes": [], "settings": {}}
        history = [{"role": "user", "content": "включи уведомления"}]
        try:
            if provider == "claude":
                text, _links = jarvis._assistant_loop_claude(history, "k", "m", app, "http://localhost", with_tg)
            else:
                text, _links = jarvis._assistant_loop_openai(history, "k", "m", app, "http://localhost", with_tg)
        finally:
            jarvis.requests.post = orig
        self.assertEqual(text, "готово")
        body = captured["json"]
        if provider == "claude":
            names = [t["name"] for t in body["tools"]]
            system = body["system"]
        else:
            names = [t["function"]["name"] for t in body["tools"]]
            system = body["messages"][0]["content"]
        return names, system

    def test_tools_follow_skill_switch(self):
        on_names, on_system = self._run("claude", {tg_watch_skill.SKILL_ID: True})
        off_names, off_system = self._run("claude", {tg_watch_skill.SKILL_ID: False})
        for name in tg_watch_skill.TOOL_NAMES:
            self.assertIn(name, on_names)
            self.assertNotIn(name, off_names)
        self.assertIn("ВРЕМЕННЫЕ ПУШИ", on_system)
        self.assertNotIn("ВРЕМЕННЫЕ ПУШИ", off_system)
        self.assertIn("некуда доставить", on_system)

        on_o, on_sys = self._run("openai", {tg_watch_skill.SKILL_ID: True})
        off_o, off_sys = self._run("openai", {tg_watch_skill.SKILL_ID: False})
        self.assertIn("tg_watch_start", on_o)
        self.assertNotIn("tg_watch_stop", off_o)
        self.assertIn("ВРЕМЕННЫЕ ПУШИ", on_sys)
        self.assertNotIn("ВРЕМЕННЫЕ ПУШИ", off_sys)

        stranger, stranger_system = self._run("claude", {tg_watch_skill.SKILL_ID: True}, with_tg=False)
        self.assertNotIn("tg_watch_start", stranger)
        self.assertNotIn("ВРЕМЕННЫЕ ПУШИ", stranger_system)
        blocked = jarvis.execute_assistant_tool(
            "tg_watch_list", {}, {"telegramSkills": {tg_watch_skill.SKILL_ID: True}}, "", False)
        self.assertIn("недоступен", blocked["error"])

        default_names, default_system = self._run("claude", {})
        self.assertIn("tg_watch_list", default_names)
        self.assertIn("ВРЕМЕННЫЕ ПУШИ", default_system)

        voice_on = tg_watch_skill.voice_payload({tg_watch_skill.SKILL_ID: True})
        voice_off = tg_watch_skill.voice_payload({tg_watch_skill.SKILL_ID: False})
        self.assertEqual([t["name"] for t in voice_on["tools"]], [t["name"] for t in tg_watch_skill.TOOLS])
        self.assertEqual(voice_off, {"tools": [], "prompt": ""})

        gone = jarvis.execute_assistant_tool(
            "tg_watch_start", {"name": "Светлана"},
            {"telegramSkills": {tg_watch_skill.SKILL_ID: False}}, "", True)
        self.assertIn("выключен", gone["error"])

    def test_catalog_backup_and_wire(self):
        skill = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == tg_watch_skill.SKILL_ID)
        self.assertEqual(skill["risk"], "read")
        self.assertEqual(skill["title"], "Временные пуши Telegram")
        self.assertEqual(set(skill["tools"]), tg_watch_skill.TOOL_NAMES)
        self.assertIn("Джарвис", skill["desc"])
        self.assertIn("Домой", skill["desc"])
        self.assertIn("не дублируются", skill["desc"])
        self.assertIn("псевдоним", skill["desc"])
        self.assertIn("личный", skill["desc"])

        src = Path("jarvis.py").read_text(encoding="utf-8")
        self.assertIn('"jarvis_webpush.json"', src)
        self.assertIn('"jarvis_tg_watch.json"', src)
        ignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("jarvis_webpush.json", ignore)
        self.assertIn("jarvis_tg_watch.json", ignore)
        self.assertIn("pywebpush", Path("requirements.txt").read_text(encoding="utf-8"))

        html = Path("index (9).html").read_text(encoding="utf-8")
        self.assertIn('data-testid="push-settings"', html)
        self.assertIn('data-testid="push-status"', html)
        self.assertIn('data-testid="push-enable"', html)
        self.assertEqual(html.count("await loadWatchTools()"), 2)
        self.assertIn("...WATCH_AGENT_TOOLS", html)
        self.assertIn("name.startsWith('tg_watch_')", html)
        self.assertIn("/api/push/tool", html)
        self.assertIn("jarvisPushEnable", html)
        self.assertLess(html.index("tg_watch_"), html.index("name.startsWith('tg_')"))
        watch_src = Path("tg_watch_skill.py").read_text(encoding="utf-8")
        self.assertNotIn("UpdateNotifySettings", watch_src)
        sw = Path("sw.js").read_text(encoding="utf-8")
        self.assertIn("addEventListener('push'", sw)
        self.assertNotIn("addEventListener('fetch'", sw)


class PushHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = os.environ.get("DATA_DIR")
        self._app = jarvis.APP_DATA_FILE
        os.environ["DATA_DIR"] = self.tmp.name
        jarvis.APP_DATA_FILE = Path(self.tmp.name) / "jarvis_app_data.json"
        self._search = tg_watch_skill.live_search
        self._latest = tg_watch_skill.live_latest_id
        tg_watch_skill.live_search = lambda query: {"dialogs": _dialogs(), "matched": len(_dialogs())}
        tg_watch_skill.live_latest_id = lambda peer: 4
        webpush._logs.clear()
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        tg_watch_skill.live_search = self._search
        tg_watch_skill.live_latest_id = self._latest
        jarvis.APP_DATA_FILE = self._app
        if self._old_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_dir
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=True):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type") or ""
            return resp.status, ctype, raw

    def test_status_subscribe_sw_and_tool(self):
        status_code, ctype, raw = self._req("GET", "/api/push/status")
        self.assertEqual(status_code, 200)
        status = json.loads(raw)
        self.assertTrue(status["publicKey"])
        self.assertEqual(status["subscriptions"], 0)
        private = webpush._load()["private_key"]
        self.assertNotIn(private, raw.decode())
        self.assertNotIn(private, "\n".join(webpush._logs))

        code, sw_type, sw = self._req("GET", "/sw.js", cookie=False)
        self.assertEqual(code, 200)
        self.assertIn("javascript", sw_type)
        self.assertIn(b"addEventListener('push'", sw)
        self.assertNotIn(b"addEventListener('fetch'", sw)

        saved_code, _, saved_raw = self._req("POST", "/api/push/subscribe", {
            "endpoint": ENDPOINT, "keys": {"p256dh": "B" + "C" * 80, "auth": AUTH},
        })
        self.assertEqual(saved_code, 200)
        saved = json.loads(saved_raw)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["subscriptions"], 1)
        self.assertNotIn(AUTH, saved_raw.decode())
        self.assertNotIn(private, saved_raw.decode())
        self.assertNotIn(AUTH, "\n".join(webpush._logs))

        tools = json.loads(self._req("GET", "/api/push/tools")[2])
        self.assertIn("tg_watch_start", [t["name"] for t in tools["tools"]])
        started = json.loads(self._req("POST", "/api/push/tool", {
            "name": "tg_watch_start", "input": {"name": "Светлана, менеджер по OZON", "minutes": 15},
        })[2])
        self.assertTrue(started["ok"])
        self.assertEqual(started["minutes"], 15)
        self.assertEqual(tg_watch_skill._load()["2"]["title"], "Светлана, менеджер по OZON")

        jarvis.APP_DATA_FILE.write_text(
            json.dumps({"telegramSkills": {tg_watch_skill.SKILL_ID: False}}), encoding="utf-8")
        off_tools = json.loads(self._req("GET", "/api/push/tools")[2])
        self.assertEqual(off_tools["tools"], [])
        self.assertEqual(off_tools["prompt"], "")
        off = json.loads(self._req("POST", "/api/push/tool", {
            "name": "tg_watch_start", "input": {"name": "Светлана"},
        })[2])
        self.assertIn("выключен", off["error"])
        skills = json.loads(self._req("GET", "/api/tg/skills")[2])["skills"]
        self.assertIn(tg_watch_skill.SKILL_ID, [s["id"] for s in skills])


if __name__ == "__main__":
    unittest.main()
