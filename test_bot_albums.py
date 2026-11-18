"""Фотоальбом обрабатывается одним ходом, включая частичные ошибки скачивания."""
import base64
import unittest
from unittest.mock import Mock, patch

import jarvis
import tg_bot as bot


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


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

    def response(self, content, status=200, headers=None):
        return Mock(ok=status < 400, status_code=status, content=content, headers=headers or {})

    def test_updating_body_is_retried_and_not_returned(self):
        envelope = b'{"ok":false,"description":"Bad Request: file is updating"}'
        with patch.object(bot, "api", return_value={"file_path": "photos/a.jpg", "file_size": None}) as api, \
             patch.object(bot.requests, "get", side_effect=[
                 self.response(b"updating"), self.response(envelope), self.response(JPEG)]), \
             patch.object(bot.time, "sleep") as sleep, \
             patch("builtins.print") as printed:
            self.assertEqual(bot.download_file("secret-token", "id", expect_image=True), JPEG)
        self.assertEqual(api.call_count, 3)
        self.assertGreaterEqual(sleep.call_count, 2)
        blob = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn("updating", blob)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn(JPEG.decode("latin1"), blob)

    def test_real_json_and_long_text_are_not_placeholders(self):
        document = b'{"name": "Total Quartz", "ok": false}'
        note = b"updating the label text\n" + b"x" * 80
        self.assertFalse(bot._file_not_ready(document))
        self.assertFalse(bot._file_not_ready(note))
        self.assertTrue(bot._file_not_ready(b"updating"))
        with patch.object(bot, "api", return_value={"file_path": "documents/file_1.json"}), \
             patch.object(bot.requests, "get", return_value=self.response(document)), \
             patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot.download_file("token", "id"), document)
        sleep.assert_not_called()

    def test_missing_file_path_then_success(self):
        with patch.object(bot, "api", side_effect=[{}, {"file_path": "photos/a.jpg"}]) as api, \
             patch.object(bot.requests, "get", return_value=self.response(JPEG)), \
             patch.object(bot.time, "sleep"):
            self.assertEqual(bot.download_file("token", "id", expect_image=True), JPEG)
        self.assertEqual(api.call_count, 2)

    def test_retry_after_is_waited_and_invalid_file_id_is_not(self):
        temporary = bot.TgApiError("Bad Request: wrong file_id or the file is temporarily unavailable")
        temporary.transient = True
        temporary.retry_after = 8
        with patch.object(bot, "api", side_effect=[temporary, {"file_path": "photos/a.jpg"}]), \
             patch.object(bot.requests, "get", return_value=self.response(JPEG)), \
             patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot.download_file("token", "id", expect_image=True), JPEG)
        self.assertGreaterEqual(sleep.call_args.args[0], 8)
        self.assertLessEqual(sleep.call_args.args[0], 12)
        invalid = bot.TgApiError("Bad Request: invalid file_id")
        with patch.object(bot, "api", side_effect=invalid) as api, \
             patch.object(bot.requests, "get") as get, \
             patch.object(bot.time, "sleep") as sleep:
            with self.assertRaises(bot.TgApiError) as error:
                bot.download_file("token", "id")
        self.assertEqual(api.call_count, 1)
        get.assert_not_called()
        sleep.assert_not_called()
        self.assertNotIn("invalid", str(error.exception))

    def test_http_404_and_403_are_retried_path_is_quoted_token_stays_out(self):
        seen = []

        def get(url, timeout=None):
            seen.append(url)
            if len(seen) == 1:
                return self.response(b"", status=404)
            if len(seen) == 2:
                return self.response(b"", status=403, headers={"Retry-After": "2"})
            raise OSError("timed out " + url)

        with patch.object(bot, "api", return_value={"file_path": "photos/a b.jpg"}) as api, \
             patch.object(bot.requests, "get", side_effect=get), \
             patch.object(bot.time, "sleep") as sleep, \
             patch("builtins.print") as printed:
            with self.assertRaises(bot.TgApiError) as error:
                bot.download_file("secret-token", "id")
        self.assertEqual(api.call_count, bot.DOWNLOAD_ATTEMPTS)
        self.assertIn("photos/a%20b.jpg", seen[0])
        self.assertNotIn("a b.jpg", seen[0])
        self.assertIn("secret-token", seen[0])
        self.assertNotIn("secret-token", str(error.exception))
        self.assertGreaterEqual(sleep.call_args_list[1].args[0], 2)
        blob = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("a%20b", blob)

    def test_content_length_over_limit_returns_none_without_using_body(self):
        body = JPEG + b"\x00" * 32

        class Response:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(bot.MAX_DOWNLOAD + 1)}

            @property
            def content(self):
                raise AssertionError("тело больше лимита не читается")

        with patch.object(bot, "api", return_value={"file_path": "photos/a.jpg", "file_size": 10}), \
             patch.object(bot.requests, "get", return_value=Response()), \
             patch.object(bot.time, "sleep"):
            self.assertIsNone(bot.download_file("token", "id"))
        self.assertTrue(body.startswith(JPEG[:3]))

    def test_api_classifies_temporary_getfile_without_leaking_token(self):
        seen = []

        def post(url, json=None, timeout=None):
            seen.append(url)
            if json and json.get("file_id") == "temp":
                return Mock(status_code=400, json=Mock(return_value={
                    "ok": False, "description": "Bad Request: wrong file_id or the file is temporarily unavailable",
                    "parameters": {"retry_after": 8}}))
            if json and json.get("file_id") == "big":
                return Mock(status_code=400, json=Mock(return_value={
                    "ok": False, "description": "Bad Request: file is too big"}))
            raise OSError("HTTPSConnectionPool host=api.telegram.org url=" + url)

        with patch.object(bot.requests, "post", side_effect=post):
            with self.assertRaises(bot.TgApiError) as temporary:
                bot.api("secret-token", "getFile", {"file_id": "temp"})
            with self.assertRaises(bot.TgApiError) as big:
                bot.api("secret-token", "getFile", {"file_id": "big"})
            with self.assertRaises(bot.TgApiError) as network:
                bot.api("secret-token", "getFile", {"file_id": "net"})
        self.assertTrue(all("secret-token" in url for url in seen))
        self.assertTrue(temporary.exception.transient)
        self.assertEqual(temporary.exception.retry_after, 8)
        self.assertTrue(big.exception.too_big)
        self.assertFalse(big.exception.transient)
        self.assertTrue(network.exception.transient)
        for error in (temporary, big, network):
            self.assertNotIn("secret-token", str(error.exception))

    def test_photo_uses_next_size_when_largest_is_not_ready(self):
        bot._recent_files.clear()
        sizes = [
            {"file_id": "thumb", "file_unique_id": "nope", "file_size": 900, "width": 90, "height": 120},
            {"file_id": "full", "file_unique_id": "also-nope", "file_size": 250000, "width": 1280, "height": 1700},
        ]
        calls = []

        def get_file(token, method, payload=None, timeout=None):
            calls.append(payload["file_id"])
            self.assertEqual(timeout, bot._GETFILE_TIMEOUT)
            if payload["file_id"] == "full":
                err = bot.TgApiError("Bad Request: wrong file_id or the file is temporarily unavailable")
                err.transient = True
                raise err
            return {"file_path": "photos/thumb.jpg", "file_size": 900}

        msg = {"message_id": 11, "chat": {"id": 42}, "caption": "Изучи это масло", "photo": sizes}
        with patch.object(bot, "api", side_effect=get_file), \
             patch.object(bot.requests, "get", return_value=self.response(JPEG)), \
             patch.object(bot.time, "sleep") as sleep:
            text, images, error = bot.extract_attachments("token", msg)
        self.assertEqual(calls, ["full", "thumb"])
        self.assertNotIn("nope", calls)
        self.assertEqual(text, "Изучи это масло")
        self.assertIsNone(error)
        self.assertEqual(base64.b64decode(images[0]["b64"]), JPEG)
        sleep.assert_not_called()
        self.assertEqual(bot.recent_photos(42), [("photo_11.jpg", JPEG)])

    def test_oversize_largest_falls_back_and_html_body_is_not_a_photo(self):
        huge = {"file_id": "huge", "file_size": bot.MAX_DOWNLOAD + 10, "width": 4000, "height": 3000}
        mid = {"file_id": "mid", "file_size": 5000, "width": 800, "height": 600}
        calls = []

        def get_file(token, method, payload=None, timeout=None):
            calls.append(payload["file_id"])
            return {"file_path": "photos/" + payload["file_id"] + ".jpg"}

        with patch.object(bot, "api", side_effect=get_file), \
             patch.object(bot.requests, "get", side_effect=[
                 self.response(b"<html>updating</html>"), self.response(JPEG)]), \
             patch.object(bot.time, "sleep") as sleep:
            data = bot.download_photo("token", [huge, mid])
        self.assertEqual(data, JPEG)
        self.assertEqual(calls, ["mid", "mid"])
        self.assertNotIn("huge", calls)
        sleep.assert_called()

    def test_getfile_too_big_uses_smaller_photo_without_waiting(self):
        def get_file(token, method, payload=None, timeout=None):
            if payload["file_id"] == "full":
                err = bot.TgApiError("Bad Request: file is too big")
                err.too_big = True
                raise err
            return {"file_path": "photos/mid.jpg"}

        sizes = [
            {"file_id": "full", "file_size": 100, "width": 2000, "height": 2000},
            {"file_id": "mid", "file_size": 100, "width": 800, "height": 800},
        ]
        with patch.object(bot, "api", side_effect=get_file), \
             patch.object(bot.requests, "get", return_value=self.response(JPEG)), \
             patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot.download_photo("token", sizes), JPEG)
        sleep.assert_not_called()

    def test_failed_photo_without_caption_is_the_download_error(self):
        msg = {"message_id": 8, "chat": {"id": 7}, "date": 100,
               "photo": [{"file_id": "id", "file_size": 20, "width": 40, "height": 40}]}
        with patch.object(jarvis, "load_app_data", return_value={}), \
             patch.object(bot, "api", side_effect=bot.TgApiError("Bad Request: invalid file_id")), \
             patch.object(bot.time, "sleep") as sleep, \
             patch("builtins.print") as printed, \
             patch.object(jarvis, "handle_assistant_message") as assistant, \
             patch.object(bot, "send_text") as send:
            jarvis._dispatch_user_message("secret-token", 7, msg)
        assistant.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], "Не удалось скачать вложение.")
        sleep.assert_not_called()
        blob = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn("secret-token", blob)
        self.assertIn("TgApiError", blob)

    def test_captioned_photo_survives_updating_race(self):
        msg = {"message_id": 9, "chat": {"id": 7}, "date": 100, "caption": "Изучи это масло",
               "photo": [{"file_id": "full", "file_size": 30000, "width": 1000, "height": 1400}]}
        with patch.object(jarvis, "load_app_data", return_value={}), \
             patch.object(bot, "api", return_value={"file_path": "photos/oil.jpg"}), \
             patch.object(bot.requests, "get", side_effect=[
                 self.response(b"updating"), self.response(JPEG)]), \
             patch.object(bot.time, "sleep"), \
             patch("builtins.print") as printed, \
             patch.object(jarvis, "handle_assistant_message") as assistant, \
             patch.object(bot, "send_text") as send:
            jarvis._dispatch_user_message("secret-token", 7, msg)
        send.assert_not_called()
        assistant.assert_called_once()
        text, images = assistant.call_args.args[2], assistant.call_args.args[3]
        self.assertIn("Изучи это масло", text)
        self.assertNotIn("не прочитано", text)
        self.assertNotIn("не удалось", text.lower())
        self.assertEqual(base64.b64decode(images[0]["b64"]), JPEG)
        blob = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn("updating", blob)
        self.assertNotIn("secret-token", blob)


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
