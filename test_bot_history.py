"""Persistent bot history, replies and live reads, with no real Telegram/AI calls."""
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import jarvis
import tg_bot as bot
import tg_history as archive
import tool_groups


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = {"telegramSkills": {}}
        self.patches = [
            patch.dict(os.environ, {"DATA_DIR": self.tmp.name}),
            patch.object(jarvis, "load_app_data", side_effect=lambda: self.app),
            patch.object(archive, "get_state", side_effect=lambda: self.app["telegramSkills"]),
            patch.object(bot, "ANCHORS_FILE", Path(self.tmp.name) / "anchors.json"),
            patch.object(bot, "DATA_DIR", Path(self.tmp.name)),
            patch.object(jarvis, "tg_user", types.SimpleNamespace(is_owner_chat=lambda cid: False)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        for cid in (100, 200):
            bot.reset(cid)
        bot.set_ctx(None)
        archive.set_dialog()
        self.addCleanup(bot.set_ctx, None)
        self.addCleanup(archive.set_dialog)

    def put(self, mid, text, role="user", chat=100, dialog="one", title="Заказы", reply_to=None):
        archive.record(chat, mid, role, text, dialog=dialog, title=title, reply_to=reply_to)

    def test_catalog_tools_prompt_and_execution_respect_toggle(self):
        sk = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == archive.SKILL_ID)
        self.assertTrue(sk["default"])
        self.assertIn("tg_bot_history", {t["name"] for t in jarvis._assistant_tool_list(self.app, False)})
        self.assertIn("tg_bot_history", jarvis.build_assistant_system_prompt(self.app))
        self.assertEqual(tool_groups.group_of("tg_bot_history"), "bot_history")
        self.assertIn("bot_history", tool_groups.pick("вспомни, о чём мы говорили"))
        self.app["telegramSkills"][archive.SKILL_ID] = False
        self.assertNotIn("tg_bot_history", {t["name"] for t in jarvis._assistant_tool_list(self.app, False)})
        self.assertNotIn("tg_bot_history", jarvis.build_assistant_system_prompt(self.app))
        bot.set_ctx(bot.Ctx("fake", 100, 9))
        result = jarvis.execute_assistant_tool("tg_bot_history", {}, self.app, "")
        self.assertIn("выключен", result["error"])
        self.put(1, "Не сохранять")
        self.assertFalse(archive.store_path().exists())

    def test_search_cyrillic_literal_wildcards_pagination_and_chat_scope(self):
        for mid, text in enumerate(("КНИГА стоит 755", "100%_скидка", "Третье сообщение", "Четвёртое сообщение"), 1):
            self.put(mid, text)
        self.put(1, "Чужой секрет", chat=200)
        self.assertEqual(archive.read_archive(100, "книга")["messages"][0]["text"], "КНИГА стоит 755")
        self.assertEqual(len(archive.read_archive(100, "%_")["messages"]), 1)
        page = archive.read_archive(100, limit=2)
        self.assertEqual([m["mid"] for m in page["messages"]], [3, 4])
        older = archive.read_archive(100, limit=2, before_id=page["before_id"])
        self.assertEqual([m["mid"] for m in older["messages"]], [1, 2])
        self.assertNotIn("Чужой", json.dumps(page, ensure_ascii=False))
        self.assertIn("error", archive.execute({"chat_id": 200}, {}, bot.Ctx("fake", 100, 1)))
        self.assertIn("error", archive.execute({}, {}, None))

    def test_context_uses_dialogue_and_keeps_target(self):
        self.put(1, "Давай обсудим WB")
        self.put(2, "Цена 755", "assistant")
        self.put(3, "Другой вопрос", dialog="two")
        self.put(4, "К теме WB", reply_to=2)
        self.assertEqual([m["mid"] for m in archive.context(100, 2)], [1, 2, 4])
        self.assertEqual(archive.context(200, 2), [])

    def test_bounded_results_do_not_drop_messages_or_break_pages(self):
        for mid in range(1, 61):
            self.put(mid, "Большой текст " * 2000)
        result = archive.read_archive(100, limit=60)
        self.assertEqual(len(result["messages"]), 60)
        self.assertLessEqual(sum(len(m["text"]) for m in result["messages"]), archive.MAX_CONTEXT)
        self.assertTrue(all(m["truncated"] for m in result["messages"]))

    def test_record_update_and_backup_snapshot(self):
        self.put(1, "Исходное")
        self.put(1, "Исправлено", dialog="")
        self.assertEqual(archive.lookup(100, 1)["dialog"], "one")
        self.assertEqual(archive.lookup(100, 1)["text"], "Исправлено")
        self.assertEqual(archive.store_path().stat().st_mode & 0o777, 0o600)
        snapshot = archive.backup_bytes()
        con = sqlite3.connect(":memory:")
        try:
            con.deserialize(snapshot)
            self.assertEqual(con.execute("SELECT text FROM messages").fetchone()[0], "Исправлено")
        finally:
            con.close()
        with patch.object(jarvis, "DATA_DIR", Path(self.tmp.name)):
            blob = jarvis.build_backup_zip()
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertTrue(zf.read("data/" + archive.STORE_NAME).startswith(b"SQLite format 3"))
            self.assertNotIn("code/" + archive.STORE_NAME, zf.namelist())

    def test_outgoing_text_files_background_and_no_binary_credentials(self):
        self.put(1, "Начало")
        archive.set_dialog(100, "one", "Заказы")
        result = {"message_id": 2, "chat": {"id": 100}, "date": 1794110000,
                  "document": {"file_name": "заказ.pdf", "file_id": "SECRET-BINARY-ID"}, "caption": "Отчёт WB"}
        reply = Mock()
        reply.json.return_value = {"ok": True, "result": result}
        with patch.object(bot.requests, "post", return_value=reply):
            bot.api("SECRET-TOKEN", "sendDocument", {"chat_id": 100})
        row = archive.lookup(100, 2)
        self.assertIn("заказ.pdf", row["text"])
        self.assertEqual(row["dialog"], "one")
        self.assertNotIn(b"SECRET", archive.store_path().read_bytes())
        archive.set_dialog()
        archive.record_api_result("sendMessage", {"message_id": 3, "chat": {"id": 100}, "text": "Фоновый отчёт"})
        self.assertEqual(archive.lookup(100, 3)["dialog"], "")

    def test_reply_text_caption_quote_and_foreign_chat(self):
        reply = {"message_id": 5, "chat": {"id": 100, "type": "private"}, "from": {"is_bot": True, "first_name": "Jarvis"},
                 "text": "Цена товара 755 ₽", "date": 1794110000}
        block, images = jarvis._prepare_bot_reply("fake", 100, reply, {"text": "755 ₽"}, {})
        self.assertIn("Цена товара 755 ₽", block)
        self.assertIn('"selected_quote": "755 ₽"', block)
        self.assertEqual(images, [])
        self.assertEqual(archive.lookup(100, 5)["role"], "assistant")
        bad, _ = jarvis._prepare_bot_reply("fake", 200, reply, None, {})
        self.assertEqual(bad, "")
        self.assertIsNone(archive.lookup(200, 5))

    def test_reply_image_is_read_even_if_caption_already_archived(self):
        self.put(5, "Подпись к фото", "assistant")
        image = {"media_type": "image/jpeg", "b64": "ZmFrZQ=="}
        reply = {"message_id": 5, "chat": {"id": 100, "type": "private"}, "caption": "Подпись к фото", "photo": [{}]}
        with patch.object(bot, "extract_attachments", return_value=("Подпись к фото", [image], "")) as extract:
            block, images = jarvis._prepare_bot_reply("fake", 100, reply, None, {})
        extract.assert_called_once()
        self.assertEqual(images, [image])
        self.assertIn("Подпись", block)

    def test_reply_reads_edited_message_not_stale_cache(self):
        self.put(5, "Старая цена", "assistant")
        block, _ = jarvis._prepare_bot_reply("fake", 100, {"message_id": 5, "text": "Исправленная цена 489 ₽"}, None, {})
        self.assertIn("Исправленная цена 489 ₽", block)
        self.assertNotIn("Старая цена", block)
        self.assertEqual(archive.lookup(100, 5)["text"], "Исправленная цена 489 ₽")

    def test_reply_transcribes_voice_and_reuses_saved_transcript(self):
        reply = {"message_id": 5, "voice": {"file_id": "not-stored"}}
        transcript = "[Голосовое сообщение — расшифровка]\nЗакажи книгу"
        with patch.object(bot, "extract_attachments", return_value=(transcript, [], "")) as extract:
            block, _ = jarvis._prepare_bot_reply("fake", 100, reply, None, {})
            self.assertIn("Закажи книгу", block)
            jarvis._prepare_bot_reply("fake", 100, reply, None, {})
        extract.assert_called_once()

    def test_legacy_anchor_is_restored_after_ttl_under_chat_lock(self):
        bot.seed_dialog(100, "Старый отчёт", "Задача", "Результат")
        bot.remember_anchor(100, [50], "Старый отчёт", "Задача", "Результат")
        bot._convo_meta[100]["ts"] = 1
        with bot.chat_lock(100):
            block, _ = jarvis._prepare_bot_reply("fake", 100, {"message_id": 50, "text": "Результат"}, None, {})
        self.assertEqual(bot.history_for(100)[0]["content"], "Задача")
        self.assertIn("Результат", block)

    def test_background_notice_starts_its_own_persistent_discussion(self):
        self.put(5, "WB: можно забирать", "assistant", dialog="", title="")
        bot.seed_dialog(100, "Другая тема", "Погода", "Дождь")
        jarvis._prepare_bot_reply("fake", 100, {"message_id": 5, "text": "WB: можно забирать"}, None, {})
        first = json.dumps(bot.history_for(100), ensure_ascii=False)
        self.assertIn("WB: можно забирать", first)
        self.assertNotIn("Погода", first)
        root = archive.lookup(100, 5)["dialog"]
        self.assertTrue(root)
        self.put(6, "А сколько стоит?", dialog=root, reply_to=5)
        self.put(7, "755 ₽", "assistant", dialog=root)
        bot.reset(100)
        jarvis._prepare_bot_reply("fake", 100, {"message_id": 5, "text": "WB: можно забирать"}, None, {})
        self.assertIn("А сколько стоит?", json.dumps(bot.history_for(100), ensure_ascii=False))

    def test_live_history_fixed_bot_peer_owner_and_distinct_ids(self):
        self.put(900, "Локальный id, не связанный с Telegram")
        remote = Mock(return_value={"messages": [{"id": 900, "from": "я", "text": "История из Telegram", "date": "2026-11-08 10:00"}], "older_than_id": 900})
        tools = types.SimpleNamespace(read_history=remote, search_messages=Mock(return_value={"messages": []}))
        with patch.object(jarvis, "tg_user", types.SimpleNamespace(is_owner_chat=lambda cid: cid == 100)), \
                patch.object(jarvis, "tg_tools", tools), \
                patch.object(bot, "api", return_value={"username": "my_jarvis_bot"}):
            ctx = bot.Ctx("fake", 100, 901)
            result = archive.execute({"source": "telegram", "before_id": 1000}, {}, ctx, jarvis._bot_history_remote)
            self.assertEqual(result["source"], "telegram")
            self.assertEqual(result["messages"][0]["text"], "История из Telegram")
            remote.assert_called_once_with(peer="@my_jarvis_bot", limit=30, offset_id=1000, before_timestamp=0)
            self.assertEqual(archive.lookup(100, 900)["text"], "Локальный id, не связанный с Telegram")
            remote.reset_mock()
            result = archive.execute({"source": "telegram"}, {}, bot.Ctx("fake", 200, 901), jarvis._bot_history_remote)
            self.assertIn("error", result)
            remote.assert_not_called()

    def test_live_failure_auto_falls_back_and_search_stays_scoped(self):
        self.put(1, "Резервная история")
        result = archive.execute({}, {}, bot.Ctx("fake", 100, 2), lambda *_: {"ok": False, "error": "Недоступно"})
        self.assertEqual(result["source"], "archive")
        self.assertEqual(result["warning"], "Недоступно")
        result = archive.execute({"query": "Резервная"}, {}, bot.Ctx("fake", 100, 2), lambda *_: {"ok": True, "messages": []})
        self.assertEqual(result["source"], "archive")
        self.assertTrue(result["messages"])
        search = Mock(return_value={"messages": []})
        with patch.object(jarvis, "tg_user", types.SimpleNamespace(is_owner_chat=lambda _: True)), \
                patch.object(jarvis, "tg_tools", types.SimpleNamespace(search_messages=search)), \
                patch.object(bot, "api", return_value={"username": "my_jarvis_bot"}):
            jarvis._bot_history_remote(bot.Ctx("fake", 100, 2), {"query": "755", "limit": 10})
        search.assert_called_once_with(peer="@my_jarvis_bot", query="755", limit=10)

    def test_old_reply_matches_live_text_and_date_not_bot_api_id(self):
        reply = {"message_id": 10, "text": "Цена 755", "date": 1794110000}
        stamp = jarvis.datetime.fromtimestamp(reply["date"], jarvis.MSK).strftime("%Y-%m-%d %H:%M")
        live = {"ok": True, "messages": [
            {"mid": 500, "role": "user", "text": "Заказ WB", "date": stamp},
            {"mid": 600, "role": "assistant", "text": "Цена 755", "date": stamp}]}
        with patch.object(jarvis, "_bot_history_remote", return_value=live):
            jarvis._prepare_bot_reply("fake", 100, reply, None, {})
        self.assertEqual(bot.history_for(100)[0]["content"], "Заказ WB")
        self.assertIsNone(archive.lookup(100, 600))


class ConversationTest(unittest.TestCase):
    def setUp(self):
        HistoryTest.setUp(self)
        conf = jarvis._AiConf("openai", "fake-key", "test-model", "", "", "main")
        self.mid, self.turns, self.sent = 1, [], []
        def api(token, method, payload=None, **kwargs):
            if method == "sendChatAction":
                return True
            if method == "sendMessage":
                self.mid += 1
                self.sent.append((self.mid, payload["text"]))
                return {"message_id": self.mid}
            raise AssertionError("Unexpected Telegram method " + method)
        def answer(messages, *args, **kwargs):
            self.turns.append(list(messages))
            return "Цена 755 ₽; можно забирать.", []
        for p in (
            patch.object(jarvis, "_ai_conf", return_value=conf),
            patch.object(jarvis, "_start_conf", return_value=(conf, False, "")),
            patch.object(jarvis, "_ai_models_cfg", return_value={}),
            patch.object(jarvis, "generate_dialog_title", return_value="Заказ WB"),
            patch.object(jarvis, "_record_turn"),
            patch.object(jarvis, "_assistant_loop_openai", side_effect=answer),
            patch.object(bot, "api", side_effect=api),
        ):
            p.start()
            self.addCleanup(p.stop)

    def send(self, text, reply=None, quote=None):
        self.mid += 1
        jarvis.handle_assistant_message("fake", 100, text, message_id=self.mid, reply_message=reply, reply_quote=quote)

    def test_reply_restores_ordinary_full_discussion_after_restart(self):
        self.send("Проверь заказ книги на WB")
        self.send("А какая цена?")
        reply_id, reply_text = self.sent[-1]
        bot.reset(100)  # also models a lost in-memory context on restart
        self.send("Другая тема: погода")
        bot.reset(100)
        self.send("Что это за товар?", reply={"message_id": reply_id, "text": bot.strip_tags(reply_text),
                  "chat": {"id": 100, "type": "private"}}, quote={"text": "755 ₽"})
        final = json.dumps(self.turns[-1], ensure_ascii=False)
        self.assertIn("Проверь заказ книги на WB", final)
        self.assertIn("А какая цена?", final)
        self.assertIn("755 ₽", final)
        self.assertIn("Что это за товар?", final)
        self.assertNotIn("Другая тема: погода", final)
        self.assertIn("selected_quote", final)
        self.assertIsNone(getattr(archive._tls, "scope", None))

    def test_dispatch_preserves_reply_and_quote_and_original_user_text(self):
        msg = {"message_id": 20, "text": "Поясни", "reply_to_message": {"message_id": 10, "caption": "Старая подпись"},
               "quote": {"text": "подпись"}}
        with patch.object(bot, "extract_attachments", return_value=("Поясни", [], "")):
            jarvis._dispatch_user_message("fake", 100, msg)
        final = json.dumps(self.turns[-1], ensure_ascii=False)
        self.assertIn("Старая подпись", final)
        self.assertIn("selected_quote", final)
        self.assertEqual(archive.lookup(100, 20)["text"], "Поясни")
        self.assertEqual(archive.lookup(100, 20)["reply_to"], 10)

    def test_disabled_does_not_restore_or_expose_quoted_text(self):
        self.app["telegramSkills"][archive.SKILL_ID] = False
        self.send("Новый запрос", reply={"message_id": 10, "text": "Не читать"})
        self.assertNotIn("Не читать", json.dumps(self.turns[-1], ensure_ascii=False))
        self.assertFalse(archive.store_path().exists())


if __name__ == "__main__":
    unittest.main()
