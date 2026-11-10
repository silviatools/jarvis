"""Фотоальбом обрабатывается одним ходом, включая частичные ошибки скачивания."""
import unittest
from unittest.mock import Mock, patch

import jarvis
import tg_bot as bot


class AlbumTests(unittest.TestCase):
    def test_buffer_orders_deduplicates_and_waits_across_batches(self):
        done = Mock()
        buffer = bot.MediaGroupBuffer(done)
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
            buffer._flush(("token", 1, "album"))
        done.assert_not_called()
        with patch.object(bot.threading, "current_thread", return_value=timers[1]):
            buffer._flush(("token", 1, "album"))
            buffer._flush(("token", 1, "album"))
        done.assert_called_once()
        self.assertEqual([m["message_id"] for m in done.call_args.args[2]], [10, 12])
        self.assertFalse(buffer.pending)

    def test_same_album_id_in_different_chats_and_bots_is_isolated(self):
        buffer = bot.MediaGroupBuffer(Mock())
        with patch.object(bot.threading, "Timer"):
            for token, chat in (("a", 1), ("a", 2), ("b", 1)):
                buffer.add(token, chat, {"media_group_id": "album", "message_id": 1})
        self.assertEqual(len(buffer.pending), 3)

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


if __name__ == "__main__":
    unittest.main()
