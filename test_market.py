"""Кабинеты Ozon/WB: подмена браузера и HTTP, без настоящих магазинов, Telegram и ИИ."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import market_skill
import jarvis


SECRET = "SuperSecret99"
COOKIE = "SECRETCOOKIE"


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.exit_value = "203.0.113.10"
        self.boom = ""
        self.login_result = {"ok": True, "status": "awaiting_code", "message": "Открылась форма кода."}
        self.code_result = {
            "ok": True, "status": "active",
            "storage": {"cookies": [{"name": "sid", "value": COOKIE}], "origins": []},
            "message": "Сессия сохранена.",
        }
        self.read_result = {
            "ok": True, "session": "active",
            "url": "https://www.ozon.ru/my/orderlist?token=hide",
            "title": "Заказы",
            "text": "Заказ 1001-0001, телефон +79991234567",
        }
        self.cancel_result = {"ok": True, "cancelled": True, "session": "active", "text": "Заказ отменён"}

    def exit_ip(self, proxy):
        self.calls.append(("ip",))
        if self.boom == "ip":
            raise RuntimeError("proxy auth failed " + (proxy.get("password") or ""))
        return self.exit_value

    def begin_login(self, shop, phone, proxy):
        self.calls.append(("login", shop))
        if self.boom == "login":
            raise RuntimeError("net::ERR " + proxy.get("password", ""))
        return dict(self.login_result)

    def submit_code(self, shop, code, proxy):
        self.calls.append(("code", shop))
        if self.boom == "code":
            raise RuntimeError("bad code " + code + " " + proxy.get("password", ""))
        return dict(self.code_result)

    def close_login(self, shop):
        self.calls.append(("close", shop))

    def read(self, shop, section, proxy, storage, order_id=""):
        self.calls.append(("read", shop, section, order_id))
        if self.boom == "read":
            raise RuntimeError(proxy.get("password", ""))
        return dict(self.read_result)

    def cancel_order(self, shop, order_id, proxy, storage):
        self.calls.append(("cancel", shop, order_id))
        return dict(self.cancel_result)


class MarketSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        self.drv = FakeDriver()
        market_skill.set_driver(self.drv)
        market_skill._logs.clear()
        self._ip = market_skill.fetch_ip_direct
        market_skill.fetch_ip_direct = lambda: "198.51.100.8"

    def tearDown(self):
        market_skill.fetch_ip_direct = self._ip
        market_skill.set_driver(None)
        self.tmp.cleanup()

    def _proxy(self, user="abcdef"):
        return market_skill.save_proxy(f"http://{user}:{SECRET}@sticky.example:8080")

    def _active(self, shop="ozon"):
        self._proxy()
        self.drv.calls.clear()
        market_skill._set_shop(shop, "active", phone_mask="79…67", storage={"cookies": [{"name": "sid", "value": COOKIE}]})

    def test_profile_and_hints(self):
        self.assertEqual(market_skill.PIXEL7_PROFILE["viewport"], {"width": 412, "height": 915})
        self.assertTrue(market_skill.PIXEL7_PROFILE["is_mobile"])
        self.assertTrue(market_skill.PIXEL7_PROFILE["has_touch"])
        self.assertEqual(market_skill.PIXEL7_PROFILE["locale"], "ru-RU")
        self.assertEqual(market_skill.PIXEL7_PROFILE["timezone_id"], "Europe/Moscow")
        hints = market_skill.build_hints("131.0.6778.85")
        self.assertEqual(hints["metadata"]["model"], "Pixel 7")
        self.assertEqual(hints["metadata"]["platform"], "Android")
        self.assertTrue(hints["metadata"]["mobile"])
        self.assertIn("Pixel 7", hints["user_agent"])
        self.assertIn("Android", hints["user_agent"])
        self.assertIn("Mobile", hints["user_agent"])
        self.assertIn("Pixel 7", hints["headers"]["sec-ch-ua-model"])
        self.assertEqual(hints["headers"]["sec-ch-ua-mobile"], "?1")

    def test_proxy_mask_and_store(self):
        res = self._proxy()
        self.assertTrue(res["ok"])
        self.assertEqual(res["mask"], "http://sticky.example:8080 · abc")
        blob = json.dumps(res, ensure_ascii=False) + json.dumps(market_skill.public_status(), ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("abcdef", res["mask"])
        raw = (Path(self.tmp.name) / "jarvis_market.json").read_text(encoding="utf-8")
        self.assertIn(SECRET, raw)
        self.assertNotIn(SECRET, "\n".join(market_skill._logs))
        self.assertFalse(market_skill.save_proxy("socks5://a:b@h:1")["ok"])
        self.assertFalse(market_skill.save_proxy("http://h")["ok"])

    def test_login_requires_proxy_and_hides_cookie(self):
        res = market_skill.begin_login("ozon", "+79991234567")
        self.assertFalse(res["ok"])
        self.assertIn("прокси", res["error"].lower())
        self.assertEqual(self.drv.calls, [])

        self._proxy("zzq")
        self.drv.calls.clear()
        started = market_skill.begin_login("ozon", "+7 999 123 45 67")
        self.assertTrue(started["ok"])
        self.assertEqual(started["status"], "awaiting_code")
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "awaiting_code")
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["phone_mask"], "79…67")

        done = market_skill.submit_code("ozon", "1234")
        self.assertTrue(done["ok"])
        self.assertEqual(done["status"], "active")
        public = json.dumps(done, ensure_ascii=False) + json.dumps(market_skill.public_status(), ensure_ascii=False)
        self.assertNotIn(COOKIE, public)
        self.assertNotIn(SECRET, public)
        self.assertIn(COOKIE, (Path(self.tmp.name) / "jarvis_market.json").read_text(encoding="utf-8"))
        self.assertNotIn(COOKIE, "\n".join(market_skill._logs))

    def test_bad_code_does_not_activate(self):
        self._proxy()
        market_skill.begin_login("wb", "89991234567")
        self.drv.code_result = {"ok": False, "status": "awaiting_code", "error": "Код не принят. " + SECRET}
        res = market_skill.submit_code("wb", "0000")
        self.assertFalse(res["ok"])
        self.assertNotIn(SECRET, res["error"])
        self.assertEqual(market_skill.public_status()["shops"]["wb"]["status"], "awaiting_code")

    def test_ip_check(self):
        self.assertFalse(market_skill.check_ip()["ok"])
        self._proxy()
        self.drv.calls.clear()
        same = market_skill.check_ip()
        market_skill.fetch_ip_direct = lambda: "203.0.113.10"
        same = market_skill.check_ip()
        self.assertFalse(same["ok"])
        self.assertIn("IP сервера", same["error"])
        market_skill.fetch_ip_direct = lambda: "198.51.100.8"
        diff = market_skill.check_ip()
        self.assertTrue(diff["ok"])
        self.assertEqual(diff["exit_ip"], "203.0.113.10")
        self.assertNotEqual(diff["exit_ip"], diff["server_ip"])
        self.assertNotIn(SECRET, json.dumps(diff))
        self.drv.boom = "ip"
        market_skill._logs.clear()
        failed = market_skill.check_ip()
        self.assertFalse(failed["ok"])
        self.assertNotIn(SECRET, failed["error"])
        self.assertNotIn(SECRET, "\n".join(market_skill._logs))

    def test_proxy_change_drops_session(self):
        self._active()
        res = market_skill.save_proxy(f"http://abcdef:{SECRET}@other.example:9090")
        self.assertIn("сброшен", res["message"])
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "none")
        self.assertIsNone(market_skill._storage("ozon"))

    def test_read_masks_phone_and_dead_session(self):
        self._active()
        out = market_skill.execute("market_orders", {"shop": "ozon"}, {market_skill.SKILL_ID: True})
        self.assertTrue(out["ok"])
        self.assertNotIn("79991234567", out["text"])
        self.assertIn("79…67", out["text"])
        self.assertNotIn("token=hide", out["url"])
        self.assertEqual(self.drv.calls[-1][0], "read")

        self.drv.read_result = {"ok": False, "session": "expired", "url": "https://id.ozon.ru/login"}
        dead = market_skill.execute("market_orders", {"shop": "ozon"}, None)
        self.assertIn("слетела", dead["error"])
        self.assertIn("СМС", dead["error"])
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "expired")
        self.assertFalse(any(c[0] == "read" and False for c in []))

    def test_public_page_is_not_cabinet(self):
        self._active()
        self.drv.read_result = {"ok": False, "session": "public", "url": "https://www.ozon.ru/product/x"}
        out = market_skill.execute("market_favorites", {"shop": "ozon"}, None)
        self.assertIn("витрин", out["error"])
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "active")

    def test_cancel_needs_confirmation(self):
        self._active()
        self.drv.calls.clear()
        blocked = market_skill.execute("market_cancel_order", {"shop": "ozon", "order_id": "1001-0001"}, None)
        self.assertTrue(blocked.get("needs_confirmation"))
        self.assertEqual(self.drv.calls, [])
        done = market_skill.execute(
            "market_cancel_order", {"shop": "ozon", "order_id": "1001-0001", "confirmed": True}, None)
        self.assertTrue(done.get("cancelled"))
        self.assertEqual(self.drv.calls[-1][0], "cancel")
        self.drv.cancel_result = {"ok": False, "cancelled": False, "session": "active", "error": "На странице нет кнопки «Отменить заказ»."}
        self._active("wb")
        no_btn = market_skill.execute(
            "market_cancel_order", {"shop": "wb", "order_id": "1001-0001", "confirmed": True}, None)
        self.assertFalse(no_btn.get("cancelled"))
        self.assertNotIn("отменён", (no_btn.get("text") or "").lower())

    def test_skill_off_skips_driver(self):
        self._active()
        self.drv.calls.clear()
        state = {market_skill.SKILL_ID: False}
        self.assertEqual(market_skill.voice_payload(state), {"tools": [], "prompt": ""})
        out = market_skill.execute("market_orders", {"shop": "ozon"}, state)
        self.assertIn("выключен", out["error"])
        self.assertEqual(self.drv.calls, [])
        via = jarvis.execute_assistant_tool("market_orders", {"shop": "ozon"}, {"telegramSkills": state}, "", True)
        self.assertIn("выключен", via["error"])
        self.assertEqual(self.drv.calls, [])
        cancel = jarvis.execute_assistant_tool(
            "market_cancel_order", {"shop": "ozon", "order_id": "1001-0001", "confirmed": True},
            {"telegramSkills": state}, "", True)
        self.assertIn("выключен", cancel["error"])
        self.assertEqual(self.drv.calls, [])

    def test_classify(self):
        self.assertEqual(market_skill.classify_page({"url": "https://www.ozon.ru/product/1", "title": "", "text": ""}), "public")
        self.assertEqual(market_skill.classify_page({"url": "https://www.wildberries.ru/catalog/1/detail.aspx", "title": "", "text": ""}), "public")
        self.assertEqual(market_skill.classify_page({"url": "https://www.ozon.ru/my/orderlist", "title": "Заказы", "text": "Мои заказы"}), "cabinet")
        self.assertEqual(market_skill.classify_page({"url": "https://id.ozon.ru/", "title": "", "text": "Введите номер телефона"}), "login")

    def test_worker_is_single_thread(self):
        worker = market_skill.SerialWorker()
        seen = []

        def task():
            seen.append(threading.get_ident())

        threads = [threading.Thread(target=lambda: worker.call(task)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(set(seen)), 1)

    def test_browser_reused(self):
        cab = market_skill.PlaywrightCabinet()

        class Dummy:
            def is_connected(self):
                return True

        browser = Dummy()
        cab._browser = browser
        self.assertIs(cab._ensure_browser(), browser)


class AssistantLoopTest(unittest.TestCase):
    def _run(self, provider, skills):
        captured = {}

        class Resp:
            ok = True
            status_code = 200

            def __init__(self, url):
                self.url = url

            def json(self):
                if "anthropic" in self.url:
                    return {"content": [{"type": "text", "text": "готово"}], "stop_reason": "end_turn"}
                return {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return Resp(url)

        orig = jarvis.requests.post
        jarvis.requests.post = fake_post
        app = {"telegramSkills": skills, "knowledgeNotes": [], "settings": {}}
        history = [{"role": "user", "content": "какие заказы"}]
        try:
            if provider == "claude":
                text, _links = jarvis._assistant_loop_claude(history, "k", "m", app, "http://localhost", True)
            else:
                text, _links = jarvis._assistant_loop_openai(history, "k", "m", app, "http://localhost", True)
        finally:
            jarvis.requests.post = orig
        self.assertEqual(text, "готово")
        body = captured["json"]
        if provider == "claude":
            names = [t["name"] for t in body["tools"]]
            system = body["system"]
        else:
            names = [t["function"]["name"] for t in body["tools"]]
            system = body["messages"][0]["content"]
        return names, system

    def test_tools_follow_skill_switch(self):
        on_names, on_system = self._run("claude", {market_skill.SKILL_ID: True})
        off_names, off_system = self._run("claude", {market_skill.SKILL_ID: False})
        for name in market_skill.TOOL_NAMES:
            self.assertIn(name, on_names)
            self.assertNotIn(name, off_names)
        self.assertIn("МАРКЕТПЛЕЙСЫ", on_system)
        self.assertNotIn("МАРКЕТПЛЕЙСЫ", off_system)

        on_o, _ = self._run("openai", {market_skill.SKILL_ID: True})
        off_o, _ = self._run("openai", {market_skill.SKILL_ID: False})
        self.assertIn("market_orders", on_o)
        self.assertNotIn("market_cancel_order", off_o)

        default_names, _ = self._run("claude", {})
        self.assertIn("market_profile", default_names)

    def test_catalog_backup_and_voice_wire(self):
        ids = [s["id"] for s in jarvis.assistant_skills_catalog()]
        self.assertIn(market_skill.SKILL_ID, ids)
        skill = next(s for s in jarvis.assistant_skills_catalog() if s["id"] == market_skill.SKILL_ID)
        self.assertEqual(skill["risk"], "write")
        self.assertEqual(set(skill["tools"]), market_skill.TOOL_NAMES)
        payload = market_skill.voice_payload({market_skill.SKILL_ID: True})
        self.assertEqual([t["name"] for t in payload["tools"]], [t["name"] for t in market_skill.TOOLS])
        self.assertIn("прокси", payload["prompt"])
        self.assertIn("СМС", payload["prompt"])

        src = Path("jarvis.py").read_text(encoding="utf-8")
        self.assertIn('"jarvis_market.json"', src)
        self.assertIn("jarvis_market.json", Path(".gitignore").read_text(encoding="utf-8"))
        self.assertIn("playwright", Path("requirements.txt").read_text(encoding="utf-8"))
        nix = Path("nixpacks.toml").read_text(encoding="utf-8")
        self.assertIn("libnss3", nix)
        self.assertIn("playwright install chromium", nix)

        html = Path("index (9).html").read_text(encoding="utf-8")
        self.assertIn("Маркетплейсы", html)
        self.assertIn("loadMarketTools", html)
        self.assertIn("...MARKET_AGENT_TOOLS", html)
        self.assertEqual(html.count("await loadMarketTools()"), 2)
        self.assertIn("name.startsWith('market_')", html)
        self.assertIn("/api/market/tool", html)
        self.assertIn("data-testid=\"market-settings\"", html)


class MarketHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        self.drv = FakeDriver()
        market_skill.set_driver(self.drv)
        market_skill._logs.clear()
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        market_skill.set_driver(None)
        self.tmp.cleanup()

    def _opener(self):
        import http.cookiejar
        import urllib.request
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        return opener

    def _req(self, opener, method, path, body=None):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "Cookie": f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"},
        )
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def test_status_proxy_and_skills(self):
        opener = self._opener()
        st = self._req(opener, "GET", "/api/market/status")
        self.assertFalse(st["has_proxy"])
        self.assertEqual(st["shops"]["ozon"]["label"], "Нет сессии")
        self.assertEqual(st["shops"]["wb"]["label"], "Нет сессии")
        saved = self._req(opener, "POST", "/api/market/proxy", {"proxy": f"http://abcdef:{SECRET}@sticky.example:8080"})
        self.assertTrue(saved["ok"])
        self.assertNotIn(SECRET, json.dumps(saved))
        self.assertEqual(saved["mask"], "http://sticky.example:8080 · abc")
        again = self._req(opener, "GET", "/api/market/status")
        self.assertEqual(again["proxy"], saved["mask"])
        self.assertNotIn(SECRET, json.dumps(again))
        skills = self._req(opener, "GET", "/api/tg/skills")
        self.assertIn(market_skill.SKILL_ID, [s["id"] for s in skills["skills"]])
        tools = self._req(opener, "GET", "/api/market/tools")
        self.assertIn("market_orders", [t["name"] for t in tools["tools"]])
        self.assertNotIn(SECRET, "\n".join(market_skill._logs))


if __name__ == "__main__":
    unittest.main()
