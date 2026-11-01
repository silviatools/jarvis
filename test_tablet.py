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


def spot_classify(final, text, conf, wake_conf=0.55, stop_conf=0.70, speaking=False, external=False):
    """Зеркало SpotRule 1.15: во время SPEAK порог выше, на BT/кабеле ещё выше."""
    if speaking and external:
        wake_conf, stop_conf = 0.82, 0.88
    elif speaking:
        wake_conf, stop_conf = 0.75, 0.82
    tokens = [
        t for t in re.sub(r"[^а-яёa-z0-9\s]", " ", (text or "").lower()).split()
        if t and t not in ("unk", "[unk]")
    ]
    if not tokens or not final or len(tokens) != 1:
        return "none"
    if tokens[0] in ("стоп", "хватит"):
        return "stop" if conf >= stop_conf else "none"
    if tokens[0] in ("джарвис", "джервис") and conf >= wake_conf:
        return "wake"
    return "none"


def wake_confirm(events, ignore_ms=0, mute_first=0):
    """Зеркало VoskWake 1.14: replay/1 с глушатся, partial не будит."""
    out = []
    for i, e in enumerate(events):
        if e.get("t_ms", 0) < ignore_ms or i < mute_first:
            out.append("none")
            continue
        out.append(spot_classify(e["final"], e["text"], e["conf"]))
    return out


def is_echo(exempt, stream_start, echo_until, heard, spoken):
    """Зеркало WakeWord.isEcho 1.13."""
    if exempt or stream_start >= echo_until:
        return False
    return near_echo(heard, spoken)


def ngram_echo(heard, spoken):
    if has_name(heard):
        return False
    a = re.sub(r"[^а-яёa-z0-9 ]", " ", (heard or "").lower())
    a = re.sub(r"\s+", " ", a).strip()
    b = re.sub(r"[^а-яёa-z0-9 ]", " ", (spoken or "").lower())
    b = re.sub(r"\s+", " ", b).strip()
    if len(a) < 5 or len(b) < 5:
        return False
    if a in b or (len(a) >= 8 and b in a):
        return True
    aw = [t for t in a.split() if len(t) > 1]
    bw = [t for t in b.split() if len(t) > 1]
    if not aw:
        return False
    n = min(3, len(aw))
    if any(" ".join(aw[i:i + n]) in b for i in range(len(aw) - n + 1)):
        return True
    hit = sum(1 for t in aw if t in bw)
    return hit > 0 and hit * 2 >= len(aw)


def clip_log(s, max_len=160):
    one = (s or "").replace("\n", " ")
    return one if len(one) <= max_len else one[:max_len] + "…"


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
    return allow_turn(stt, need_name=not in_convo, spotter_wake=in_convo or has_name(stt), name_at_start=has_name(stt))


def endpoint_clamp(raw, default=2000, step=500, lo=500, hi=5000):
    """Зеркало EndpointPause.clamp: шаг 0.5 с, диапазон 0.5–5 с."""
    if raw <= 0:
        return default
    snapped = ((raw + step // 2) // step) * step
    return min(hi, max(lo, snapped))


def endpoint_ends(silent_ms, threshold_ms):
    """Зеркало EndpointPause.ends — сравнение в appendCapture на каждом кадре."""
    return silent_ms >= threshold_ms


def convo_clamp(raw):
    return min(20, max(0, int(raw)))


def convo_decide(now, deadline, speech_held):
    """Зеркало ConvoWindow.decide: речь держит окно, иначе ноль закрывает."""
    if speech_held:
        return "hold"
    if deadline <= 0 or now >= deadline:
        return "expire"
    return "wait"


def convo_cap(now, deadline, seconds):
    """Зеркало ConvoWindow.capDeadline: остаток не длиннее нового окна."""
    if deadline <= 0:
        return deadline
    cap = convo_clamp(seconds) * 1000
    left = deadline - now
    if left <= cap:
        return deadline
    return now + cap


def convo_episode(window_ms, speech_start, speech_end, step=200):
    """Окно с нуля. speech_start=None — речи не было.
    Возвращает phase, turn, sounds (мс, когда сыграл бы конец)."""
    deadline = window_ms
    held = False
    sounds = []
    limit = window_ms if speech_end is None else speech_end
    t = 0
    while t <= limit:
        if speech_start is not None and speech_start <= t < speech_end and (t <= window_ms or held):
            held = True
        decision = convo_decide(t, deadline, held)
        if decision == "expire":
            sounds.append(t)
            return {"phase": "WAKE", "turn": False, "sounds": sounds}
        if speech_end is not None and t >= speech_end and held:
            return {"phase": "TURN", "turn": True, "sounds": sounds}
        t += step
    return {"phase": "LISTEN", "turn": False, "sounds": sounds}


def allow_turn(stt, need_name, spotter_wake, name_at_start):
    t = (stt or "").strip()
    if not t:
        return False
    if re.match(r"^\s*(эй[,.]?\s*)?(окей[,.]?\s*)?(джарвис|джервис|жервис|жарвис|дарвис|джарвиз|джарвес|алиса)[а-яё]*[,!.]?\s*$", t.lower()):
        return False
    if not need_name:
        return True
    if not spotter_wake:
        return False
    if has_name(t):
        return True
    words = [w for w in t.split() if w]
    return bool(name_at_start and len(words) <= 3)

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
        self.assertIn("MAX_UTTER_MS = 30_000L", src)
        self.assertIn("READY_IGNORE_MS = 1000L", src)
        self.assertNotIn("HOLD_AFTER_WAKE_MS", src)
        self.assertIn("echoExempt", src)
        self.assertIn("abortListen", src)
        self.assertIn("heardPing", src)
        self.assertIn("wake готов, прогрет", src)
        self.assertIn("ignoreFor", src)
        self.assertIn("w.replay(extra)", src)
        self.assertIn("нет имени — игнор", src)
        self.assertIn("окно закрыто", src)
        self.assertIn("notif_convo", src)
        self.assertIn("expireConvo", src)
        self.assertIn("needName", src)
        self.assertIn("floor * 1.35", src)
        self.assertNotIn("speakLocal(getString(R.string.yes))", src)
        self.assertNotIn("overlapsTts", src)
        self.assertNotIn("PARTIAL_WAKE_HITS", src)
        self.assertNotIn("SILENCE_MS", src)

    def test_v114_wake_name_gate(self):
        self.assertTrue(allow_turn("Джарвис какая погода", True, True, False))
        self.assertTrue(allow_turn("какая погода", True, True, True))
        self.assertFalse(allow_turn("Это трейсер. Мы снимаем рекламу.", True, True, True))
        self.assertFalse(allow_turn("да конечно вот", True, False, False))
        self.assertTrue(allow_turn("Да, про погоду в Москве.", False, False, False))
        self.assertFalse(allow_turn("Джарвис", True, True, True))
        ev = [
            {"t_ms": 50, "final": False, "text": "джарвис", "conf": 1.0},
            {"t_ms": 100, "final": False, "text": "джарвис", "conf": 1.0},
            {"t_ms": 200, "final": True, "text": "джарвис какая", "conf": 0.9},
        ]
        self.assertEqual(wake_confirm(ev), ["none", "none", "none"])
        self.assertEqual(spot_classify(True, "джарвис", 0.7), "wake")

    def test_v115_speak_barge_and_clip(self):
        spoken = "Для поездки лучше выбрать домик в кемпинге у реки."
        self.assertTrue(ngram_echo("Лучше выбрать домик.", spoken))
        self.assertTrue(ngram_echo("Для поездки", spoken))
        self.assertFalse(ngram_echo("Джарвис какая погода", spoken))
        self.assertEqual(spot_classify(True, "джарвис", 0.70, speaking=True, external=True), "none")
        self.assertEqual(spot_classify(True, "стоп", 0.80, speaking=True, external=True), "none")
        self.assertEqual(spot_classify(True, "джарвис", 0.90, speaking=True, external=True), "wake")
        long = "а" * 200
        self.assertTrue(clip_log(long).endswith("…"))
        self.assertGreater(len(clip_log(long)), 48)
        src = (Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java"
               / "ru/jarvis/tablet/VoiceService.kt").read_text(encoding="utf-8")
        self.assertIn("barge-in без имени — эхо", src)
        self.assertIn("out=", src)
        self.assertIn("pauseSpeechForBarge", src)
        self.assertNotIn("echo сброшен (команда)", src)
        self.assertIn("+ \"…\"", src)
        rule = (Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java"
                / "ru/jarvis/tablet/SpotRule.kt").read_text(encoding="utf-8")
        self.assertIn("SPEAK_EXT_WAKE_CONF = 0.82", rule)
        self.assertIn("SPEAK_EXT_STOP_CONF = 0.88", rule)

    def test_v116_endpoint_pause_is_live_vad(self):
        """Смена паузы двигает порог тишины в appendCapture, не подпись на экране."""
        root = Path(__file__).resolve().parent / "android/tablet-voice/app/src/main"
        voice = (root / "java/ru/jarvis/tablet/VoiceService.kt").read_text(encoding="utf-8")
        cap = voice.split("private fun appendCapture")[1].split("private fun onUtterance")[0]
        self.assertIn("EndpointPause.ends(silentMs, EndpointPause.silenceMs)", cap)
        self.assertNotIn("SILENCE_MS", cap)
        self.assertNotIn("CONVO_MS", voice)
        wake = voice.split("private fun onWakeSpot()")[1].split("private fun onStopSpot()")[0]
        barge = wake.split("phase == Phase.SPEAK")[1].split("return")[0]
        self.assertIn("heardPing()", barge)
        after = wake.split('beginListen("barge-wake"')[1]
        self.assertIn("heardPing()", after)
        exp = voice.split("private fun expireConvo")[1].split("private fun armConvoTick")[0]
        self.assertIn("Earcon.done()", exp)
        self.assertIn("val closing = !needName && convoUntil > 0L && !convoHeld", exp)
        self.assertIn("bus.level", voice)
        ep = (root / "java/ru/jarvis/tablet/EndpointPause.kt").read_text(encoding="utf-8")
        self.assertIn("const val DEFAULT_MS = 2000", ep)
        self.assertIn("const val MIN_MS = 500", ep)
        self.assertIn("const val MAX_MS = 5000", ep)
        self.assertIn("const val STEP_MS = 500", ep)
        self.assertIn("fun ends(silentMs: Int, thresholdMs: Int): Boolean = silentMs >= thresholdMs", ep)
        commit = ep.split("fun commit")[1].split("fun ")[0]
        self.assertIn("silenceMs = ms", commit)
        self.assertIn('VoiceLog.add("пауза конца $ms мс")', commit)
        load = ep.split("fun load")[1].split("fun commit")[0]
        self.assertNotIn("VoiceLog", load)
        prefs = (root / "java/ru/jarvis/tablet/Prefs.kt").read_text(encoding="utf-8")
        setter = prefs.split("var endpointMs")[1].split("fun configured")[0]
        self.assertIn("EndpointPause.commit", setter)
        self.assertIn('getBoolean("dev_ui", false)', prefs)
        self.assertIn("EndpointPause.load", prefs)
        ui = (root / "java/ru/jarvis/tablet/MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("Prefs.endpointMs = ms", ui)
        self.assertIn("fromUser", ui)
        self.assertIn("Prefs.devUi", ui)
        strings = (root / "res/values/strings.xml").read_text(encoding="utf-8")
        self.assertIn("Пауза, после которой я считаю, что ты договорил", strings)
        tail = (root / "res/layout/include_user_tail.xml").read_text(encoding="utf-8")
        self.assertIn('android:valueFrom="0.5"', tail)
        self.assertIn('android:valueTo="5.0"', tail)
        self.assertIn('android:stepSize="0.5"', tail)
        self.assertIn("@+id/endpointSlider", tail)
        self.assertNotIn("@+id/log", tail)
        dev = (root / "res/layout/include_dev.xml").read_text(encoding="utf-8")
        self.assertIn("@+id/version", dev)
        self.assertIn("@+id/log", dev)
        self.assertIn("@+id/toggle", dev)
        self.assertIn("@+id/copyLog", dev)
        self.assertNotIn("endpointSlider", dev)
        for name in ("layout/activity_main.xml", "layout-land/activity_main.xml"):
            screen = (root / "res" / name).read_text(encoding="utf-8")
            self.assertIn("@+id/userRoot", screen)
            self.assertIn("@+id/modeToggle", screen)
            self.assertIn("@+id/orb", screen)
            self.assertIn("@+id/historyBox", screen)
            self.assertIn("@layout/include_dev", screen)
            self.assertIn("@layout/include_user_tail", screen)
            self.assertNotIn("@+id/version", screen)
            self.assertNotIn("@+id/log", screen)
            user, _dev = screen.split("@+id/devRoot", 1)
            self.assertIn("@+id/userRoot", user)
            self.assertNotIn("@+id/version", user)
        gradle = (Path(__file__).resolve().parent / "android/tablet-voice/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('versionName = "1.17"', gradle)
        self.assertIn("versionCode = 18", gradle)
        recs = []
        for p in (root / "java").rglob("*.kt"):
            if "AudioRecord(" in p.read_text(encoding="utf-8"):
                recs.append(p.name)
        self.assertEqual(recs, ["Ear.kt"])
        self.assertNotIn("SpeechRecognizer", (root / "java/ru/jarvis/tablet/Earcon.kt").read_text(encoding="utf-8"))
        self.assertNotIn("AudioRecord", (root / "java/ru/jarvis/tablet/OrbView.kt").read_text(encoding="utf-8"))
        # та же фраза, 1.6 с тишины: 2 с ещё нет, 1.5 с уже конец, 2.5 с ещё нет
        self.assertFalse(endpoint_ends(1600, endpoint_clamp(2000)))
        self.assertTrue(endpoint_ends(1600, endpoint_clamp(1500)))
        self.assertFalse(endpoint_ends(1600, endpoint_clamp(2500)))
        self.assertEqual(endpoint_clamp(2000), 2000)
        self.assertEqual(endpoint_clamp(0), 2000)
        self.assertEqual(endpoint_clamp(400), 500)
        self.assertEqual(endpoint_clamp(1750), 2000)
        self.assertEqual(endpoint_clamp(1749), 1500)
        self.assertEqual(endpoint_clamp(9000), 5000)
        self.assertEqual(endpoint_clamp(2500), 2500)

    def test_v117_window_holds_phrase_and_slider_is_timer(self):
        """Речь с 4-й по 7-ю секунду — ход, без звука конца. Тишина — конец на 5-й и WAKE."""
        root = Path(__file__).resolve().parent / "android/tablet-voice/app/src/main"
        voice = (root / "java/ru/jarvis/tablet/VoiceService.kt").read_text(encoding="utf-8")
        tick = voice.split("private fun armConvoTick")[1].split("private fun cancelConvoTick")[0]
        self.assertIn("convoUntil = ConvoWindow.capDeadline(now, convoUntil, ConvoWindow.seconds)", tick)
        self.assertIn("ConvoWindow.decide(now, convoUntil, convoHeld)", tick)
        expire_branch = tick.split("Decision.EXPIRE")[1].split("Decision.HOLD")[0]
        self.assertIn('expireConvo("таймаут")', expire_branch)
        hold_branch = tick.split("Decision.HOLD")[1]
        self.assertNotIn("expireConvo", hold_branch)
        self.assertNotIn("Earcon.done", hold_branch)
        cap = voice.split("private fun appendCapture")[1].split("private fun onUtterance")[0]
        self.assertIn("val started = !speechSeen && speech", cap)
        self.assertIn("if (started && !needName) holdConvo()", cap)
        self.assertIn("EndpointPause.ends(silentMs, EndpointPause.silenceMs)", cap)
        hold = voice.split("private fun holdConvo")[1].split("private fun stayQuiet")[0]
        self.assertIn("convoHeld = true", hold)
        exp = voice.split("private fun expireConvo")[1].split("private fun retargetConvo")[0]
        self.assertIn("if (closing) Earcon.done()", exp)
        self.assertIn("!convoHeld", exp)
        retarget = voice.split("private fun retargetConvo")[1].split("private fun armConvoTick")[0]
        self.assertIn("convoUntil = ConvoWindow.capDeadline(now, convoUntil, ConvoWindow.seconds)", retarget)
        self.assertIn('expireConvo("настройка")', retarget)
        self.assertIn("ConvoWindow.onChange", voice)
        self.assertIn("ConvoWindow.durationMs()", voice)
        cw = (root / "java/ru/jarvis/tablet/ConvoWindow.kt").read_text(encoding="utf-8")
        self.assertIn("const val DEFAULT_S = 5", cw)
        self.assertIn("const val MIN_S = 0", cw)
        self.assertIn("const val MAX_S = 20", cw)
        self.assertIn("if (speechHeld) return Decision.HOLD", cw)
        self.assertIn("if (deadline <= 0L || now >= deadline) return Decision.EXPIRE", cw)
        self.assertIn("return now + cap", cw)
        commit = cw.split("fun commit")[1]
        self.assertIn("seconds = s", commit)
        self.assertIn('VoiceLog.add("окно диалога $s с")', commit)
        self.assertIn("onChange?.invoke(s)", commit)
        prefs = (root / "java/ru/jarvis/tablet/Prefs.kt").read_text(encoding="utf-8")
        setter = prefs.split("var convoSec")[1].split("fun configured")[0]
        self.assertIn("ConvoWindow.commit", setter)
        ui = (root / "java/ru/jarvis/tablet/MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("Prefs.convoSec = sec", ui)
        tail = (root / "res/layout/include_user_tail.xml").read_text(encoding="utf-8")
        self.assertIn("@+id/convoSlider", tail)
        self.assertIn('android:valueFrom="0.0"', tail)
        self.assertIn('android:valueTo="20.0"', tail)
        self.assertIn('android:stepSize="1.0"', tail)
        strings = (root / "res/values/strings.xml").read_text(encoding="utf-8")
        self.assertIn("Окно продолжения без имени", strings)
        ep = (root / "java/ru/jarvis/tablet/EndpointPause.kt").read_text(encoding="utf-8")
        self.assertIn("const val DEFAULT_MS = 2000", ep)
        spoke = convo_episode(5000, 4000, 7000)
        self.assertTrue(spoke["turn"])
        self.assertEqual(spoke["sounds"], [])
        self.assertEqual(spoke["phase"], "TURN")
        self.assertTrue(allow_command("погода в Москве", True))
        quiet = convo_episode(5000, None, None)
        self.assertFalse(quiet["turn"])
        self.assertEqual(quiet["phase"], "WAKE")
        self.assertEqual(quiet["sounds"], [5000])
        # ползунок двигает дедлайн, с которым decide закрывает окно
        shortened = convo_cap(1000, 5000, 2)
        self.assertEqual(shortened, 3000)
        self.assertEqual(convo_decide(2999, shortened, False), "wait")
        self.assertEqual(convo_decide(3000, shortened, False), "expire")
        self.assertEqual(convo_cap(1000, 5000, 20), 5000)
        self.assertEqual(convo_cap(1000, 5000, 0), 1000)
        self.assertEqual(convo_decide(1000, convo_cap(1000, 5000, 0), False), "expire")
        # удержание важнее укороченного дедлайна: фраза не обрывается
        self.assertEqual(convo_decide(3000, shortened, True), "hold")

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
        self.assertEqual(spot_classify(False, "джарвис", 1.0), "none")
        self.assertEqual(spot_classify(True, "джарвис какая", 0.9), "none")
        self.assertEqual(spot_classify(True, "джарвис", 0.4), "none")
        self.assertEqual(spot_classify(True, "джарвис", 0.7), "wake")
        self.assertEqual(spot_classify(True, "какая погода", 0.99), "none")
        ev = [
            {"t_ms": 50, "final": False, "text": "джарвис", "conf": 1.0},
            {"t_ms": 100, "final": False, "text": "джарвис", "conf": 1.0},
        ]
        self.assertEqual(wake_confirm(ev, ignore_ms=1000), ["none", "none"])
        self.assertEqual(wake_confirm(ev), ["none", "none"])
        root = Path(__file__).resolve().parent / "android/tablet-voice/app/src/main/java/ru/jarvis/tablet"
        vosk = (root / "VoskPack.kt").read_text(encoding="utf-8")
        rule = (root / "SpotRule.kt").read_text(encoding="utf-8")
        voice = (root / "VoiceService.kt").read_text(encoding="utf-8")
        self.assertNotIn("text.contains", vosk)
        self.assertIn("partialResult", vosk)
        self.assertIn("ignoreFor", vosk)
        self.assertNotIn("PARTIAL_WAKE_HITS", rule)
        self.assertIn("WAKE_CONF = 0.55", rule)
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
