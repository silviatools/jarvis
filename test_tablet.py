"""Планшетный голосовой мост: подмена ИИ, без настоящих Telegram/модели."""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name
if "pyaes" not in sys.modules:
    try:
        import pyaes  # noqa: F401
    except ImportError:
        sys.modules["pyaes"] = types.ModuleType("pyaes")

import jarvis  # noqa: E402


class R:
    def __init__(self, j):
        self.ok, self._j, self.status_code = True, j, 200

    def json(self):
        return self._j


class TabletUnitTest(unittest.TestCase):
    def setUp(self):
        p = jarvis.tablet_tokens_path()
        if p.exists():
            p.unlink()
        jarvis._tablet_history.clear()

    def test_sanitize_strips_pin_puk_password_and_keys(self):
        raw = "PIN 1234, PUK 11112222, пароль secret1, api_key sk-ant-abcdefghijklmnop, sk-abcdefghijklmnopqrst"
        out = jarvis.tablet_sanitize(raw)
        self.assertNotIn("1234", out)
        self.assertNotIn("11112222", out)
        self.assertNotIn("secret1", out)
        self.assertNotIn("sk-ant-abcdefghijklmnop", out)
        self.assertNotIn("sk-abcdefghijklmnopqrst", out)
        self.assertIn("***", out)

    def test_sanitize_strips_html_markup(self):
        raw = "Да! Напишите, пожалуйста, <b>адрес или станцию метро</b> либо отправьте геопозицию."
        out = jarvis.tablet_sanitize(raw)
        self.assertNotIn("<b>", out)
        self.assertNotIn("</b>", out)
        self.assertIn("адрес или станцию метро", out)

    def test_login_and_token(self):
        self.assertFalse(jarvis.tablet_login("nope")["ok"])
        ok = jarvis.tablet_login(jarvis.AUTH_PASSWORD)
        self.assertTrue(ok["ok"])
        self.assertTrue(jarvis.tablet_token_ok(ok["token"]))
        self.assertFalse(jarvis.tablet_token_ok("x" * 40))
        self.assertEqual(ok["cookie"], jarvis.AUTH_COOKIE_VALUE)

    def test_voice_reply_mode_and_extra(self):
        self.assertEqual(jarvis.voice_reply_mode({}, "привет"), "short")
        self.assertIn("1–2 короткие", jarvis.voice_reply_extra({}, "привет"))
        self.assertEqual(jarvis.voice_reply_mode({}, "расскажи про погоду"), "detailed")
        self.assertEqual(jarvis.voice_reply_mode({}, "почему так"), "detailed")
        self.assertEqual(jarvis.voice_reply_mode({}, "сделай по шагам"), "detailed")
        self.assertEqual(jarvis.voice_reply_mode({"settings": {"voiceReply": "normal"}}, "привет"), "normal")
        self.assertEqual(jarvis.voice_reply_extra({"settings": {"voiceReply": "normal"}}, "привет"), "")
        self.assertIn("просит подробно", jarvis.voice_reply_extra({}, "подробно что в бюджете"))

    def test_turn_no_key_and_empty(self):
        jarvis.load_app_data = lambda: {"settings": {"aiPresets": []}}
        self.assertEqual(jarvis.handle_tablet_turn("", "s")["error"], "empty")
        r = jarvis.handle_tablet_turn("привет", "s")
        self.assertEqual(r["error"], "no_key")
        self.assertIn("ключей", r["text"])


class TabletAssistantTest(unittest.TestCase):
    def setUp(self):
        p = jarvis.tablet_tokens_path()
        if p.exists():
            p.unlink()
        jarvis._tablet_history.clear()
        self.app = {"telegramSkills": {}, "settings": {
            "aiPresets": [{"id": "a", "name": "n", "provider": "claude", "apiKey": "k", "model": ""}],
            "aiAssignments": {"voiceAssistant": "a"},
            "publicUrl": "https://jarvis.example",
        }}
        self._load, self._post = jarvis.load_app_data, jarvis.requests.post
        jarvis.load_app_data = lambda: self.app

    def tearDown(self):
        jarvis.load_app_data, jarvis.requests.post = self._load, self._post

    def model(self, final="На столе хлеб.", pin_leak=False):
        seen = []

        def claude(url, headers=None, json=None, timeout=None):
            seen.append(json)
            text = "PIN 9999 и пароль hunter2" if pin_leak else final
            return R({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})

        jarvis.requests.post = claude
        return seen

    def test_turn_uses_assistant_loop(self):
        seen = self.model()
        r = jarvis.handle_tablet_turn("что купить", "dev1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["text"], "На столе хлеб.")
        self.assertTrue(seen)
        self.assertIn("что купить", json.dumps(seen[0], ensure_ascii=False))

    def test_history_keeps_next_turn(self):
        self.model("ок")
        jarvis.handle_tablet_turn("первый", "dev1")
        seen = []

        def claude(url, headers=None, json=None, timeout=None):
            seen.append(json)
            return R({"content": [{"type": "text", "text": "второй"}], "stop_reason": "end_turn"})

        jarvis.requests.post = claude
        r = jarvis.handle_tablet_turn("ещё", "dev1")
        self.assertEqual(r["text"], "второй")
        msgs = seen[0]["messages"]
        roles = [m["role"] for m in msgs]
        self.assertIn("user", roles)
        blob = json.dumps(msgs, ensure_ascii=False)
        self.assertIn("первый", blob)

    def test_tablet_gets_voice_block_telegram_does_not(self):
        seen = self.model()
        jarvis.handle_tablet_turn("какая погода", "dev1")
        self.assertTrue(seen)
        self.assertIn("ГОЛОС:", json.dumps(seen[0], ensure_ascii=False))
        self.assertIn("1–2 короткие", json.dumps(seen[0], ensure_ascii=False))

        seen.clear()
        jarvis.handle_tablet_turn("расскажи про погоду", "dev1")
        self.assertIn("просит подробно", json.dumps(seen[0], ensure_ascii=False))

        seen.clear()
        bot = jarvis.tg_bot
        old = {n: getattr(bot, n) for n in (
            "history_for", "touch", "api", "set_ctx", "send_text",
            "current_dialog", "bump_dialog", "dialog_footer")}
        bot.history_for = lambda cid: [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
        bot.touch = lambda cid: None
        bot.api = lambda *a, **k: None
        bot.set_ctx = lambda ctx: None
        bot.send_text = lambda *a, **k: None
        bot.current_dialog = lambda cid: {"title": "x"}
        bot.bump_dialog = lambda cid: None
        bot.dialog_footer = lambda *a, **k: ""
        try:
            jarvis._handle_assistant_message("t", 1, "какая погода", [], None)
        finally:
            for n, v in old.items():
                setattr(bot, n, v)
        self.assertTrue(seen)
        self.assertNotIn("ГОЛОС:", json.dumps(seen[0], ensure_ascii=False))

    def test_leaked_pin_stripped(self):
        self.model(pin_leak=True)
        r = jarvis.handle_tablet_turn("пин карты", "dev1")
        self.assertNotIn("9999", r["text"])
        self.assertNotIn("hunter2", r["text"])


class TabletHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        jarvis._tablet_history.clear()
        self.app = {"telegramSkills": {}, "settings": {
            "aiPresets": [{"id": "a", "name": "n", "provider": "claude", "apiKey": "k", "model": ""}],
            "aiAssignments": {"voiceAssistant": "a"},
        }}
        self._load, self._post = jarvis.load_app_data, jarvis.requests.post
        jarvis.load_app_data = lambda: self.app
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        jarvis.load_app_data, jarvis.requests.post = self._load, self._post
        if self._old_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_dir
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=False, token=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode())
                return resp.status, payload
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode()) if not raw else e.read()
            return e.code, payload

    def test_unauthorized_and_login_and_turn(self):
        code, body = self.req("POST", "/api/tablet/turn", {"text": "привет"})
        self.assertEqual(code, 401)

        code, body = self.req("POST", "/api/tablet/login", {"password": "wrong"})
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))

        code, body = self.req("POST", "/api/tablet/login", {"password": jarvis.AUTH_PASSWORD})
        self.assertEqual(code, 200)
        token = body["token"]
        self.assertTrue(token)

        def claude(url, headers=None, json=None, timeout=None):
            return R({"content": [{"type": "text", "text": "PIN 4321 готово"}], "stop_reason": "end_turn"})

        jarvis.requests.post = claude
        code, body = self.req("POST", "/api/tablet/turn", {"text": "скажи пин"}, token=token)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn("4321", body["text"])
        self.assertIn("готово", body["text"])

        code, body = self.req("GET", "/api/tablet/status", cookie=True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

        code, body = self.req("POST", "/api/tablet/turn", {"text": "ещё раз"}, cookie=True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
