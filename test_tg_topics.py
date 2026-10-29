"""Темы форумов и перенос сообщений: подмена клиента Telethon — без настоящего Telegram."""

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import tg_tools  # noqa: E402
from telethon import types  # noqa: E402
from telethon.tl import functions  # noqa: E402


class Msg:
    """Сообщение с теми же полями, что читает tg_tools."""
    def __init__(self, id, day, text="", kind="text", gid=None, action=None, sender=None, entities=None):
        self.id, self.message, self.grouped_id, self.action = id, text, gid, action
        self.date = datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc)
        self.kind, self.sender, self.entities = kind, sender, entities or []
        self.media = None if kind == "text" else object()
        self.noforwards = False

    def __getattr__(self, name):
        if name in ("photo", "video", "voice", "video_note", "audio", "sticker", "gif", "poll", "document",
                    "geo", "contact", "web_preview", "pinned", "fwd_from", "reply_to", "reactions",
                    "views", "forwards", "replies", "edit_date", "file", "mentioned", "out"):
            real = {"photo": "photo", "video": "video", "voice": "voice", "file": "file"}.get(self.kind)
            if name == "document":
                return bool(self.kind in ("file", "video", "voice")) or None
            return True if name == real else None
        raise AttributeError(name)


class Fake:
    """Клиент Telethon: запоминает вызовы."""
    def __init__(self, messages, topics):
        self.messages, self.topics = messages, topics
        self.sent, self.files, self.requests, self.iter_kw = [], [], [], []

    async def iter_messages(self, ent, **kw):
        self.iter_kw.append(kw)
        for m in self.messages:
            yield m

    async def send_message(self, dst, message, **kw):
        self.sent.append((message, kw))

    async def send_file(self, dst, file, **kw):
        self.files.append((file, kw))

    async def __call__(self, req):
        self.requests.append(req)
        if isinstance(req, functions.messages.GetForumTopicsRequest):
            return types.messages.ForumTopics(
                count=len(self.topics), topics=self.topics, messages=[], chats=[], users=[], pts=0)
        return None


def topic(id, title, pinned=False):
    return types.ForumTopic(id=id, date=datetime.now(timezone.utc), peer=types.PeerChannel(1), title=title,
                            icon_color=0, top_message=id, read_inbox_max_id=0, read_outbox_max_id=0,
                            unread_count=2, unread_mentions_count=0, unread_reactions_count=0,
                            unread_poll_votes_count=0, from_id=types.PeerUser(1),
                            notify_settings=types.PeerNotifySettings(), pinned=pinned or None)


def channel(id, title, forum=True, noforwards=False):
    return types.Channel(id=id, title=title, photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc),
                         megagroup=True, forum=forum or None, noforwards=noforwards or None, access_hash=1)


class TopicsTest(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = channel(1, "Склад - Смирновская"), channel(2, "Архив")
        self.fake = Fake([], [topic(1, "General"), topic(7, "Приёмка", True), topic(9, "Отгрузка")])
        self.notified = []

        async def get_client():
            return self.fake

        async def resolve(c, peer):
            return {"src": self.src, "dst": self.dst}[peer]

        tg_tools._get_client, tg_tools._resolve = get_client, resolve
        tg_tools.job_notify = self.notified.append

    def wait(self, job_id):
        for _ in range(100):
            st = tg_tools.job_status(job_id)
            if st["status"] != "running":
                return st
            time.sleep(0.05)
        self.fail("job hangs")

    def test_list_and_resolve(self):
        rows = tg_tools.list_topics("src")["topics"]
        self.assertEqual([r["id"] for r in rows], [1, 7, 9])
        self.assertTrue(rows[1]["pinned"])
        req = self.fake.requests[0]
        self.assertIsInstance(req, functions.messages.GetForumTopicsRequest)
        r = tg_tools.execute_tg_tool("tg_topic_scan", {"peer": "src", "topic": "приёмка"})
        self.assertEqual(r["topic_id"], 7)
        self.assertEqual(self.fake.iter_kw[-1]["reply_to"], 7)

    def test_not_forum(self):
        self.src = channel(1, "Обычная", forum=False)
        r = tg_tools.execute_tg_tool("tg_list_topics", {"peer": "src"})
        self.assertIn("не группа с темами", r["error"])

    def test_copy_mixed(self):
        self.fake.messages = [
            Msg(1, 1, action=object()),                                  # служебное — пропуск
            Msg(2, 2, "привет"),
            Msg(3, 3, "альбом", "photo", gid=5), Msg(4, 3, "", "photo", gid=5),
            Msg(5, 4, "файл", "file"),
        ]
        r = tg_tools.execute_tg_tool("tg_topic_copy", {
            "from_peer": "src", "from_topic": "Приёмка", "to_peer": "dst", "to_topic": "9",
            "mode": "copy", "confirmed": True})
        self.assertTrue(r["ok"], r)
        st = self.wait(r["job_id"])
        self.assertEqual((st["status"], st["done"], st["failed"]), ("done", 4, 0))
        self.assertEqual(len(self.fake.sent), 2)                         # текст + файл
        self.assertEqual(self.fake.sent[0][1]["reply_to"], 9)
        self.assertEqual(len(self.fake.files), 1)                        # альбом одним вызовом
        self.assertEqual(len(self.fake.files[0][0]), 2)
        self.assertEqual(self.fake.files[0][1]["reply_to"], 9)
        kw = self.fake.iter_kw[-1]
        self.assertEqual((kw["reply_to"], kw["reverse"]), (7, True))
        self.assertTrue(self.notified and "перенесено 4" in self.notified[0])

    def test_filter_photos_and_credit(self):
        self.fake.messages = [Msg(2, 2, "текст"), Msg(3, 3, "фото", "photo"), Msg(5, 4, "ф", "file")]
        r = tg_tools.execute_tg_tool("tg_topic_copy", {
            "from_peer": "src", "to_peer": "dst", "mode": "copy", "media_type": "photos",
            "from": "2026-10-03", "to": "2026-10-03", "credit": True, "confirmed": True})
        st = self.wait(r["job_id"])
        self.assertEqual((st["done"], st["skipped"]), (1, 1))        # текст пропущен, на файле (4 окт.) — стоп
        self.assertEqual(len(self.fake.files), 1)
        self.assertIn("2026-10-03", self.fake.files[0][1]["caption"])
        self.assertIsNotNone(self.fake.iter_kw[-1].get("offset_date"))
        self.assertNotIn("reply_to", self.fake.iter_kw[-1])             # весь чат

    def test_date_upper_bound(self):
        self.fake.messages = [Msg(2, 2, "раньше"), Msg(3, 5, "позже")]
        r = tg_tools.topic_copy("src", "dst", mode="copy", date_to="2026-10-03")
        st = self.wait(r["job_id"])
        self.assertEqual(st["done"], 1)

    def test_forward_batches_with_topic(self):
        self.fake.messages = [Msg(i, 2, f"m{i}") for i in range(1, 251)]
        r = tg_tools.topic_copy("src", "dst", "7", "9", mode="forward")
        st = self.wait(r["job_id"])
        self.assertEqual((st["status"], st["done"]), ("done", 250))
        fw = [q for q in self.fake.requests if isinstance(q, functions.messages.ForwardMessagesRequest)]
        self.assertEqual([len(q.id) for q in fw], [100, 100, 50])
        self.assertTrue(all(q.top_msg_id == 9 for q in fw))
        self.assertTrue(all(len(q.random_id) == len(q.id) for q in fw))

    def test_limit(self):
        self.fake.messages = [Msg(i, 2, f"m{i}") for i in range(1, 30)]
        r = tg_tools.topic_copy("src", "dst", mode="copy", limit=5)
        self.assertEqual(self.wait(r["job_id"])["done"], 5)

    def test_protected_content_refused(self):
        self.src = channel(1, "Закрытая", noforwards=True)
        r = tg_tools.execute_tg_tool("tg_topic_copy", {"from_peer": "src", "to_peer": "dst", "mode": "copy",
                                                       "confirmed": True})
        self.assertIn("защита контента", r["error"])

    def test_confirmation_and_skill_switch(self):
        r = tg_tools.execute_tg_tool("tg_topic_copy", {"from_peer": "src", "to_peer": "dst", "mode": "copy"})
        self.assertTrue(r.get("needs_confirmation"))
        self.assertFalse(self.fake.iter_kw)
        r = tg_tools.execute_tg_tool("tg_topic_delete", {"peer": "src", "topic": "7"}, {"danger": True})
        self.assertTrue(r.get("needs_confirmation"))
        r = tg_tools.execute_tg_tool("tg_topic_delete", {"peer": "src", "topic": "7", "confirmed": True})
        self.assertIn("выключен", r["error"])                           # навык danger по умолчанию выключен
        names = {t["name"] for t in tg_tools.enabled_tools({"topics": False})}
        self.assertFalse(names & {"tg_topic_copy", "tg_list_topics", "tg_topic_edit", "tg_job_status"})
        self.assertIn("tg_topic_copy", {t["name"] for t in tg_tools.enabled_tools({})})
        self.assertNotIn("tg_topic_delete", {t["name"] for t in tg_tools.enabled_tools({})})
        self.assertEqual(tg_tools.execute_tg_tool("tg_list_topics", {"peer": "src"}, {"topics": False})["error"][:11],
                         "Этот навык ")

    def test_topic_edit_and_delete(self):
        tg_tools.execute_tg_tool("tg_topic_edit", {"action": "close", "peer": "src", "topic": "Отгрузка", "confirmed": True})
        q = self.fake.requests[-1]
        self.assertIsInstance(q, functions.messages.EditForumTopicRequest)
        self.assertEqual((q.topic_id, q.closed), (9, True))
        tg_tools.execute_tg_tool("tg_topic_edit", {"action": "pin", "peer": "src", "topic": "9", "confirmed": True})
        self.assertIsInstance(self.fake.requests[-1], functions.messages.UpdatePinnedForumTopicRequest)
        r = tg_tools.execute_tg_tool("tg_topic_delete", {"peer": "src", "topic": "9", "confirmed": True}, {"danger": True})
        self.assertEqual(r["deleted_topic_id"], 9)
        self.assertIsInstance(self.fake.requests[-1], functions.messages.DeleteTopicHistoryRequest)

    def test_cancel(self):
        class Slow(Fake):
            async def iter_messages(s, ent, **kw):
                for i in range(1000):
                    await asyncio.sleep(0.01)
                    yield Msg(i + 1, 2, "x")
        self.fake = Slow([], [])
        r = tg_tools.topic_copy("src", "dst", mode="copy")
        time.sleep(0.2)
        tg_tools.job_status(r["job_id"], cancel=True)
        self.assertEqual(self.wait(r["job_id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
