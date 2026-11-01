"""Планшетный голосовой мост: подмена ИИ, без настоящих Telegram/модели."""

import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def spot_classify(final, text, conf, wake_conf=0.50, stop_conf=0.70):
    """Зеркало SpotRule 1.13: partial — гипотеза, финал с порогом 0.50."""
    tokens = [
        t for t in re.sub(r"[^а-яёa-z0-9\s]", " ", (text or "").lower()).split()
        if t and t not in ("unk", "[unk]")
    ]
    if not tokens:
        return "none"
    if tokens[0] in ("стоп", "хватит"):
        return "stop" if final and len(tokens) == 1 and conf >= stop_conf else "none"
    wake = ("джарвис", "джервис", "жервис", "жарвис", "дарвис", "джарвиз", "джарвес")
    if any(t in wake for t in tokens) and (not final or conf >= wake_conf):
        return "wake"
    return "none"


def wake_confirm(events, ignore_ms=0, mute_first=0):
    """Зеркало VoskWake: replay/первая секунда не wake; partial — 2 кадра."""
    hits = 0
    out = []
    for i, e in enumerate(events):
        if e.get("t_ms", 0) < ignore_ms or i < mute_first:
            hits = 0
            out.append("none")
            continue
        k = spot_classify(e["final"], e["text"], e["conf"])
        if k == "wake":
            if e["final"]:
                hits = 0
                out.append("wake")
            else:
                hits += 1
                if hits >= 2:
                    hits = 0
                    out.append("wake")
                else:
                    out.append("none")
        else:
            hits = 0
            out.append(k)
    return out


def is_echo(exempt, stream_start, echo_until, heard, spoken):
    """Зеркало WakeWord.isEcho 1.13."""
    if exempt or stream_start >= echo_until:
        return False
    return near_echo(heard, spoken)


def near_echo(heard, spoken):
    a = re.sub(r"[^а-яёa-z0-9 ]", " ", (heard or "").lower()).strip()
    b = re.sub(r"[^а-яёa-z0-9 ]", " ", (spoken or "").lower()).strip()
    if len(a) < 8 or len(b) < 8:
        return False
    if a in b:
        return True
    if b in a and len(b) >= 12:
        return True
    aw = [t for t in a.split() if len(t) > 2]
    bw = {t for t in b.split() if len(t) > 2}
    if len(aw) < 4:
        return False
    return all(t in bw for t in aw) and len(a) <= len(b) + 6


def has_name(text):
    return bool(re.search(
        r"(?:^|[^\w])(эй[,.]?\s*)?(окей[,.]?\s*)?(джарвис|джервис|жервис|жарвис|дарвис|джарвиз|джарвес|алиса)",
        (text or "").lower(),
    ))


def allow_command(stt, in_convo):
    t = (stt or "").strip()
    if not t:
        return False
    if re.match(r"^\s*(эй[,.]?\s*)?(окей[,.]?\s*)?(джарвис|джервис|жервис|жарвис|дарвис|джарвиз|джарвес|алиса)[а-яё]*[,!.]?\s*$", t.lower()):
        return False
    if has_name(t):
        return True
    return in_convo

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

    def test_pcm_wav_and_stt_no_key(self):
        wav = jarvis.pcm16_to_wav(b"\x00\x01" * 10, 16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])
        jarvis.load_app_data = lambda: {"settings": {"aiPresets": []}}
        r = jarvis.handle_tablet_stt(b"\x00\x01" * 80, "application/octet-stream")
        self.assertEqual(r["error"], "no_key")

    def test_apk_listen_path_has_no_google_stt(self):
        root = Path(__file__).resolve().parent / "android" / "tablet-voice" / "app" / "src" / "main"
        files = list(root.rglob("*.kt")) + list(root.rglob("*.xml"))
        self.assertTrue(files)
        banned = ("SpeechRecognizer", "RecognizerIntent", "android.speech.RecognitionListener")
        hits = []
        for p in files:
            text = p.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    hits.append(f"{p.relative_to(root)}:{token}")
        self.assertEqual(hits, [], "Google STT остался в горячем пути: " + ", ".join(hits))
        self.assertFalse((root / "java/ru/jarvis/tablet/MicKeep.kt").exists())

    def test_spotter_not_gated_by_echo_window(self):
        src = (Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java"
               / "ru/jarvis/tablet/VoiceService.kt").read_text(encoding="utf-8")
        wake = src.split("private fun onWakeSpot()")[1].split("private fun onStopSpot()")[0]
        stop = src.split("private fun onStopSpot()")[1].split("private fun spotOk()")[0]
        self.assertNotIn("echoUntil", wake)
        self.assertNotIn("echoUntil", stop)
        self.assertIn("barge-in wake", wake)
        self.assertIn("barge-in стоп", stop)
        self.assertIn("echoUntil", src)

    def test_v113_listen_policy(self):
        src = (Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java"
               / "ru/jarvis/tablet/VoiceService.kt").read_text(encoding="utf-8")
        self.assertIn("PREROLL_MS = 2500", src)
        self.assertIn("SILENCE_MS = 1000", src)
        self.assertIn("MAX_UTTER_MS = 30_000L", src)
        self.assertIn("READY_IGNORE_MS = 1000L", src)
        self.assertNotIn("HOLD_AFTER_WAKE_MS", src)
        self.assertIn("echoExempt", src)
        self.assertIn("abortListen", src)
        self.assertIn("heardPing", src)
        self.assertIn("wake готов, прогрет", src)
        self.assertIn("ignoreFor", src)
        self.assertIn("w.replay(extra)", src)
        self.assertIn("stt без имени — без хода", src)
        self.assertIn('barge || why == "после TTS"', src)
        self.assertIn("floor * 1.35", src)
        self.assertNotIn("speakLocal(getString(R.string.yes))", src)
        self.assertNotIn("overlapsTts", src)

    def test_v113_echo_and_name_gate(self):
        spoken = "В Москве сейчас плюс пять, без осадков, ветер слабый."
        tts_end = 1_700_000_000_000
        tail = tts_end + 900
        self.assertFalse(is_echo(False, tail + 1500, tail, "Да, про погоду в Москве.", spoken))
        self.assertFalse(is_echo(False, tail + 3000, tail, "Что ты умеешь?", spoken))
        self.assertFalse(is_echo(True, tail - 100, tail, spoken, spoken))
        self.assertTrue(is_echo(False, tail - 50, tail, "в москве сейчас плюс пять без осадков", spoken))
        self.assertTrue(allow_command("Джарвис какая погода", False))
        self.assertFalse(allow_command("Это трейсер. Мы снимаем.", False))
        self.assertTrue(allow_command("Да, про погоду в Москве.", True))
        self.assertFalse(allow_command("Джарвис", False))

    def test_spot_rule_wake_soft_stop_strict(self):
        self.assertEqual(spot_classify(False, "стоп", 0.99), "none")
        self.assertEqual(spot_classify(True, "стоп какая", 0.99), "none")
        self.assertEqual(spot_classify(True, "стоп", 0.4), "none")
        self.assertEqual(spot_classify(True, "стоп", 0.9), "stop")
        self.assertEqual(spot_classify(False, "джарвис", 1.0), "wake")
        self.assertEqual(spot_classify(True, "джарвис какая", 0.4), "none")
        self.assertEqual(spot_classify(True, "джарвис какая", 0.55), "wake")
        self.assertEqual(spot_classify(True, "какая погода", 0.99), "none")
        ev = [
            {"t_ms": 50, "final": False, "text": "джарвис", "conf": 1.0},
            {"t_ms": 100, "final": False, "text": "джарвис", "conf": 1.0},
        ]
        self.assertEqual(wake_confirm(ev, ignore_ms=1000), ["none", "none"])
        self.assertEqual(wake_confirm(ev), ["none", "wake"])
        root = Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java/ru/jarvis/tablet"
        vosk = (root / "VoskPack.kt").read_text(encoding="utf-8")
        rule = (root / "SpotRule.kt").read_text(encoding="utf-8")
        voice = (root / "VoiceService.kt").read_text(encoding="utf-8")
        self.assertNotIn("text.contains", vosk)
        self.assertIn("partialResult", vosk)
        self.assertIn("ignoreFor", vosk)
        self.assertIn("PARTIAL_WAKE_HITS = 2", rule)
        self.assertIn("WAKE_CONF = 0.50", rule)
        self.assertIn("STOP_CONF = 0.70", rule)
        stop = voice.split("private fun onStopSpot()")[1].split("private fun abortListen")[0]
        self.assertIn("phase == Phase.LISTEN && capturing", stop)
        self.assertNotIn("wake уже слушаю", voice)

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

    def test_stt_needs_auth_and_uses_whisper_mock(self):
        code, body = self.req("POST", "/api/tablet/stt", None)
        self.assertEqual(code, 401)

        code, body = self.req("POST", "/api/tablet/login", {"password": jarvis.AUTH_PASSWORD})
        token = body["token"]

        self.app["settings"]["aiPresets"] = [
            {"id": "a", "name": "n", "provider": "claude", "apiKey": "k", "model": ""},
            {"id": "o", "name": "o", "provider": "openai", "apiKey": "sk-test", "model": ""},
        ]
        seen = []

        def post(url, headers=None, data=None, files=None, json=None, timeout=None):
            seen.append({"url": url, "has_file": bool(files), "data": data})
            if "transcriptions" in str(url):
                return R({"text": "какая погода"})
            return R({"content": [{"type": "text", "text": "ок"}], "stop_reason": "end_turn"})

        jarvis.requests.post = post
        pcm = b"\x00\x01" * 200
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/tablet/stt",
            data=pcm, method="POST",
            headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["text"], "какая погода")
        self.assertTrue(any("transcriptions" in str(x["url"]) for x in seen))
        self.assertTrue(any(
            isinstance(x.get("data"), dict) and "Джарвис" in str(x["data"].get("prompt", ""))
            for x in seen
        ))

        empty = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/tablet/stt",
            data=b"", method="POST",
            headers={"Content-Type": "audio/wav", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(empty, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        self.assertEqual(payload.get("error"), "empty")

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
