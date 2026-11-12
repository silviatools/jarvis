"""Web search / page reading skill with mocked HTTP: providers, SSRF guard, limits, assistant loops, HTTP API."""
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import jarvis
import tool_groups
import web_skill as web

PUBLIC = "93.184.216.34"


def api(body, status=200):
    r = Mock(ok=status == 200, status_code=status)
    r.json.return_value = body
    return r


def page(body, status=200, ctype="text/html; charset=utf-8", headers=None):
    raw = body.encode("utf-8") if isinstance(body, str) else body
    r = Mock(status_code=status, headers={"Content-Type": ctype, **(headers or {})})
    r.iter_content = lambda size: (raw[i:i + size] for i in range(0, len(raw), size))
    r.__enter__ = lambda self: self
    r.__exit__ = lambda self, *a: None
    return r


TAVILY = {"results": [{"title": "Python 3.14", "url": "https://docs.python.org/3.14/whatsnew/", "content": "What's new <b>in</b> 3.14",
                       "published_date": "2025-10-07"}, {"title": "bad", "url": "javascript:alert(1)", "content": "x"}]}
BRAVE = {"web": {"results": [{"title": "Brave hit", "url": "https://example.org/a", "description": "snippet"}]}}
WIKI = {"query": {"search": [{"title": "Python (язык)", "snippet": "<span>Python</span> — язык"}]}}

HTML = """<html><head><title> Тест страницы </title><script>var secret=1</script><style>.a{}</style></head>
<body><nav><a href="/menu">Меню</a></nav><div role="navigation">Навигация</div><svg><title>Иконка</title></svg>
<div class="cookie-banner">Куки</div><main><h1>Заголовок</h1><p>Первый абзац &amp; текст.</p>
<ul><li>пункт</li></ul><p><a href="/next?p=2#x">Дальше</a> <a href="mailto:a@b.c">почта</a></p>""" + \
    "<p>" + "длинный текст " * 80 + "</p></main><footer>подвал</footer></body></html>"


class WebSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        env.start(); self.addCleanup(env.stop)
        for k in web.ENV_KEYS.values():
            os.environ.pop(k, None)
        self.req = patch.object(web.requests, "request", return_value=api(WIKI)).start()
        self.get_patcher = patch.object(web, "_http_get", return_value=page(HTML))
        self.get = self.get_patcher.start()
        patch.object(web, "_resolve", return_value=[PUBLIC]).start()
        patch.object(web.time, "sleep", return_value=None).start()
        self.addCleanup(patch.stopall)
        web._fetch_cache.clear()
        jarvis.tg_bot.set_ctx(None)

    def calls(self, host):
        return [c for c in self.req.call_args_list if host in c.args[1]]

    # ── catalog / toggle ────────────────────────────────────────────────────
    def test_registered_in_catalog_groups_and_toggle_hides_everywhere(self):
        self.assertIn("web_search", {s["id"] for s in jarvis.assistant_skills_catalog()})
        self.assertEqual(set(web.SKILLS[0]["tools"]), web.TOOL_NAMES)
        self.assertEqual(tool_groups.unassigned(web.TOOLS), [])
        self.assertIn("web", tool_groups.pick("найди в интернете, что нового в Python"))
        self.assertIn("web", tool_groups.pick("открой https://example.org и перескажи"))
        on = {"telegramSkills": {}}
        self.assertTrue(web.TOOL_NAMES <= {t["name"] for t in jarvis._assistant_tool_list(on, False)})
        self.assertIn("ПОИСК В ИНТЕРНЕТЕ", jarvis.build_assistant_system_prompt(on))
        off = {"telegramSkills": {"web_search": False}}
        self.assertTrue(web.TOOL_NAMES.isdisjoint(t["name"] for t in jarvis._assistant_tool_list(off, False)))
        self.assertNotIn("ПОИСК В ИНТЕРНЕТЕ", jarvis.build_assistant_system_prompt(off))
        for name in web.TOOL_NAMES:
            self.assertIn("выключен", jarvis.execute_assistant_tool(name, {"query": "x", "url": "https://a.org"}, off, "")["error"])
        self.assertEqual(web.voice_payload(off["telegramSkills"]), {"tools": [], "prompt": ""})
        web.configure({"native": True})
        self.assertFalse(web.native_enabled(off["telegramSkills"]))
        self.req.assert_not_called()

    # ── search providers ────────────────────────────────────────────────────
    def test_tavily_results_normalized_counted_and_cached(self):
        web.configure({"tavily": "tvly-SECRET"})
        self.req.return_value = api(TAVILY)
        r = web.execute("web_search", {"query": "python 3.14", "count": 3, "freshness": "month", "site": "https://python.org/x"})
        self.assertEqual(r["provider"], "tavily")
        self.assertEqual(r["results"], [{"title": "Python 3.14", "url": "https://docs.python.org/3.14/whatsnew/",
                                         "snippet": "What's new in 3.14", "site": "docs.python.org", "published": "2025-10-07"}])
        call = self.req.call_args
        self.assertEqual(call.args[:2], ("POST", "https://api.tavily.com/search"))
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer tvly-SECRET")
        self.assertEqual(call.kwargs["json"]["include_domains"], ["python.org"])
        self.assertEqual(call.kwargs["json"]["time_range"], "month")
        self.assertFalse(call.kwargs["allow_redirects"])
        again = web.execute("web_search", {"query": "Python 3.14", "count": 3, "freshness": "month", "site": "python.org"})
        self.assertTrue(again["cached"])
        self.assertEqual(self.req.call_count, 1)
        self.assertEqual(web.snapshot()["usage"]["tavily"]["used"], 1)

    def test_brave_parameters(self):
        web.configure({"brave": "BRAVE-SECRET"})
        self.req.return_value = api(BRAVE)
        r = web.search("новости", 4, "day", None, "example.org")
        self.assertEqual(r["provider"], "brave")
        kw = self.req.call_args.kwargs
        self.assertEqual(kw["headers"]["X-Subscription-Token"], "BRAVE-SECRET")
        self.assertEqual(kw["params"], {"q": "новости site:example.org", "count": 4, "text_decorations": "false", "freshness": "pd"})

    def test_without_keys_wikipedia_is_used_and_labeled(self):
        r = web.execute("web_search", {"query": "язык Python"})
        self.assertEqual(r["provider"], "wikipedia")
        self.assertIn("Только Википедия", r["scope"])
        self.assertNotIn("fallback", r)
        self.assertEqual(r["results"][0]["url"], "https://ru.wikipedia.org/wiki/Python_%28%D1%8F%D0%B7%D1%8B%D0%BA%29")
        self.assertEqual(r["results"][0]["snippet"], "Python — язык")
        self.assertIn("User-Agent", self.req.call_args.kwargs["headers"])

    def test_plan_limit_falls_back_to_next_provider_with_diagnostics(self):
        web.configure({"tavily": "tvly-SECRET", "brave": "BRAVE-SECRET"})
        self.req.side_effect = [api({}, 432), api(BRAVE)]
        r = web.search("x")
        self.assertEqual(r["provider"], "brave")
        self.assertIn("HTTP 432", r["provider_warnings"][0])
        self.req.side_effect = [api({}, 401), api({}, 429), api(WIKI)]
        r = web.search("y")
        self.assertEqual(r["provider"], "wikipedia")
        self.assertTrue(r["fallback"])
        self.assertEqual(len(r["provider_warnings"]), 2)
        self.assertNotIn("SECRET", json.dumps(r) + json.dumps(web.snapshot()))
        self.assertFalse(web.snapshot()["last_checks"]["tavily"]["ok"])

    def test_hard_quota_is_reserved_before_http_and_persisted(self):
        web.configure({"tavily": "tvly-SECRET"})
        data = web._read()
        data["usage"] = {"tavily": [web.time.time()] * web.LIMITS["tavily"]}
        web._write(data)
        r = web.search("z")
        self.assertEqual(r["provider"], "wikipedia")
        self.assertIn("исчерпан", r["provider_warnings"][0])
        self.assertEqual(self.calls("tavily"), [])
        web.configure({"provider": "tavily", "clear": ["tavily"]})
        self.assertFalse(web.snapshot()["keys"]["tavily"])
        data["usage"]["wikipedia"] = [web.time.time()] * web.LIMITS["wikipedia"]
        web._write(data)
        r = web.execute("web_search", {"query": "quota"})
        self.assertIn("исчерпан", r["error"])

    def test_network_errors_count_against_quota(self):
        self.req.side_effect = web.requests.ConnectionError("boom")
        self.assertIn("не ответил", web.execute("web_search", {"query": "q"})["error"])
        self.assertEqual(web.snapshot()["usage"]["wikipedia"]["used"], 1)

    # ── keys storage ────────────────────────────────────────────────────────
    def test_keys_private_600_not_in_backup_and_not_served(self):
        web.configure({"tavily": "tvly-SAVED-SECRET", "brave": "BRAVE-SAVED-SECRET", "provider": "brave"})
        self.assertEqual(web._path(web.KEYS_NAME).stat().st_mode & 0o777, 0o600)
        snap = web.snapshot()
        self.assertNotIn("SECRET", json.dumps(snap))
        self.assertEqual(snap["order"], ["brave", "tavily", "wikipedia"])
        web.search("backup me")
        fake_code = Path(self.tmp.name) / "code"
        fake_code.mkdir()
        (fake_code / web.KEYS_NAME).write_text("tvly-SAVED-SECRET")
        with patch.object(jarvis, "DATA_DIR", Path(self.tmp.name)), patch.object(jarvis, "DIR", fake_code):
            z = zipfile.ZipFile(io.BytesIO(jarvis.build_backup_zip()))
        self.assertIn("data/" + web.STORE_NAME, z.namelist())
        self.assertFalse(any(web.KEYS_NAME in n for n in z.namelist()))
        with patch.dict(os.environ, {"TAVILY_API_KEY": "ENV-KEY"}):
            self.assertEqual(web._key("tavily"), "ENV-KEY")

    # ── SSRF ────────────────────────────────────────────────────────────────
    def test_ssrf_blocked_urls(self):
        bad = ["http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/admin",
               "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
               "http://100.64.0.1/", "http://0.0.0.0/", "http://2130706433/", "http://router.local/", "http://api.internal/",
               "http://intranet/", "https://example.org:8080/", "file:///etc/passwd", "ftp://example.org/",
               "http://user:pw@example.org/", "gopher://example.org/"]
        for url in bad:
            with self.subTest(url=url):
                self.assertIn("error", web.execute("web_fetch", {"url": url}))
        with patch.object(web, "_resolve", return_value=[PUBLIC, "10.1.2.3"]):
            self.assertIn("частную", web.execute("web_fetch", {"url": "https://rebind.example.com/"})["error"])
        self.get.assert_not_called()
        self.assertEqual(web.snapshot()["usage"]["fetch"]["used"], 0)

    def test_redirect_to_private_address_is_not_followed(self):
        self.get.side_effect = [page("", 302, headers={"Location": "http://127.0.0.1/admin"})]
        r = web.execute("web_fetch", {"url": "https://example.org/go"})
        self.assertIn("не открываются", r["error"])
        self.assertEqual(self.get.call_count, 1)

    def test_guard_checks_the_actually_connected_address(self):
        # The pre-check passed (DNS said "public"), but the connection lands on loopback: the socket-level guard refuses.
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200); self.end_headers(); self.wfile.write(b"secret")

            def log_message(self, *a):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.get_patcher.stop()
        with patch.object(web, "check_url", side_effect=lambda url: url):
            r = web.execute("web_fetch", {"url": f"http://127.0.0.1:{server.server_port}/x"})
        self.assertIn("не открываются", r["error"])
        self.assertEqual(hits, [])

    # ── page reading ────────────────────────────────────────────────────────
    def test_fetch_extracts_clean_text_links_and_paginates(self):
        r = web.execute("web_fetch", {"url": "example.org/article", "max_chars": 500, "links": True})
        self.assertEqual(r["url"], "https://example.org/article")
        self.assertEqual(r["title"], "Тест страницы")
        self.assertIn("## Заголовок", r["text"])
        self.assertIn("Первый абзац & текст.", r["text"])
        self.assertIn("• пункт", r["text"])
        for junk in ("secret", "Меню", "подвал", ".a{}", "Навигация", "Иконка", "Куки"):
            self.assertNotIn(junk, r["text"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["next_offset"], 500)
        self.assertIn({"text": "Дальше", "url": "https://example.org/next?p=2"}, r["links"])
        self.assertFalse(any(l["url"].startswith("mailto") for l in r["links"]))
        nxt = web.execute("web_fetch", {"url": "https://example.org/article", "offset": 500, "max_chars": 500})
        self.assertEqual(nxt["offset"], 500)
        self.assertNotIn("links", nxt)
        self.assertEqual(self.get.call_count, 1)  # second part from the short cache

    def test_size_type_status_and_redirect_limits(self):
        self.get.return_value = page("<p>" + "x" * (web.MAX_HTML_BYTES + 10) + "</p>")
        r = web.execute("web_fetch", {"url": "https://big.example.org/"})
        self.assertIn("только начало", r["note"])
        self.get.return_value = page(b"\x89PNG", ctype="image/png")
        self.assertIn("не читается", web.execute("web_fetch", {"url": "https://img.example.org/a.png"})["error"])
        self.get.return_value = page("no", 403)
        self.assertIn("HTTP 403", web.execute("web_fetch", {"url": "https://private.example.org/"})["error"])
        self.get.return_value = page("", 301, headers={"Location": "/loop"})
        self.assertIn("перенаправлений", web.execute("web_fetch", {"url": "https://loop.example.org/"})["error"])
        self.get.return_value = page("x", ctype="application/pdf", headers={"Content-Length": str(web.MAX_PDF_BYTES + 1)})
        self.assertIn("5 МБ", web.execute("web_fetch", {"url": "https://pdf.example.org/a.pdf"})["error"])
        self.get.side_effect = web.requests.Timeout()
        self.assertIn("не ответил", web.execute("web_fetch", {"url": "https://slow.example.org/"})["error"])

    def test_redirect_followed_and_charset_decoded(self):
        self.get.side_effect = [page("", 301, headers={"Location": "/new"}),
                                page("<html><head><meta charset='windows-1251'></head><body><p>Привет</p></body></html>".encode("cp1251"),
                                     ctype="text/html")]
        r = web.execute("web_fetch", {"url": "https://example.org/old"})
        self.assertEqual(r["url"], "https://example.org/new")
        self.assertEqual(r["requested_url"], "https://example.org/old")
        self.assertIn("Привет", r["text"])

    def test_fetch_quota(self):
        data = web._read()
        data["usage"] = {"fetch": [web.time.time()] * web.LIMITS["fetch"]}
        web._write(data)
        r = web.execute("web_fetch", {"url": "https://example.org/"})
        self.assertTrue(r["quota_exhausted"])
        self.get.assert_not_called()

    # ── assistant loops ─────────────────────────────────────────────────────
    def test_both_assistant_loops_search_and_cite(self):
        web.configure({"tavily": "tvly-SECRET"})
        self.req.return_value = api(TAVILY)
        for model in ("claude", "openai"):
            web._path().unlink(missing_ok=True)
            web.configure({"provider": "auto"})
            inp = {"query": "что нового в python 3.14"}
            final = "Главное — free-threading. <a href=\"https://docs.python.org/3.14/whatsnew/\">docs.python.org</a>"
            if model == "claude":
                replies = [api({"content": [{"type": "tool_use", "id": "t", "name": "web_search", "input": inp}]}),
                           api({"content": [{"type": "text", "text": final}]})]
                fn = jarvis._assistant_loop_claude
            else:
                replies = [api({"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "t", "type": "function", "function": {"name": "web_search", "arguments": json.dumps(inp)}}]}}]}),
                           api({"choices": [{"message": {"content": final}, "finish_reason": "stop"}]})]
                fn = jarvis._assistant_loop_openai
            with patch.object(jarvis.requests, "post", side_effect=replies) as post:
                answer, _ = fn([{"role": "user", "content": "Что нового в Python 3.14? Найди в интернете"}], "k", "model", {}, "")
            self.assertEqual(answer, final)
            first = post.call_args_list[0].kwargs["json"]
            names = {t.get("name") or t.get("function", {}).get("name") for t in first["tools"]}
            self.assertTrue(web.TOOL_NAMES <= names)
            system = json.dumps(first.get("system") or first.get("messages"), ensure_ascii=False)
            self.assertIn("источники ссылками", system)
            last = json.dumps(post.call_args.kwargs["json"]["messages"][-1], ensure_ascii=False)
            self.assertIn("docs.python.org/3.14/whatsnew", last)
            off = {"telegramSkills": {"web_search": False}}
            with patch.object(jarvis.requests, "post", side_effect=[api({"content": [{"type": "text", "text": "ok"}]}) if model == "claude"
                                                                    else api({"choices": [{"message": {"content": "ok"}}]})]) as post:
                fn([{"role": "user", "content": "найди в интернете"}], "k", "model", off, "")
            sent = post.call_args.kwargs["json"]
            names = {t.get("name") or t.get("function", {}).get("name") for t in sent["tools"]}
            self.assertTrue(web.TOOL_NAMES.isdisjoint(names))

    def test_native_claude_search_replaces_tool_and_appends_sources(self):
        web.configure({"native": True})
        reply = api({"stop_reason": "end_turn", "content": [
            {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {"query": "x"}},
            {"type": "web_search_tool_result", "tool_use_id": "s", "content": []},
            {"type": "text", "text": "По данным сайта, "},
            {"type": "text", "text": "релиз вышел в октябре", "citations": [
                {"type": "web_search_result_location", "url": "https://python.org/r", "title": "Python release"}]},
            {"type": "text", "text": "."}]})
        with patch.object(jarvis.requests, "post", return_value=reply) as post:
            answer, _ = jarvis._assistant_loop_claude([{"role": "user", "content": "когда релиз"}], "k", "m", {}, "")
        tools = post.call_args.kwargs["json"]["tools"]
        self.assertIn(web.NATIVE_CLAUDE_TOOL, tools)
        self.assertEqual([t for t in tools if t.get("name") == "web_search"], [web.NATIVE_CLAUDE_TOOL])
        self.assertIn("web_fetch", {t.get("name") for t in tools})
        self.assertTrue(answer.startswith("По данным сайта, релиз вышел в октябре."))
        self.assertIn("[Python release](https://python.org/r)", answer)

    def test_native_claude_pause_turn_and_rejection_fallback(self):
        web.configure({"native": True})
        replies = [api({"stop_reason": "pause_turn", "content": [{"type": "text", "text": "Ищу. "}]}),
                   api({"stop_reason": "end_turn", "content": [{"type": "text", "text": "Готово"}]})]
        with patch.object(jarvis.requests, "post", side_effect=replies) as post:
            answer, _ = jarvis._assistant_loop_claude([{"role": "user", "content": "q"}], "k", "m", {}, "")
        self.assertEqual(answer, "Ищу.\nГотово")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][-1]["role"], "assistant")
        replies = [api({"error": {"message": "web search is not enabled"}}, 400),
                   api({"content": [{"type": "text", "text": "Ответ"}]})]
        with patch.object(jarvis.requests, "post", side_effect=replies) as post:
            answer, _ = jarvis._assistant_loop_claude([{"role": "user", "content": "q"}], "k", "m", {}, "")
        self.assertEqual(answer, "Ответ")
        second = post.call_args.kwargs["json"]["tools"]
        self.assertNotIn(web.NATIVE_CLAUDE_TOOL, second)
        self.assertIn("web_search", {t.get("name") for t in second})

    def test_native_openai_uses_responses_and_falls_back(self):
        web.configure({"native": True})
        out = {"output": [{"type": "web_search_call", "id": "w"}, {"type": "message", "content": [
            {"type": "output_text", "text": "Релиз в октябре.", "annotations": [
                {"type": "url_citation", "url": "https://python.org/r", "title": "Python"}]}]}], "usage": {}}
        with patch.object(jarvis.requests, "post", return_value=api(out)) as post:
            answer, _ = jarvis._assistant_loop_openai([{"role": "user", "content": "q"}], "k", "gpt-4.1", {}, "")
        body = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], jarvis.OPENAI_RESPONSES_URL)
        self.assertIn(web.NATIVE_OPENAI_TOOL, body["tools"])
        self.assertNotIn("web_search", {t.get("name") for t in body["tools"]})
        self.assertIn("[Python](https://python.org/r)", answer)
        with patch.object(jarvis.requests, "post", side_effect=[api({"error": {"message": "unsupported tool"}}, 400),
                                                                 api({"output": [{"type": "message", "content": [
                                                                     {"type": "output_text", "text": "ok"}]}]})]) as post:
            answer, _ = jarvis._assistant_loop_openai([{"role": "user", "content": "q"}], "k", "gpt-x", {}, "")
        self.assertEqual(answer, "ok")
        self.assertIn("web_search", {t.get("name") for t in post.call_args.kwargs["json"]["tools"]})
        web.configure({"native": False})
        with patch.object(jarvis.requests, "post", return_value=api({"choices": [{"message": {"content": "plain"}}]})) as post:
            jarvis._assistant_loop_openai([{"role": "user", "content": "q"}], "k", "gpt-4.1", {}, "")
        self.assertNotEqual(post.call_args.args[0], jarvis.OPENAI_RESPONSES_URL)

    # ── HTTP API ────────────────────────────────────────────────────────────
    def test_http_auth_config_status_tool_and_key_files(self):
        from http.client import HTTPConnection
        server = ThreadingHTTPServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(client.close)

        def call(path, body=None):
            client.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), {"Content-Type": "application/json"})
            r = client.getresponse()
            return r.status, r.read()
        with patch.object(jarvis.JarvisHandler, "_is_authed", return_value=False):
            self.assertEqual(call("/api/web/status")[0], 401)
            self.assertEqual(call("/api/web/config", {"tavily": "SECRET"})[0], 401)
        with patch.object(jarvis.JarvisHandler, "_is_authed", return_value=True), \
             patch.object(jarvis, "load_app_data", return_value={}):
            self.assertEqual(call("/api/web/config", {"tavily": "tvly-SECRET", "native": True})[0], 200)
            status, body = call("/api/web/status")
            self.assertEqual(status, 200)
            self.assertNotIn(b"SECRET", body)
            j = json.loads(body)
            self.assertTrue(j["keys"]["tavily"] and j["config"]["native"])
            tools = json.loads(call("/api/web/tools")[1])
            self.assertEqual({t["name"] for t in tools["tools"]}, web.TOOL_NAMES)
            self.req.return_value = api(TAVILY)
            status, body = call("/api/web/tool", {"name": "web_search", "input": {"query": "py"}})
            self.assertEqual(json.loads(body)["provider"], "tavily")
            self.assertEqual(call("/api/web/tool", {"name": "web_search", "input": [1]})[0], 400)
            self.assertEqual(call("/api/web/tool", {"name": "route_places", "input": {}})[0], 404)
            for url in ("/jarvis_web_keys.json", "/jarvis_web_%6beys.json", "/jarvis_web_keys.json.tmp"):
                self.assertEqual(call(url)[0], 404)


if __name__ == "__main__":
    unittest.main()
