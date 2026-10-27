"""Почта Mail.ru: подмена IMAP, Telegram и ИИ — без настоящих ящиков и сетей."""

import base64
import imaplib
import json
import os
import re
import socket
import stat
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name

import file_tools   # noqa: E402
import jarvis       # noqa: E402
import mail_skill   # noqa: E402
import tg_bot       # noqa: E402

ADDR = "user@mail.ru"
APP_PW = "AppPassw0rdXyz123"
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"


def mutf7(s: str) -> str:
    out, buf = [], ""
    def flush():
        nonlocal buf
        if buf:
            b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf = ""
    for ch in s:
        if 0x20 <= ord(ch) < 0x7f:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


SENT = mutf7("Отправленные")
WORK = mutf7("Работа") + "/" + mutf7("Проекты")


def make_msg(subject, frm, to, *, text=None, html=None, cc=None, atts=(), date="Mon, 20 Oct 2026 10:00:00 +0300"):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, frm, to, date
    if cc:
        m["Cc"] = cc
    if text:
        m.set_content(text)
    if html:
        if text:
            m.add_alternative(html, subtype="html")
        else:
            m.set_content(html, subtype="html")
    for name, data, mt in atts:
        main, sub = mt.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m.as_bytes()


def fixtures():
    docx = file_tools.build_docx(file_tools.parse_blocks("# Отчёт\n\nПривет из docx, квартал третий"))
    pdf = file_tools.build_pdf(file_tools.parse_blocks("Счёт на оплату номер 42"))
    raw8 = ("Subject: Голая кириллица в теме\r\nFrom: robot@shop.ru\r\nTo: user@mail.ru\r\n"
            "Date: Tue, 21 Oct 2026 09:00:00 +0300\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "Ваш заказ собран").encode("utf-8")
    return {
        "INBOX": {
            1: (make_msg("Счёт за октябрь", "Иван Петров <ivan@example.ru>", ADDR,
                         html="<html><head><style>p{}</style></head><body><p>Здравствуйте!</p>"
                              "<script>alert(1)</script><p>Оплатите <a href='https://pay.example/1'>счёт</a></p>"
                              "<img src='https://cdn.example/logo.png' alt='лого'></body></html>",
                         atts=[("отчёт.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")],
                         date="Wed, 01 Oct 2026 10:00:00 +0300"), {"\\Seen"}),
            2: (make_msg("Фото с объекта", "Мария <maria@corp.ru>", ADDR, cc="Босс <boss@corp.ru>",
                         text="Привет! Во вложении фото и акт сверки.",
                         atts=[("акт.pdf", pdf, "application/pdf"), ("photo.jpg", JPG, "image/jpeg")],
                         date="Mon, 20 Oct 2026 12:00:00 +0300"), set()),
            3: (raw8, set()),
        },
        SENT: {
            5: (make_msg("Договор", ADDR, "client@x.ru", text="Высылаю договор поставки.",
                         date="Thu, 02 Oct 2026 15:00:00 +0300"), {"\\Seen"}),
        },
        WORK: {
            7: (make_msg("Планёрка", "Пётр <petr@corp.ru>", ADDR, text="Перенесли на пятницу.",
                         date="Fri, 17 Oct 2026 11:00:00 +0300"), {"\\Seen"}),
        },
    }


class FakeIMAP:
    boxes = {}
    validity = {}
    accounts = {}
    peek_violations = []
    write_selects = []
    connect_error = None
    login_error = None

    def __init__(self, host, port=993, timeout=None):
        if FakeIMAP.connect_error:
            raise FakeIMAP.connect_error
        assert host == "imap.mail.ru" and port == 993
        self.sel = None

    def login(self, user, pw):
        if FakeIMAP.login_error:
            raise imaplib.IMAP4.error(FakeIMAP.login_error)
        if FakeIMAP.accounts.get(user) != pw:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] NEOBHODIM parol prilozheniya "
                                      b"https://help.mail.ru/mail/security/protection/external / Application password is REQUIRED")
        return "OK", [b"Authentication successful"]

    def logout(self):
        return "BYE", [b""]

    def noop(self):
        return "OK", [b""]

    def list(self):
        rows = []
        for name in FakeIMAP.boxes:
            rows.append(b'(\\HasNoChildren) "/" "' + name.encode() + b'"')
        rows.append(b'(\\Noselect \\HasChildren) "/" "' + mutf7("Работа").encode() + b'"')
        return "OK", rows

    @staticmethod
    def _name(q):
        return q[1:-1] if q.startswith('"') else q

    def status(self, q, items):
        name = self._name(q)
        box = FakeIMAP.boxes[name]
        unseen = sum(1 for _, fl in box.values() if "\\Seen" not in fl)
        nxt = max(box) + 1 if box else 1
        return "OK", [f'"{name}" (MESSAGES {len(box)} UIDNEXT {nxt} UIDVALIDITY {FakeIMAP.validity.get(name, 1)} UNSEEN {unseen})'.encode()]

    def select(self, q, readonly=False):
        if not readonly:
            FakeIMAP.write_selects.append(q)
        self.sel = self._name(q)
        return "OK", [str(len(FakeIMAP.boxes[self.sel])).encode()]

    def _uids(self, spec):
        box = FakeIMAP.boxes[self.sel]
        out = set()
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                hi = max(box) if box else 0
                a = int(a)
                b = hi if b == "*" else int(b)
                if a > hi and box:
                    out.add(hi)
                out |= {u for u in box if min(a, b) <= u <= max(a, b)}
            else:
                out.add(int(part))
        return sorted(u for u in out if u in box)

    def uid(self, cmd, *args):
        box = FakeIMAP.boxes[self.sel]
        if cmd == "SEARCH":
            q = args[1]
            if q == "ALL":
                return "OK", [" ".join(map(str, sorted(box))).encode()]
            if q == "UNSEEN":
                return "OK", [" ".join(str(u) for u in sorted(box) if "\\Seen" not in box[u][1]).encode()]
            return "NO", [b"[CANNOT] Unsupported search criterion: " + q.encode()]
        if cmd == "FETCH":
            spec, items = args
            if "BODY[" in items.replace("BODY.PEEK[", ""):
                FakeIMAP.peek_violations.append(items)
            out = []
            for i, u in enumerate(self._uids(spec), 1):
                raw, flags = box[u]
                fl = " ".join(sorted(flags))
                if "HEADER.FIELDS" in items:
                    hdr = raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]
                    keep = []
                    for line in re.split(rb"\r?\n(?![ \t])", hdr):
                        if line.split(b":", 1)[0].upper() in (b"FROM", b"TO", b"CC", b"SUBJECT", b"DATE", b"CONTENT-TYPE"):
                            keep.append(line)
                    lit = b"\r\n".join(keep) + b"\r\n\r\n"
                    head = (f'{i} (UID {u} FLAGS ({fl}) RFC822.SIZE {len(raw)} INTERNALDATE "20-Oct-2026 10:00:00 +0300" '
                            f'BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE)] {{{len(lit)}}}').encode()
                else:
                    lit = raw
                    head = f"{i} (UID {u} RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}".encode()
                out += [(head, lit), b")"]
            return "OK", out
        raise AssertionError(cmd)


class Base(unittest.TestCase):
    def setUp(self):
        for p in Path(_TMP.name).iterdir():
            if p.is_dir():
                for q in p.iterdir():
                    q.unlink()
            else:
                p.unlink()
        mail_skill._indexes.clear()
        mail_skill._conns.clear()
        mail_skill._notifiers.clear()
        FakeIMAP.boxes = fixtures()
        FakeIMAP.validity = {}
        FakeIMAP.accounts = {ADDR: APP_PW}
        FakeIMAP.peek_violations, FakeIMAP.write_selects = [], []
        FakeIMAP.connect_error = FakeIMAP.login_error = None
        self._orig = imaplib.IMAP4_SSL
        imaplib.IMAP4_SSL = FakeIMAP
        self._bg = mail_skill._background_sync
        mail_skill._background_sync = lambda acc: None
        self.logs = []
        self._print = mail_skill.print if hasattr(mail_skill, "print") else None
        mail_skill.print = lambda *a, **k: self.logs.append(" ".join(map(str, a)))

    def tearDown(self):
        imaplib.IMAP4_SSL = self._orig
        mail_skill._background_sync = self._bg
        del mail_skill.print

    def add(self):
        r = mail_skill.add_account(ADDR, APP_PW, "Личная")
        self.assertTrue(r["ok"], r)
        return r["account"]["id"]


class AccountTest(Base):
    def test_add_masks_and_stores_password_privately(self):
        acc_id = self.add()
        cfg = Path(_TMP.name, "jarvis_mail.json").read_text(encoding="utf-8")
        self.assertNotIn(APP_PW, cfg)
        sec = mail_skill.secrets_path()
        self.assertIn(APP_PW, sec.read_text())
        self.assertEqual(stat.S_IMODE(sec.stat().st_mode), 0o600)
        st = json.dumps(mail_skill.public_status(), ensure_ascii=False)
        self.assertNotIn(APP_PW, st)
        self.assertIn("••••••••", st)
        self.assertEqual(mail_skill.check_account(acc_id)["ok"], True)
        self.assertIn("jarvis_mail.json", Path(".gitignore").read_text())
        self.assertIn("jarvis_mail/", Path(".gitignore").read_text())

    def test_errors_are_clear(self):
        r = mail_skill.add_account(ADDR, "обычный-пароль")
        self.assertFalse(r["ok"])
        self.assertIn("пароль для внешнего приложения", r["error"])
        FakeIMAP.login_error = b"[AUTHENTICATIONFAILED] IMAP access is disabled for this account"
        self.assertIn("Доступ по IMAP выключен", mail_skill.add_account(ADDR, APP_PW)["error"])
        FakeIMAP.login_error = None
        FakeIMAP.connect_error = socket.timeout("timed out")
        self.assertIn("Сеть", mail_skill.add_account(ADDR, APP_PW)["error"])
        FakeIMAP.connect_error = socket.gaierror(-2, "Name or service not known")
        self.assertIn("DNS", mail_skill.add_account(ADDR, APP_PW)["error"])
        FakeIMAP.connect_error = ConnectionRefusedError(111, "refused")
        self.assertIn("Сеть", mail_skill.add_account(ADDR, APP_PW)["error"])
        self.assertEqual(mail_skill.load_config()["accounts"], [])

    def test_check_after_password_revoked_and_delete(self):
        acc_id = self.add()
        mail_skill.sync_account(mail_skill._find_account(acc_id))
        FakeIMAP.accounts[ADDR] = "revoked"
        r = mail_skill.check_account(acc_id)
        self.assertFalse(r["ok"])
        self.assertIn("пароль", r["error"])
        self.assertTrue(mail_skill.index_path(acc_id).exists())
        self.assertTrue(mail_skill.delete_account(acc_id)["ok"])
        self.assertFalse(mail_skill.index_path(acc_id).exists())
        self.assertNotIn(acc_id, json.loads(mail_skill.secrets_path().read_text()))
        self.assertEqual(mail_skill.public_status()["accounts"], [])
        self.assertFalse(any(APP_PW in l for l in self.logs))


class SyncSearchTest(Base):
    def setUp(self):
        super().setUp()
        self.acc_id = self.add()
        self.acc = mail_skill._find_account(self.acc_id)
        self.sent = []
        mail_skill.add_notifier(lambda acc, metas: self.sent.append([m["subject"] for m in metas]))

    def test_first_sync_silent_then_new_mail_notifies(self):
        r = mail_skill.sync_account(self.acc)
        self.assertTrue(r["ok"] and r["first"], r)
        self.assertEqual(self.sent, [])
        names = {f["name"] for f in mail_skill.public_status()["accounts"][0]["folders"]}
        self.assertEqual(names, {"Входящие", "Отправленные", "Работа/Проекты"})
        FakeIMAP.boxes["INBOX"][4] = (make_msg("Срочно: созвон", "Олег <oleg@corp.ru>", ADDR, text="Созвон в 15:00"), set())
        FakeIMAP.boxes[WORK][8] = (make_msg("В рабочей папке", "x@corp.ru", ADDR, text="тест"), set())
        mail_skill.sync_account(self.acc)
        self.assertEqual(self.sent, [["Срочно: созвон"]])
        mail_skill.sync_account(self.acc)
        self.assertEqual(len(self.sent), 1, "повторная проверка не шлёт то же письмо")
        mail_skill.update_account(self.acc_id, {"notifyFolders": ["INBOX", WORK]})
        FakeIMAP.boxes[WORK][9] = (make_msg("Вторая рабочая", "x@corp.ru", ADDR, text="тест"), set())
        mail_skill.sync_account(mail_skill._find_account(self.acc_id))
        self.assertEqual(self.sent[-1], ["Вторая рабочая"])
        self.assertEqual(FakeIMAP.peek_violations, [])
        self.assertEqual(FakeIMAP.write_selects, [])

    def test_uidvalidity_reset_is_silent(self):
        mail_skill.sync_account(self.acc)
        FakeIMAP.validity["INBOX"] = 99
        FakeIMAP.boxes["INBOX"][10] = (make_msg("После сброса", "a@b.ru", ADDR, text="x"), set())
        mail_skill.sync_account(self.acc)
        self.assertEqual(self.sent, [])
        self.assertEqual(mail_skill.tool_search({"subject": "после сброса"})["total"], 1)

    def search(self, **kw):
        return mail_skill.execute("mail_search", kw, {})

    def test_search_filters_sort_pages(self):
        mail_skill.sync_account(self.acc)
        allm = self.search()
        self.assertEqual(allm["total"], 5)
        self.assertEqual(allm["items"][0]["subject"], "Голая кириллица в теме")
        self.assertEqual(self.search(**{"from": "иван"})["items"][0]["subject"], "Счёт за октябрь")
        self.assertEqual(self.search(subject="счет")["total"], 1, "ё и е — одно и то же")
        self.assertEqual(self.search(to="boss@corp")["items"][0]["subject"], "Фото с объекта")
        self.assertEqual(self.search(folder="Отправленные")["items"][0]["subject"], "Договор")
        self.assertEqual(self.search(folder="работа/проекты")["total"], 1)
        self.assertEqual(self.search(date_from="2026-10-15", date_to="2026-10-20")["total"], 2)
        self.assertEqual({i["subject"] for i in self.search(unread=True)["items"]}, {"Фото с объекта", "Голая кириллица в теме"})
        self.assertEqual({i["subject"] for i in self.search(has_attachments=True)["items"]}, {"Счёт за октябрь", "Фото с объекта"})
        sizes = [i["size"] for i in self.search(sort="size")["items"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        froms = [i["from"].casefold() for i in self.search(sort="from")["items"]]
        self.assertEqual(froms, sorted(froms))
        p1 = self.search(limit=2)
        p2 = self.search(limit=2, offset=p1["next_offset"])
        p3 = self.search(limit=2, offset=p2["next_offset"])
        self.assertEqual(len(p1["items"]) + len(p2["items"]) + len(p3["items"]), 5)
        self.assertIsNone(p3["next_offset"])
        self.assertFalse({i["uid"] for i in p1["items"]} & {i["uid"] for i in p2["items"]})

    def test_text_search_scans_bodies_locally(self):
        mail_skill.sync_account(self.acc)
        r = self.search(text="договор поставки")
        self.assertEqual([i["subject"] for i in r["items"]], ["Договор"])
        self.assertGreater(r["text_scanned"], 0)
        r2 = self.search(text="оплатите")
        self.assertEqual([i["subject"] for i in r2["items"]], ["Счёт за октябрь"])
        self.assertNotIn("text_scanned", r2, "тексты уже в индексе")
        self.assertEqual(FakeIMAP.peek_violations, [])

    def test_read_html_attachments_peek(self):
        mail_skill.sync_account(self.acc)
        r = mail_skill.execute("mail_read", {"account": ADDR, "folder": "Входящие", "uid": 1}, {})
        self.assertIn("Здравствуйте!", r["text"])
        self.assertNotIn("alert", r["text"])
        self.assertNotIn("<p>", r["text"])
        self.assertEqual(r["attachments"][0]["filename"], "отчёт.docx")
        self.assertIn("https://pay.example/1", r["links"])
        self.assertEqual(r["remote_images"][0]["src"], "https://cdn.example/logo.png")
        doc = mail_skill.execute("mail_attachment", {"account": ADDR, "folder": "INBOX", "uid": 1, "index": 1, "action": "read"}, {})
        self.assertIn("Привет из docx", doc["text"])
        r2 = mail_skill.execute("mail_read", {"account": "Личная", "folder": "inbox", "uid": 2}, {})
        self.assertEqual([a["filename"] for a in r2["attachments"]], ["акт.pdf", "photo.jpg"])
        pdf = mail_skill.execute("mail_attachment", {"account": ADDR, "folder": "INBOX", "uid": 2, "index": 1, "action": "read"}, {})
        self.assertIn("42", pdf["text"])
        img = mail_skill.execute("mail_attachment", {"account": ADDR, "folder": "INBOX", "uid": 2, "index": 2, "action": "read"}, {})
        self.assertEqual(img["_image"]["media_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(img["_image"]["b64"]), JPG)
        no_ctx = mail_skill.execute("mail_attachment", {"account": ADDR, "folder": "INBOX", "uid": 2, "index": 1, "action": "send"}, {})
        self.assertIn("только в чате", no_ctx["error"])
        r3 = mail_skill.execute("mail_read", {"account": ADDR, "folder": "INBOX", "uid": 3}, {})
        self.assertEqual(r3["subject"], "Голая кириллица в теме")
        self.assertEqual(FakeIMAP.peek_violations, [])
        self.assertEqual(FakeIMAP.write_selects, [])
        self.assertEqual(FakeIMAP.boxes["INBOX"][2][1], set(), "непрочитанное осталось непрочитанным")

    def test_send_attachment_to_telegram(self):
        mail_skill.sync_account(self.acc)
        calls = []
        orig = tg_bot.api
        tg_bot.api = lambda token, method, payload=None, files=None, timeout=30: (
            calls.append((method, payload, files)) or {"message_id": 77})
        try:
            ctx = tg_bot.Ctx("TOKEN", 555)
            r = mail_skill.execute("mail_attachment", {"account": ADDR, "folder": "INBOX", "uid": 2, "index": 1, "action": "send"},
                                   {}, None, ctx, tg_bot)
        finally:
            tg_bot.api = orig
        self.assertTrue(r["ok"], r)
        doc = [c for c in calls if c[0] == "sendDocument"][0]
        self.assertEqual(doc[1]["chat_id"], 555)
        self.assertEqual(doc[2]["document"][0], "акт.pdf")
        self.assertEqual(tg_bot.recent_file(555, "акт")[0], "акт.pdf")

    def test_unknown_account_and_folder(self):
        mail_skill.sync_account(self.acc)
        self.assertIn("не найден", self.search(account="nobody@mail.ru")["error"])
        self.assertIn("не найдена", self.search(folder="Нет такой")["error"])
        self.assertIn("Нет подключённых", mail_skill.execute("mail_search", {}, {}, allowed=set())["error"])


class JarvisIntegrationTest(Base):
    def setUp(self):
        super().setUp()
        self.acc_id = self.add()
        mail_skill.sync_account(mail_skill._find_account(self.acc_id))
        self.app = {"settings": {"notifyRouting": {"111": ["mail:" + self.acc_id], "222": ["chores"]}}, "telegramSkills": {}}
        jarvis.save_app_data(self.app)
        jarvis.save_subscribers({"offset": 0, "chat_ids": [111, 222]})

    def test_catalog_and_skill_toggle(self):
        ids = [s["id"] for s in jarvis.assistant_skills_catalog()]
        self.assertIn("mail", ids)
        self.assertIn("mail_notify", ids)
        names = {t["name"] for t in jarvis._assistant_tool_list(self.app, False)}
        self.assertTrue({"mail_accounts", "mail_search", "mail_read", "mail_attachment"} <= names)
        self.assertIn("ПОЧТА MAIL.RU", jarvis.build_assistant_system_prompt(self.app))
        off = dict(self.app, telegramSkills={"mail": False})
        self.assertFalse({n for n in (t["name"] for t in jarvis._assistant_tool_list(off, True)) if n.startswith("mail_")})
        self.assertNotIn("ПОЧТА MAIL.RU", jarvis.build_assistant_system_prompt(off, True))
        self.assertIn("выключен", jarvis.execute_assistant_tool("mail_search", {}, off, "")["error"])
        self.assertEqual(mail_skill.voice_payload({"mail": False}), {"tools": [], "prompt": ""})

    def test_claude_and_openai_loops(self):
        seen = []

        class R:
            def __init__(self, j):
                self.ok, self._j, self.status_code = True, j, 200

            def json(self):
                return self._j

        def claude(url, headers=None, json=None, timeout=None):
            seen.append(json)
            if len(seen) == 1:
                return R({"content": [{"type": "tool_use", "id": "t1", "name": "mail_search", "input": {"from": "мария"}}]})
            return R({"content": [{"type": "text", "text": "Письмо от Марии"}], "stop_reason": "end_turn"})

        orig = jarvis.requests.post
        jarvis.requests.post = claude
        try:
            text, _ = jarvis._assistant_loop_claude([{"role": "user", "content": "что от Марии?"}], "k", "m", self.app, "")
        finally:
            jarvis.requests.post = orig
        self.assertEqual(text, "Письмо от Марии")
        self.assertIn("mail_search", [t["name"] for t in seen[0]["tools"]])
        res = seen[1]["messages"][-1]["content"][0]["content"]
        self.assertIn("Фото с объекта", res)

        seen.clear()

        def openai(url, headers=None, json=None, timeout=None):
            seen.append(json)
            if len(seen) == 1:
                return R({"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "mail_attachment", "arguments": '{"account":"user@mail.ru","folder":"Входящие","uid":2,"index":2,"action":"read"}'}}]}}]})
            return R({"choices": [{"message": {"content": "На фото объект"}, "finish_reason": "stop"}]})

        jarvis.requests.post = openai
        try:
            text, _ = jarvis._assistant_loop_openai([{"role": "user", "content": "покажи фото"}], "k", "m", self.app, "")
        finally:
            jarvis.requests.post = orig
        self.assertEqual(text, "На фото объект")
        self.assertIn("mail_attachment", [t["function"]["name"] for t in seen[0]["tools"]])
        self.assertTrue(any(isinstance(m.get("content"), list) and any(c.get("type") == "image_url" for c in m["content"])
                            for m in seen[1]["messages"]), "картинка вложения ушла модели")

        off = dict(self.app, telegramSkills={"mail": False})
        seen.clear()
        jarvis.requests.post = claude
        try:
            jarvis._assistant_loop_claude([{"role": "user", "content": "x"}], "k", "m", off, "")
        finally:
            jarvis.requests.post = orig
        self.assertFalse([t for t in seen[0]["tools"] if t["name"].startswith("mail_")])
        self.assertNotIn("ПОЧТА MAIL.RU", seen[0]["system"])

    def test_notifications_follow_routing(self):
        calls = []
        orig = tg_bot.api
        tg_bot.api = lambda token, method, payload=None, files=None, timeout=30: (
            calls.append((method, payload)) or {"message_id": len(calls)})
        orig_tok = jarvis.get_token
        jarvis.get_token = lambda: "TOKEN"
        mail_skill.add_notifier(jarvis._mail_notify)
        try:
            FakeIMAP.boxes["INBOX"][4] = (make_msg("Новое <важное>", "Олег <oleg@corp.ru>", ADDR, text="Созвон в 15:00"), set())
            mail_skill.sync_account(mail_skill._find_account(self.acc_id))
            msgs = [p for m, p in calls if m == "sendMessage"]
            self.assertEqual([p["chat_id"] for p in msgs], [111])
            self.assertIn("Новое &lt;важное&gt;", msgs[0]["text"])
            self.assertIn("Созвон в 15:00", msgs[0]["text"])
            anchors = json.loads((Path(_TMP.name) / "jarvis_anchors.json").read_text(encoding="utf-8"))
            self.assertIn("uid 4", json.dumps(anchors, ensure_ascii=False))
            calls.clear()
            jarvis.save_app_data(dict(self.app, telegramSkills={"mail_notify": False}))
            FakeIMAP.boxes["INBOX"][5] = (make_msg("Ещё одно", "a@b.ru", ADDR, text="x"), set())
            mail_skill.sync_account(mail_skill._find_account(self.acc_id))
            self.assertEqual([m for m, _ in calls if m == "sendMessage"], [])
        finally:
            tg_bot.api = orig
            jarvis.get_token = orig_tok

    def test_web_push_per_box(self):
        import webpush
        pushed = []
        webpush.set_deliver(lambda sub, data, key: pushed.append(json.loads(data)))
        orig_tok = jarvis.get_token
        jarvis.get_token = lambda: ""
        mail_skill.add_notifier(jarvis._mail_notify)
        try:
            self.assertTrue(webpush.save_subscription({"endpoint": "https://push.example/sub1",
                                                       "keys": {"p256dh": "p" * 20, "auth": "a" * 10}})["ok"])
            FakeIMAP.boxes["INBOX"][4] = (make_msg("Без пуша", "a@b.ru", ADDR, text="x"), set())
            mail_skill.sync_account(mail_skill._find_account(self.acc_id))
            self.assertEqual(pushed, [])
            self.assertTrue(mail_skill.update_account(self.acc_id, {"push": True})["account"]["push"])
            FakeIMAP.boxes["INBOX"][5] = (make_msg("С пушем", "Олег <oleg@corp.ru>", ADDR, text="Текст письма"), set())
            mail_skill.sync_account(mail_skill._find_account(self.acc_id))
            self.assertEqual(len(pushed), 1)
            self.assertEqual(pushed[0]["title"], "📬 Личная")
            self.assertIn("С пушем", pushed[0]["body"])
            self.assertEqual(pushed[0]["tag"], f"mail-{self.acc_id}-5")
        finally:
            webpush.set_deliver(None)
            jarvis.get_token = orig_tok

    def test_bot_chat_sees_only_routed_boxes(self):
        tg_bot.set_ctx(tg_bot.Ctx("TOKEN", 222))
        try:
            r = jarvis.execute_assistant_tool("mail_search", {}, self.app, "")
            self.assertIn("Нет подключённых", r["error"])
            tg_bot.set_ctx(tg_bot.Ctx("TOKEN", 111))
            self.assertEqual(jarvis.execute_assistant_tool("mail_search", {}, self.app, "")["total"], 5)
        finally:
            tg_bot.set_ctx(None)
        self.assertEqual(jarvis.execute_assistant_tool("mail_search", {}, self.app, "")["total"], 5)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json", "Cookie": f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def test_endpoints(self):
        bad = self.req("POST", "/api/mail/account", {"email": ADDR, "password": "wrong"})
        self.assertFalse(bad["ok"])
        ok = self.req("POST", "/api/mail/account", {"email": ADDR, "password": APP_PW, "title": "Личная"})
        self.assertTrue(ok["ok"])
        acc_id = ok["account"]["id"]
        mail_skill.sync_account(mail_skill._find_account(acc_id))
        st = self.req("GET", "/api/mail/status")
        self.assertNotIn(APP_PW, json.dumps(st))
        self.assertEqual(st["accounts"][0]["email"], ADDR)
        self.assertTrue(self.req("POST", "/api/mail/check", {"id": acc_id})["ok"])
        tools = self.req("GET", "/api/mail/tools")
        self.assertIn("mail_search", [t["name"] for t in tools["tools"]])
        img = self.req("POST", "/api/mail/tool", {"name": "mail_attachment", "input": {
            "account": ADDR, "folder": "Входящие", "uid": 2, "index": 2, "action": "read"}})
        self.assertNotIn("_image", img)
        self.assertIn("Telegram", img["note"])
        found = self.req("POST", "/api/mail/tool", {"name": "mail_search", "input": {"text": "пятницу"}})
        self.assertEqual(found["items"][0]["subject"], "Планёрка")
        upd = self.req("POST", "/api/mail/update", {"id": acc_id, "notifyFolders": ["INBOX", SENT]})
        self.assertEqual(upd["account"]["notifyFolders"], ["INBOX", SENT])
        self.assertTrue(self.req("POST", "/api/mail/delete", {"id": acc_id})["ok"])
        self.assertEqual(self.req("GET", "/api/mail/status")["accounts"], [])
        skills = self.req("GET", "/api/tg/skills")
        self.assertIn("mail", [s["id"] for s in skills["skills"]])


class PollerTest(Base):
    """Фоновая проверка: старт процесса, ошибки и зависания ящиков, сторож, строки лога."""

    def setUp(self):
        super().setUp()
        self.acc_id = self.add()
        mail_skill.sync_account(mail_skill._find_account(self.acc_id))
        jarvis.save_app_data({"settings": {"notifyRouting": {"111": ["mail:" + self.acc_id]}}, "telegramSkills": {}})
        jarvis.save_subscribers({"offset": 0, "chat_ids": [111]})
        self.calls = []
        self._api, self._tok = tg_bot.api, jarvis.get_token
        tg_bot.api = lambda token, method, payload=None, files=None, timeout=30: (
            self.calls.append((method, payload)) or {"message_id": len(self.calls)})
        jarvis.get_token = lambda: "TOKEN"

    def tearDown(self):
        mail_skill._poller_state["gen"] += 1
        tg_bot.api, jarvis.get_token = self._api, self._tok
        super().tearDown()

    def wait(self, cond, sec=10):
        import time
        end = time.time() + sec
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def sent_texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_restart_syncs_and_notifies(self):
        import importlib
        import webpush
        pushed = []
        webpush.set_deliver(lambda sub, data, key: pushed.append(json.loads(data)))
        try:
            self.assertTrue(webpush.save_subscription({"endpoint": "https://push.example/s",
                                                       "keys": {"p256dh": "p" * 20, "auth": "a" * 10}})["ok"])
            mail_skill.update_account(self.acc_id, {"push": True})
            # Пока процесс лежал, во Входящие пришло непрочитанное письмо.
            FakeIMAP.boxes["INBOX"][4] = (make_msg("Silvia tools", "Сильвия <s@silvia.ru>", ADDR, text="Тест"), set())
            importlib.reload(mail_skill)          # новый процесс: пустая память, всё с диска
            self.assertEqual(mail_skill._poller_state["gen"], 0)
            jarvis._start_mail_poller()           # то же, что делает main() при старте
            self.assertTrue(self.wait(lambda: self.sent_texts()), "уведомление не пришло")
            self.assertIn("Silvia tools", self.sent_texts()[0])
            self.assertEqual([p["chat_id"] for m, p in self.calls if m == "sendMessage"], [111])
            self.assertTrue(self.wait(lambda: pushed))
            self.assertIn("Silvia tools", pushed[0]["body"])
            self.assertTrue(self.wait(lambda: any("новых 1" in l for l in self.logs)), self.logs)
            st = mail_skill.public_status()
            self.assertTrue(st["poller"]["alive"])
            acc = st["accounts"][0]
            self.assertGreater(acc["lastPollAt"], 0)
            self.assertIn("4", {str(u) for u in mail_skill._index(self.acc_id)["folders"]["INBOX"]["msgs"]})
        finally:
            webpush.set_deliver(None)
        self.assertIn("mail: poller started, 1 ящиков, раз в 90 с", self.logs)
        line = next(l for l in self.logs if "новых 1" in l)
        self.assertEqual(line.split(" (")[0], "mail: u***@mail.ru: новых 1, уведомлений 2")
        for l in self.logs:
            self.assertNotIn(APP_PW, l)
            self.assertNotIn("Silvia tools", l)
            self.assertNotIn(ADDR, l)

    def test_error_on_one_box_does_not_stop_others_or_loop(self):
        FakeIMAP.accounts["second@mail.ru"] = "SecondPass12345"
        second = mail_skill.add_account("second@mail.ru", "SecondPass12345", "Вторая")["account"]["id"]
        mail_skill.sync_account(mail_skill._find_account(second))
        jarvis.save_app_data({"settings": {"notifyRouting": {"111": ["mail:" + self.acc_id, "mail:" + second]}},
                              "telegramSkills": {}})
        mail_skill.add_notifier(jarvis._mail_notify)
        orig = mail_skill._sync_locked

        def broken(acc):
            if acc["id"] == self.acc_id:
                raise SystemExit("ящик сломал проверку")
            return orig(acc)

        mail_skill._sync_locked = broken
        try:
            FakeIMAP.boxes["INBOX"][4] = (make_msg("Во второй", "a@b.ru", ADDR, text="x"), set())
            mail_skill.start_poller(interval=0.1)
            self.assertTrue(self.wait(lambda: self.sent_texts()))
            self.assertTrue(self.wait(lambda: sum("u***@mail.ru: ошибка" in l for l in self.logs) >= 2),
                            "после ошибки цикл продолжает круги")
        finally:
            mail_skill._sync_locked = orig
        self.assertTrue(mail_skill._poller.is_alive())
        self.assertTrue(any(l.startswith("mail: s***@mail.ru: новых 1, уведомлений 1") for l in self.logs), self.logs)

    def test_hung_box_is_cut_and_dead_thread_restarted(self):
        import time
        orig_deadline, orig_abort, orig = mail_skill.SYNC_DEADLINE, mail_skill.ABORT_WAIT, mail_skill._sync_locked
        release = threading.Event()

        def hang(acc):
            release.wait(5)
            return {"ok": False, "error": "x"}, []

        mail_skill.SYNC_DEADLINE, mail_skill.ABORT_WAIT, mail_skill._sync_locked = 0.2, 0.2, hang
        try:
            t0 = time.time()
            res = mail_skill.poll_once()
            self.assertLess(time.time() - t0, 3)
            self.assertIn("соединение сброшено", res[0]["error"])
            self.assertGreater(mail_skill._find_account(self.acc_id)["lastPollAt"], 0)
        finally:
            release.set()
            mail_skill.SYNC_DEADLINE, mail_skill.ABORT_WAIT, mail_skill._sync_locked = orig_deadline, orig_abort, orig
        self.assertTrue(mail_skill.start_poller(interval=0.1))
        self.assertFalse(mail_skill.start_poller(interval=0.1), "живой поток второй раз не запускается")
        mail_skill._poller_state["gen"] += 1     # поток вышел из цикла — как будто умер
        self.assertTrue(self.wait(lambda: not mail_skill._poller.is_alive()))
        self.assertTrue(mail_skill.ensure_poller())
        self.assertTrue(any("poller restarted (прежний поток умер)" in l for l in self.logs), self.logs)
        mail_skill._poller_state["gen"] += 1
        self.assertTrue(self.wait(lambda: not mail_skill._poller.is_alive()))
        self.assertTrue(mail_skill.public_status()["poller"]["alive"], "открытие вкладки «Почта» тоже поднимает поток")


if __name__ == "__main__":
    unittest.main()
