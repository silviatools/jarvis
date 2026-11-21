"""Документы в Заметках: хранение, файлы, навык ассистента, страница."""

import io
import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

import jarvis
import notes_docs
import tool_groups


def _png(w, h, rgb):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png(96, 64, (30, 90, 180))
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
MP4 = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x00\x00" + b"isom"
HTML_FILE = "<html><script>alert(1)</script><p>секрет</p></html>".encode()
DIRTY = (
    '<p>Привет <b>мир</b></p><script>alert(1)</script>'
    '<img src="https://evil.test/a.png" onerror="x">'
    '<a href="javascript:alert(1)">плохо</a>'
    '<a href="https://example.com/note">ссылка</a>'
)


class Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


def app(**extra):
    base = {"telegramSkills": {}, "knowledgeNotes": [], "settings": {}}
    base.update(extra)
    return base


class NotesStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def test_rich_text_is_sanitized_and_searchable(self):
        code, created = notes_docs.create_doc({"title": "Отпуск", "html": DIRTY})
        self.assertEqual(code, 200, created)
        doc = created["doc"]
        self.assertIn("<b>мир</b>", doc["html"])
        self.assertIn("https://example.com/note", doc["html"])
        self.assertNotIn("script", doc["html"])
        self.assertNotIn("onerror", doc["html"])
        self.assertNotIn("javascript:", doc["html"])
        self.assertNotIn("evil.test", doc["html"])
        self.assertIn("Привет", doc["html"])
        found = notes_docs.search_result("привет мир")
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["docs"][0]["id"], doc["id"])
        self.assertNotIn("alert", found["docs"][0]["excerpt"])
        code, huge = notes_docs.create_doc({"title": "большой", "html": "а" * (notes_docs.MAX_HTML + 1)})
        self.assertEqual(code, 400, huge)

    def test_files_roundtrip_and_delete_removes_bytes(self):
        _, created = notes_docs.create_doc({"title": "Сканы", "html": "<p>договор</p>"})
        doc_id = created["doc"]["id"]
        code, image = notes_docs.save_file(doc_id, "море.png", "png", PNG)
        self.assertEqual(code, 200, image)
        self.assertEqual(image["file"]["kind"], "image")
        code, video = notes_docs.save_file(doc_id, "клип.mp4", "mp4", MP4)
        self.assertEqual(code, 200, video)
        self.assertEqual(video["file"]["kind"], "video")
        code, other = notes_docs.save_file(doc_id, "заметка.txt", "txt", "черновик".encode())
        self.assertEqual(code, 200, other)
        self.assertEqual(other["file"]["kind"], "file")
        self.assertEqual(other["file"]["mime"], "application/octet-stream")
        code, page = notes_docs.save_file(doc_id, "page.html", "html", HTML_FILE)
        self.assertEqual(code, 200, page)
        self.assertNotEqual(page["file"]["mime"], "text/html")
        opened = notes_docs.open_file(image["file"]["id"])
        self.assertEqual(opened[0].read_bytes(), PNG)
        self.assertIsNone(notes_docs.open_file("../jarvis_notes.json"))
        self.assertEqual(notes_docs.save_file("../x", "a.png", "png", PNG)[0], 400)
        self.assertEqual(notes_docs.save_file(doc_id, "пусто.png", "png", b"")[0], 400)
        code, pdf = notes_docs.save_file(doc_id, "бланк.pdf", "pdf", PDF)
        self.assertEqual(code, 200)
        self.assertEqual(pdf["file"]["mime"], "application/pdf")
        hit = notes_docs.search_result("заметка.txt")
        self.assertEqual(hit["docs"][0]["id"], doc_id)
        code, removed = notes_docs.remove_file(other["file"]["id"])
        self.assertEqual(code, 200, removed)
        self.assertFalse(any(f["id"] == other["file"]["id"] for f in removed["doc"]["files"]))
        self.assertEqual(notes_docs.delete_doc(doc_id)[0], 200)
        self.assertEqual(notes_docs.list_docs()["docs"], [])
        self.assertFalse(notes_docs.files_dir().exists() and any(notes_docs.files_dir().rglob("*")))

    def test_read_picks_one_and_asks_when_several(self):
        notes_docs.create_doc({"title": "Отпуск море", "html": "<p>В июне едем на море, билеты куплены.</p>"})
        second = notes_docs.create_doc({"title": "Отпуск горы", "html": "<p>В августе горы.</p>"})[1]["doc"]
        one = jarvis.execute_assistant_tool("notes_read", {"query": "Отпуск море"}, app(), "")
        self.assertEqual(one["doc"]["title"], "Отпуск море")
        self.assertIn("билеты", one["doc"]["text"])
        self.assertNotIn("html", one["doc"])
        many = jarvis.execute_assistant_tool("notes_read", {"query": "отпуск"}, app(), "")
        self.assertTrue(many.get("needs_clarification"), many)
        self.assertGreaterEqual(len(many["docs"]), 2)
        self.assertNotIn("text", many["docs"][0])
        by_id = jarvis.execute_assistant_tool("notes_read", {"id": second["id"]}, app(), "")
        self.assertIn("горы", by_id["doc"]["text"])
        long_html = "<p>" + ("слово " * 4000) + "</p>"
        long_doc = notes_docs.create_doc({"title": "Длинный", "html": long_html})[1]["doc"]
        read = notes_docs.read_result(long_doc["id"], "")
        self.assertTrue(read["doc"]["truncated"])
        self.assertEqual(len(read["doc"]["text"]), notes_docs.READ_LIMIT)

    def test_skill_off_hides_tools_prompt_and_refuses(self):
        off = app(telegramSkills={"notes_docs": False})
        names = {t["name"] for t in jarvis._assistant_tool_list(off, False)}
        self.assertFalse(names & notes_docs.TOOL_NAMES)
        prompt = jarvis.build_assistant_system_prompt(off, False)
        self.assertNotIn("ДОКУМЕНТЫ В ЗАМЕТКАХ", prompt)
        self.assertIn("выключен", jarvis.execute_assistant_tool("notes_search", {"query": "отпуск"}, off, "")["error"])
        on = app()
        self.assertIn("ДОКУМЕНТЫ В ЗАМЕТКАХ", jarvis.build_assistant_system_prompt(on, False))
        self.assertIn("notes_read", {t["name"] for t in jarvis._assistant_tool_list(on, False)})
        self.assertNotIn("ДОКУМЕНТЫ В ЗАМЕТКАХ", jarvis.build_assistant_system_prompt(on, False, groups={"weather"}))
        self.assertIn("ДОКУМЕНТЫ В ЗАМЕТКАХ", jarvis.build_assistant_system_prompt(on, False, groups={"notes"}))
        quiet = {t["name"] for t in tool_groups.filter_tools(jarvis._assistant_tool_list(on, False), {"core", "weather"})}
        self.assertNotIn("notes_search", quiet)
        loud = {t["name"] for t in tool_groups.filter_tools(
            jarvis._assistant_tool_list(on, False), tool_groups.pick("найди в заметках про отпуск"))}
        self.assertIn("notes_search", loud)
        self.assertIn("notes_read", loud)
        self.assertNotIn("notes", tool_groups.pick("какая погода завтра"))
        self.assertNotIn("notes", tool_groups.pick("документация к серверу"))
        self.assertIn("notes", tool_groups.pick("прочитай документ про отпуск"))
        self.assertEqual(tool_groups.unassigned(jarvis._assistant_tool_list(on, True)), [])
        row = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == "notes_docs")
        self.assertEqual(row["tools"], ["notes_search", "notes_read"])
        self.assertEqual(row["risk"], "read")
        self.assertTrue(row["default"])
        self.assertEqual(set(row["tools"]), notes_docs.TOOL_NAMES)

    def test_conversation_searches_then_reads(self):
        created = notes_docs.create_doc({"title": "Отпуск", "html": "<p>В июне море, билеты куплены.</p>"})[1]["doc"]
        steps = [
            {"type": "tool_use", "id": "t1", "name": "notes_search", "input": {"query": "отпуск"}},
            {"type": "tool_use", "id": "t2", "name": "notes_read", "input": {"id": created["id"]}},
            {"type": "text", "text": "В июне море, билеты куплены."},
        ]
        seen = []

        def post(url, headers=None, json=None, timeout=None, **kw):
            body = json or {}
            seen.append({t.get("name") for t in (body.get("tools") or [])})
            step = steps.pop(0)
            return Resp({"content": [step], "stop_reason": "end_turn" if step["type"] == "text" else "tool_use", "usage": {}})

        with patch.object(jarvis.requests, "post", side_effect=post):
            text, _links = jarvis._assistant_loop_claude(
                [{"role": "user", "content": "Что у меня в заметках про отпуск?"}],
                "k", "claude-test", app(), "", False)
        self.assertIn("море", text)
        self.assertIn("notes_search", seen[0])
        self.assertIn("notes_read", seen[0])

    def test_backup_gitignore_static_and_ui(self):
        _, created = notes_docs.create_doc({"title": "Архив", "html": "<p>текст</p>"})
        notes_docs.save_file(created["doc"]["id"], "скан.png", "png", PNG)
        ignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("jarvis_notes.json", ignore)
        self.assertIn("jarvis_notes/", ignore)
        self.assertTrue(jarvis._static_forbidden(Path("jarvis_notes.json")))
        self.assertTrue(jarvis._static_forbidden(Path("data") / "jarvis_notes" / "file"))
        html = Path("index (9).html").read_text(encoding="utf-8")
        for needle in ("Документы", "notes-doc-new", "notes-doc-body", "Жирный", "Сим-карт пока нет", "id: 'docs'", "id: 'sims'"):
            self.assertIn(needle, html)
        old = jarvis.DATA_DIR
        jarvis.DATA_DIR = Path(self.tmp.name)
        try:
            names = zipfile.ZipFile(io.BytesIO(jarvis.build_backup_zip())).namelist()
        finally:
            jarvis.DATA_DIR = old
        self.assertIn("data/jarvis_notes.json", names)
        self.assertTrue(any(n.startswith("data/jarvis_notes/") and not n.endswith("/") for n in names))
        self.assertFalse(any(n.startswith("code/jarvis_notes") for n in names))


class NotesHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=False, raw=None, content_type="application/json", headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        hdrs = dict(headers or {})
        if data is not None and content_type:
            hdrs["Content-Type"] = content_type
        if cookie:
            hdrs["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                blob = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(blob.decode()), resp.headers
                return resp.status, blob, resp.headers
        except urllib.error.HTTPError as e:
            blob = e.read()
            try:
                return e.code, json.loads(blob.decode()), e.headers
            except Exception:
                return e.code, blob, e.headers

    def test_auth_crud_and_ranged_file(self):
        code, _, _ = self.req("GET", "/api/notes")
        self.assertEqual(code, 401)
        code, created, _ = self.req("POST", "/api/notes", {"title": "Отпуск", "html": "<p>Море</p>"}, cookie=True)
        self.assertEqual(code, 200, created)
        doc_id = created["doc"]["id"]
        code, saved, _ = self.req("POST", "/api/notes/save", {"id": doc_id, "title": "Отпуск 2026", "html": "<p>Море <b>в июне</b></p>"}, cookie=True)
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["doc"]["title"], "Отпуск 2026")
        self.assertIn("<b>в июне</b>", saved["doc"]["html"])
        code, uploaded, _ = self.req(
            "POST", f"/api/notes/file?doc={doc_id}&name=sea.png&ext=png",
            cookie=True, raw=PNG, content_type="application/octet-stream")
        self.assertEqual(code, 200, uploaded)
        fid = uploaded["file"]["id"]
        code, blob, headers = self.req("GET", f"/api/notes/file/{fid}", cookie=True)
        self.assertEqual(code, 200)
        self.assertEqual(blob, PNG)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        code, _, _ = self.req("GET", f"/api/notes/file/{fid}")
        self.assertEqual(code, 401)
        code, piece, headers = self.req("GET", f"/api/notes/file/{fid}", cookie=True, headers={"Range": "bytes=0-3"})
        self.assertEqual(code, 206, piece if isinstance(piece, dict) else headers.get("Content-Range"))
        self.assertEqual(piece, PNG[:4])
        code, page, headers = self.req(
            "POST", f"/api/notes/file?doc={doc_id}&name=page.html&ext=html",
            cookie=True, raw=HTML_FILE, content_type="application/octet-stream")
        self.assertEqual(code, 200, page)
        code, html_blob, headers = self.req("GET", f"/api/notes/file/{page['file']['id']}", cookie=True)
        self.assertEqual(html_blob, HTML_FILE)
        self.assertNotIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        code, listed, _ = self.req("GET", "/api/notes", cookie=True)
        self.assertEqual(listed["docs"][0]["title"], "Отпуск 2026")
        code, gone, _ = self.req("POST", "/api/notes/delete", {"id": doc_id}, cookie=True)
        self.assertEqual(code, 200, gone)
        code, listed, _ = self.req("GET", "/api/notes", cookie=True)
        self.assertEqual(listed["docs"], [])


CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable")


@unittest.skipUnless(CHROME, "нужен Chrome")
class NotesBrowserTest(unittest.TestCase):
    def test_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright

        tmp = tempfile.TemporaryDirectory()
        old = os.environ.get("DATA_DIR")
        old_paths = (jarvis.DATA_DIR, jarvis.APP_DATA_FILE, jarvis.CONFIG_FILE, jarvis.SUBSCRIBERS_FILE)
        os.environ["DATA_DIR"] = tmp.name
        jarvis.DATA_DIR = Path(tmp.name)
        jarvis.APP_DATA_FILE = jarvis.DATA_DIR / "jarvis_app_data.json"
        jarvis.CONFIG_FILE = jarvis.DATA_DIR / "jarvis_notify_config.json"
        jarvis.SUBSCRIBERS_FILE = jarvis.DATA_DIR / "jarvis_subscribers.json"
        srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        out = Path("/cursor/stores/self/media/notes-docs")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception:
            out = Path(tmp.name)
        cdn = {
            "https://cdn.tailwindcss.com": "/tmp/jarvis-cdn/tailwind.js",
            "https://unpkg.com/react@18/umd/react.production.min.js": "/tmp/jarvis-cdn/react.js",
            "https://unpkg.com/react-dom@18/umd/react-dom.production.min.js": "/tmp/jarvis-cdn/react-dom.js",
            "https://unpkg.com/@babel/standalone@7.24.7/babel.min.js": "/tmp/jarvis-cdn/babel.js",
        }
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda e: errors.append(str(e)))

                def route(route):
                    url = route.request.url.split("?", 1)[0].rstrip("/")
                    path = cdn.get(url)
                    if path and Path(path).exists():
                        route.fulfill(path=path, content_type="application/javascript; charset=utf-8")
                    else:
                        route.continue_()

                page.route("**/*", route)
                page.context.add_cookies([{
                    "name": "jarvis_auth", "value": jarvis.AUTH_COOKIE_VALUE,
                    "url": f"http://127.0.0.1:{port}/",
                }])
                page.goto(f"http://127.0.0.1:{port}/notes", wait_until="domcontentloaded", timeout=60000)
                page.get_by_text("Сим-карт пока нет").wait_for(timeout=180000)
                page.screenshot(path=str(out / "desktop-sims.png"), full_page=True)
                page.get_by_test_id("notes-sec-docs").click()
                page.get_by_text("Документов пока нет").wait_for()
                page.screenshot(path=str(out / "desktop-empty.png"), full_page=True)
                page.get_by_test_id("notes-doc-new").click()
                page.get_by_test_id("notes-doc-editor").wait_for()
                page.get_by_test_id("notes-doc-title").fill("Отпуск")
                page.get_by_test_id("notes-doc-body").click()
                page.keyboard.type("Море в июне. ")
                page.get_by_test_id("notes-doc-bold").click()
                page.keyboard.type("важно")
                page.keyboard.press("Enter")
                page.get_by_test_id("notes-doc-list").click()
                page.keyboard.type("билеты")
                page.get_by_test_id("notes-doc-status").filter(has_text="Сохранено").wait_for(timeout=10000)
                body = page.get_by_test_id("notes-doc-body").inner_html()
                self.assertIn("важно", body)
                self.assertTrue("<b>" in body or "<strong>" in body, body)
                shot = Path(tmp.name) / "sea.png"
                shot.write_bytes(PNG)
                page.get_by_test_id("notes-doc-file").set_input_files(str(shot))
                page.locator(".nd-media img").wait_for()
                page.screenshot(path=str(out / "desktop-editor.png"), full_page=True)
                page.get_by_test_id("notes-doc-back").click()
                page.get_by_test_id("notes-doc-card").filter(has_text="Отпуск").wait_for()
                page.get_by_test_id("notes-doc-search").fill("море")
                page.get_by_test_id("notes-doc-card").filter(has_text="Отпуск").wait_for()
                page.screenshot(path=str(out / "desktop-list.png"), full_page=True)
                page.get_by_test_id("notes-doc-search").fill("неттакого")
                page.get_by_text("Ничего не нашлось").wait_for()
                page.get_by_test_id("notes-doc-search").fill("")
                page.get_by_test_id("notes-doc-card").click()
                page.get_by_test_id("notes-doc-body").wait_for()
                page.get_by_text("Море в июне.").wait_for()
                page.locator(".nd-media img").wait_for()

                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(out / "mobile-editor.png"), full_page=True)
                page.get_by_test_id("notes-doc-back").click()
                page.get_by_test_id("notes-doc-card").wait_for()
                page.screenshot(path=str(out / "mobile-list.png"), full_page=True)
                page.get_by_test_id("notes-sec-sims").click()
                page.get_by_text("Сим-карт пока нет").wait_for()
                page.screenshot(path=str(out / "mobile-sims.png"), full_page=True)
                browser.close()
        finally:
            srv.shutdown()
            thread.join(timeout=3)
            srv.server_close()
            if old is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = old
            jarvis.DATA_DIR, jarvis.APP_DATA_FILE, jarvis.CONFIG_FILE, jarvis.SUBSCRIBERS_FILE = old_paths
            tmp.cleanup()
        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
