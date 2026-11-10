"""Widget tool integration, model loops and Bot API, without live messages."""
import json
import os
import random
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jarvis
import tg_bot
import tg_history
import tool_groups
import widget_skill as widgets

HTML = '<style>button{color:var(--tg-theme-text-color)}</style><p id="n">0</p><button onclick="document.getElementById(\'n\').textContent=++n">+</button><script>let n=0</script>'
ARGS = {"html": HTML, "caption": "Счётчик: нажмите +"}


class WidgetsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = {"telegramSkills": {}}
        self.ctx = tg_bot.Ctx("fake-token", 123, 5)
        tg_bot.set_ctx(self.ctx)
        self.addCleanup(tg_bot.set_ctx, None)
        for p in (patch.dict(os.environ, {"DATA_DIR": tmp.name}),
                  patch.object(tg_history, "get_state", return_value={}),
                  patch.object(tg_bot, "DATA_DIR", Path(tmp.name)),
                  patch.object(tg_bot, "STATE_FILE", Path(tmp.name) / "bot_state.json")):
            p.start()
            self.addCleanup(p.stop)
        self.calls, self.uploads = [], []

        def post(url, json=None, **kw):
            if json is None:
                json = kw.get("data", {})
            self.calls.append((url.rsplit('/', 1)[-1], json))
            if kw.get("files"):
                self.uploads.append(kw["files"])
            return Mock(status_code=200, json=lambda: {"ok": True, "result": {
                "message_id": 42, "chat": {"id": 123, "type": "private"},
                "caption": json.get("caption", ""), "widget": {"html": json.get("html", "")}}})
        p = patch.object(tg_bot.requests, "post", side_effect=post)
        self.post = p.start()
        self.addCleanup(p.stop)

    def execute(self, args=None):
        return jarvis.execute_assistant_tool("tg_send_widget", ARGS if args is None else args, self.app, "")

    def test_send_preserves_source_and_current_chat_and_archives_caption(self):
        result = self.execute(dict(ARGS, height=280))
        self.assertTrue(result["ok"])
        self.assertEqual(result["client_rendering"], "unknown")
        self.assertIn("Не вижу виджет", self.calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"])
        method, payload = self.calls[0]
        self.assertEqual(method, "sendWidget")
        self.assertEqual(payload["html"], HTML)  # not the Telegram text sanitizer
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(payload["height"], 280)
        self.assertNotIn("width", payload)
        self.assertNotIn("parse_mode", payload)
        self.assertFalse(payload["allow_queries"])
        self.assertEqual(self.ctx.last_message_id, 42)
        self.assertIn(ARGS["caption"], self.ctx.sent[0])
        self.assertEqual(tg_history.lookup(123, 42)["text"], ARGS["caption"])

    def test_catalog_group_and_toggle_for_tools_prompt_and_executor(self):
        self.assertIn(widgets.SKILLS[0], jarvis.assistant_skills_catalog())
        self.assertEqual(tool_groups.group_of("tg_send_widget"), "widgets")
        groups = tool_groups.pick("объясни дроби")
        self.assertIn("widgets", groups)  # autonomous presentation choice on any topic
        for disabled in (False, True):
            self.app["telegramSkills"][widgets.SKILL_ID] = not disabled
            ts = jarvis._TurnState(self.app, False, None, groups, None, {}, "openai", "m", "", "", True, False, "main", "chat", 2)
            names = {t["function"]["name"] for t in ts.openai_tools()}
            self.assertEqual("tg_send_widget" in names, not disabled)
            self.assertEqual("tg_send_widget" in jarvis.build_assistant_system_prompt(self.app, groups=groups, compact=True), not disabled)
            self.assertEqual("tg_send_widget" in {t["name"] for t in ts.claude_tools()}, not disabled)
        self.assertIn("выключен", self.execute()["error"])
        self.post.assert_not_called()

    def test_private_chat_context_and_destination_cannot_be_overridden(self):
        for cid in (-123, None):
            tg_bot.set_ctx(tg_bot.Ctx("fake", cid) if cid else None)
            self.assertIn("error", self.execute())
        tg_bot.set_ctx(self.ctx)
        self.assertIn("error", self.execute(dict(ARGS, chat_id=999)))
        self.post.assert_not_called()

    def test_validation_is_in_bytes_and_never_truncates_source(self):
        rng = random.Random(123)
        incompressible = ''.join(rng.choices(string.ascii_letters + string.digits, k=7000))
        bad = [dict(ARGS, html=""), dict(ARGS, html="\x00"), dict(ARGS, html="```html\nx```"),
               dict(ARGS, html="<script>drawEverything()</script>"),
               dict(ARGS, html="<html><head><title>Hidden</title><style>p{color:red}</style></head><body></body></html>"),
               dict(ARGS, html="я" * 16385), dict(ARGS, html=incompressible),
               dict(ARGS, caption=""), dict(ARGS, caption="😀" * 513),
               dict(ARGS, width=True), dict(ARGS, height=601), dict(ARGS, width=199)]
        for args in bad:
            with self.subTest(args=str(args)[:80]):
                self.assertIn("error", self.execute(args))
        self.post.assert_not_called()
        self.assertTrue(self.execute(dict(ARGS, html="я" * 16384, caption="😀" * 512))["ok"])
        self.assertEqual(len(self.calls[0][1]["html"].encode()), 32768)

    def test_api_failure_does_not_claim_delivery_or_modify_context(self):
        self.post.side_effect = lambda *a, **kw: Mock(status_code=400, json=lambda: {
            "ok": False, "description": "Bad Request: widgets unavailable"})
        self.assertIn("Telegram отклонил", self.execute()["error"])
        self.assertEqual(self.ctx.sent, [])
        self.assertIsNone(self.ctx.last_message_id)
        self.assertIsNone(tg_history.lookup(123, 42))

    def test_message_without_widget_media_is_not_success(self):
        for media in (None, {}, {"html": ""}, {"html": 42}):
            with self.subTest(media=media):
                self.post.side_effect = lambda *a, **kw: Mock(status_code=200, json=lambda: {
                    "ok": True, "result": {"message_id": 42, "chat": {"id": 123}, "widget": media}})
                result = self.execute()
                self.assertIn("error", result)
                self.assertFalse(result["retryable"])
                self.assertEqual(self.ctx.sent, [])
                self.assertIsNone(self.ctx.last_message_id)

    def callback(self, **overrides):
        data = self.calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        cq = {"id": "click1", "data": data, "from": {"id": 123},
              "message": {"message_id": 42, "chat": {"id": 123}}}
        cq.update(overrides)
        return cq

    def test_fallback_click_downloads_source_after_restart_without_ai(self):
        self.execute()
        cq = self.callback()
        tg_bot.set_ctx(None)  # uses persisted state, not the assistant's turn
        widgets.handle_fallback("fake", cq, {}, tg_bot)
        self.assertEqual([m for m, _ in self.calls], ["sendWidget", "answerCallbackQuery", "sendDocument"])
        self.assertEqual(self.calls[-1][1]["chat_id"], 123)
        self.assertIn("обновите Telegram", self.calls[-1][1]["caption"])
        filename, data, mime = self.uploads[0]["document"]
        self.assertEqual((filename, mime), ("widget.html", "text/html"))
        self.assertIn(HTML, data.decode("utf-8"))

    def test_fallback_refuses_other_chat_message_user_expired_or_disabled(self):
        self.execute()
        cq = self.callback()
        for overrides, state in (({"from": {"id": 999}}, {}),
                ({"message": {"message_id": 42, "chat": {"id": 999}}}, {}),
                ({"message": {"message_id": 99, "chat": {"id": 123}}}, {}),
                ({"data": "twf:expired"}, {}), ({}, {widgets.SKILL_ID: False})):
            with self.subTest(overrides=overrides, state=state):
                widgets.handle_fallback("fake", dict(cq, **overrides), state, tg_bot)
                self.assertEqual(self.calls[-1][0], "answerCallbackQuery")
                self.assertEqual(self.uploads, [])

    def test_static_markup_before_script_is_accepted_but_head_only_is_not(self):
        source = '<!doctype html><html><head><style>p{color:red}</style></head><body><p>Привет</p><script>throw Error("oops")</script></body></html>'
        self.assertTrue(self.execute(dict(ARGS, html=source))["ok"])

    def test_model_loops_generate_then_send_widget_with_group_filter(self):
        for provider in ("openai", "claude"):
            with self.subTest(provider=provider):
                self.calls.clear()
                self.ctx.sent.clear()
                user = [{"role": "user", "content": "Покажи счётчик виджетом"}]
                if provider == "openai":
                    steps = [
                        {"content": "", "tool_calls": [{"id": "w1", "type": "function", "function": {
                            "name": "tg_send_widget", "arguments": json.dumps(ARGS)}}], "usage": {}, "effort": "", "truncated": False},
                        {"content": "Готово", "tool_calls": [], "usage": {}, "effort": "", "truncated": False}]
                    with patch.object(jarvis, "_openai_step", side_effect=steps) as model:
                        answer, _ = jarvis._assistant_loop_openai(user, "key", "m", self.app, "", groups=tool_groups.pick("счётчик"))
                    self.assertIn("tg_send_widget", str(model.call_args_list[0]))
                    self.assertIn('"ok": true', str(model.call_args_list[1]))
                else:
                    telegram_post = self.post.side_effect
                    model_payloads = []
                    def post(url, json=None, **kw):
                        if "api.telegram.org" in url:
                            return telegram_post(url, json=json, **kw)
                        model_payloads.append(json)
                        content = [{"type": "tool_use", "id": "w1", "name": "tg_send_widget", "input": ARGS}] if len(model_payloads) == 1 else [{"type": "text", "text": "Готово"}]
                        return Mock(ok=True, json=lambda: {"content": content, "usage": {}})
                    with patch.object(jarvis.requests, "post", side_effect=post):
                        answer, _ = jarvis._assistant_loop_claude(user, "key", "m", self.app, "", groups=tool_groups.pick("счётчик"))
                    self.assertIn("tg_send_widget", {t["name"] for t in model_payloads[0]["tools"]})
                    self.assertIn('"ok": true', str(model_payloads[-1]["messages"]))
                self.assertEqual(answer, "Готово")
                self.assertEqual([m for m, _ in self.calls], ["sendWidget"])
                self.assertEqual(len(self.ctx.sent), 1)


if __name__ == "__main__":
    unittest.main()
