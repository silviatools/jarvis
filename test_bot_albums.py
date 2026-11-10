"""Фотоальбом обрабатывается одним ходом, включая частичные ошибки скачивания."""
import unittest
from unittest.mock import Mock, patch

import jarvis
import tg_bot as bot


class AlbumTests(unittest.TestCase):
    def test_buffer_orders_deduplicates_and_waits_across_batches(self):
        done = Mock()
        buffer = bot.MessageBatchBuffer(done)
        timers = []

        def timer(*args, **kwargs):
            obj = Mock()
            timers.append(obj)
            return obj

        with patch.object(bot.threading, "Timer", side_effect=timer):
            buffer.add("token", 1, {"media_group_id": "album", "message_id": 12})
            buffer.add("token", 1, {"media_group_id": "album", "message_id": 10, "caption": "Все ответы"})
            buffer.add("token", 1, {"media_group_id": "album", "message_id": 10})
        done.assert_not_called()
        self.assertEqual(len(timers), 2)
        timers[0].cancel.assert_called_once()
        with patch.object(bot.threading, "current_thread", return_value=timers[0]):
            buffer._flush(("token", 1, None))
        done.assert_not_called()
        with patch.object(bot.threading, "current_thread", return_value=timers[1]):
            buffer._flush(("token", 1, None))
            buffer._flush(("token", 1, None))
        done.assert_called_once()
        self.assertEqual([m["message_id"] for m in done.call_args.args[2]], [10, 12])
        self.assertFalse(buffer.pending)

    def test_same_album_id_in_different_chats_and_bots_is_isolated(self):
        buffer = bot.MessageBatchBuffer(Mock())
        with patch.object(bot.threading, "Timer"):
            for token, chat in (("a", 1), ("a", 2), ("b", 1)):
                buffer.add(token, chat, {"media_group_id": "album", "message_id": 1})
        self.assertEqual(len(buffer.pending), 3)

    def test_different_senders_in_group_are_isolated(self):
        buffer = bot.MessageBatchBuffer(Mock())
        with patch.object(bot.threading, "Timer"):
            for sender in (10, 20):
                buffer.add("token", 1, {"from": {"id": sender}, "message_id": sender})
        self.assertEqual(len(buffer.pending), 2)

    def test_reset_cancels_pending_messages_only_in_target_chat(self):
        done = Mock()
        buffer = bot.MessageBatchBuffer(done)
        with patch.object(bot.threading, "Timer"):
            buffer.add("token", 1, {"message_id": 1, "text": "Старый вопрос"})
            timer = buffer.pending[("token", 1, None)]["timer"]
            buffer.add("token", 2, {"message_id": 2, "text": "Другой чат"})
        buffer.cancel("token", 1)
        timer.cancel.assert_called_once()
        with patch.object(bot.threading, "current_thread", return_value=timer):
            buffer._flush(("token", 1, None))
        done.assert_not_called()
        self.assertEqual(list(buffer.pending), [("token", 2, None)])

    def dispatch(self, outcomes):
        messages = [{"message_id": i, "chat": {"id": 1}, "date": 100,
                     "caption": "Дай все ответы" if i == 1 else ""} for i in range(1, 5)]
        with patch.object(jarvis, "load_app_data", return_value={}), \
             patch.object(bot, "api"), \
             patch.object(bot, "extract_attachments", side_effect=outcomes), \
             patch.object(jarvis, "handle_assistant_message") as assistant, \
             patch.object(bot, "send_text") as send:
            jarvis._dispatch_user_message("token", 1, messages)
        return assistant, send

    def test_four_photos_and_caption_make_one_assistant_call(self):
        outcomes = [("Дай все ответы" if i == 0 else "", [{"b64": str(i), "media_type": "image/jpeg"}], None)
                    for i in range(4)]
        assistant, send = self.dispatch(outcomes)
        assistant.assert_called_once()
        args = assistant.call_args.args
        self.assertIn("Дай все ответы", args[2])
        self.assertEqual(len(args[3]), 4)
        self.assertEqual(args[4], 1)
        send.assert_not_called()

    def test_partial_download_failure_keeps_other_photos_in_one_turn(self):
        assistant, send = self.dispatch([
            ("Дай все ответы", [{"b64": "a"}], None), RuntimeError("download"),
            ("", [{"b64": "b"}], None), ("", [{"b64": "c"}], None)])
        assistant.assert_called_once()
        self.assertEqual(len(assistant.call_args.args[3]), 3)
        self.assertIn("Вложение 2", assistant.call_args.args[2])
        send.assert_not_called()

    def test_all_downloads_fail_send_one_error_without_model_call(self):
        assistant, send = self.dispatch([RuntimeError("download")] * 4)
        assistant.assert_not_called()
        send.assert_called_once()


class DownloadTests(unittest.TestCase):
    def test_temporary_failure_retries_get_file_and_download(self):
        with patch.object(bot, "api", return_value={"file_path": "photos/a.jpg"}) as api, \
             patch.object(bot.requests, "get", side_effect=[OSError("timeout"), Mock(ok=True, content=b"image")]), \
             patch.object(bot.time, "sleep"):
            self.assertEqual(bot.download_file("secret", "id"), b"image")
        self.assertEqual(api.call_count, 2)

    def test_download_error_never_exposes_token(self):
        with patch.object(bot, "api", side_effect=RuntimeError("secret-token")), \
             patch.object(bot.time, "sleep"):
            with self.assertRaises(bot.TgApiError) as error:
                bot.download_file("secret-token", "id")
        self.assertNotIn("secret-token", str(error.exception))

    def test_oversize_is_not_downloaded(self):
        with patch.object(bot, "api") as api:
            self.assertIsNone(bot.download_file("token", "id", bot.MAX_DOWNLOAD + 1))
        api.assert_not_called()


class MessageBatchIntegrationTests(unittest.TestCase):
    def run_batches(self, batches):
        buffer = bot.MessageBatchBuffer(jarvis._dispatch_user_message)
        responses = [Mock(ok=True, json=Mock(return_value={"result": [
            {"update_id": m["message_id"], "message": m} for m in batch]})) for batch in batches]
        subs = {"offset": 0, "chat_ids": [1]}
        with patch.object(jarvis, "_bot_message_batches", buffer), \
             patch.object(jarvis, "get_token", side_effect=["token"] * len(batches) + [KeyboardInterrupt]), \
             patch.object(jarvis, "load_subscribers", return_value=subs), \
             patch.object(jarvis, "save_subscribers"), \
             patch.object(jarvis, "handle_diet_custom_reply", return_value=False), \
             patch.object(jarvis.requests, "get", side_effect=responses), \
             patch.object(bot.threading, "Timer"), \
             patch.object(bot, "api"), \
             patch.object(bot, "remember_file"), \
             patch.object(bot, "download_file", return_value=b"image"), \
             patch.object(jarvis, "load_app_data", return_value={}), \
             patch.object(jarvis, "handle_assistant_message") as assistant:
            with self.assertRaises(KeyboardInterrupt):
                jarvis.updates_loop()
            assistant.assert_not_called()
            self.assertEqual(len(buffer.pending), 1)
            key, entry = next(iter(buffer.pending.items()))
            with patch.object(bot.threading, "current_thread", return_value=entry["timer"]):
                buffer._flush(key)
            assistant.assert_called_once()
        return assistant.call_args

    def message(self, mid, **kwargs):
        return {"message_id": mid, "chat": {"id": 1}, "from": {"id": 10}, "date": 100, **kwargs}

    def test_separate_text_messages_across_polls_form_one_request(self):
        call = self.run_batches([
            [self.message(1, text="Составь план")],
            [self.message(2, text="на завтра"), self.message(3, text="с учётом учёбы")]])
        text = call.args[2]
        self.assertLess(text.index("Составь план"), text.index("на завтра"))
        self.assertIn("с учётом учёбы", text)
        self.assertNotIn("альбом", text)
        self.assertFalse(call.args[3])

    def test_forwarded_photo_album_and_separate_instruction_share_one_turn(self):
        photo = [{"file_id": "file", "file_size": 10}]
        call = self.run_batches([
            [self.message(1, photo=photo, media_group_id="a",
                          forward_origin={"type": "hidden_user", "sender_user_name": "Преподаватель"})],
            [self.message(2, photo=photo, media_group_id="a"),
             self.message(3, text="Дай ответы на оба вопроса", reply_to_message={"message_id": 50}),
             self.message(4, text="И объясни почему")]])
        self.assertEqual(len(call.args[3]), 2)
        self.assertIn("Преподаватель", call.args[2])
        self.assertIn("Дай ответы на оба вопроса", call.args[2])
        self.assertIn("И объясни почему", call.args[2])
        self.assertEqual(call.kwargs["reply_message"], {"message_id": 50})

    def test_single_message_still_dispatches_after_pause(self):
        call = self.run_batches([[self.message(1, text="Привет")]])
        self.assertEqual(call.args[2], "Привет")


if __name__ == "__main__":
    unittest.main()
