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
            "ok": True, "status": "active", "authenticated": True, "auth": "code",
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
        self.calls.append(("login", shop, phone))
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
        self._lookup = market_skill.lookup_exit
        market_skill.lookup_exit = lambda ip: {"mobile": True, "org": "", "asn": "", "warning": ""}

    def tearDown(self):
        market_skill.fetch_ip_direct = self._ip
        market_skill.lookup_exit = self._lookup
        market_skill.set_driver(None)
        self.tmp.cleanup()

    def _proxy(self, user="abcdef"):
        return market_skill.save_proxy(f"http://{user}:{SECRET}@sticky.example:8080")

    def _active(self, shop="ozon"):
        self._proxy()
        self.drv.calls.clear()
        market_skill._set_shop(
            shop, "active", phone_mask="79…67", auth="code",
            storage={"cookies": [{"name": "sid", "value": COOKIE}]},
            why="тест: код уже был",
        )

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
        res = market_skill.begin_login("ozon", "+79001234579")
        self.assertFalse(res["ok"])
        self.assertIn("прокси", res["error"].lower())
        self.assertEqual(self.drv.calls, [])

        self._proxy("zzq")
        self.drv.calls.clear()
        started = market_skill.begin_login("ozon", "+7 900 123 45 79")
        self.assertTrue(started["ok"])
        self.assertEqual(started["status"], "awaiting_code")
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "awaiting_code")
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["phone_mask"], "79…79")
        self.assertEqual(self.drv.calls[0], ("login", "ozon", "+79001234579"))

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
        market_skill.begin_login("wb", "89001234579")
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
        self.assertIn("не активна", dead["error"])
        self.assertNotIn("VPN", dead["error"])
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "expired")
        self.assertFalse(any(c[0] == "read" and False for c in []))

    def test_public_page_is_not_cabinet(self):
        self._active()
        self.drv.read_result = {"ok": False, "session": "public", "url": "https://www.ozon.ru/product/x"}
        out = market_skill.execute("market_favorites", {"shop": "ozon"}, None)
        self.assertIn("не активна", out["error"])
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "expired")

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
        self.assertEqual(market_skill.classify_page({
            "url": "https://www.ozon.ru/my/orderlist", "title": "", "text": "Введите номер телефона\nДоставка",
        }), "login")
        offline = {
            "url": "https://www.ozon.ru/my/orderlist",
            "title": "Ozon",
            "text": "Похоже, нет соединения. Выключите VPN или смените сеть",
        }
        self.assertEqual(market_skill.classify_page(offline), "offline")
        self.assertNotEqual(market_skill.classify_page(offline), "cabinet")
        self.assertEqual(market_skill.classify_page({
            "url": "https://www.ozon.ru/my/orderlist", "title": "", "text": "Похоже, нет\u00a0соединения",
        }), "offline")
        self.assertEqual(market_skill.page_face(offline), "антибот: нет соединения")
        challenge = {"url": "https://id.ozon.ru/", "title": "Antibot Challenge Page", "text": ""}
        self.assertEqual(market_skill.classify_page(challenge), "challenge")
        self.assertEqual(market_skill.page_face(challenge), "антибот")
        wb_phone = {
            "url": "https://id.wb.ru/login/",
            "title": "Войти в WB.ID",
            "text": "Войдите с WB ID\n+7\nПолучить код",
        }
        self.assertEqual(market_skill.classify_page(wb_phone), "login")
        self.assertEqual(market_skill.page_face(wb_phone), "форма входа")
        shell = {"url": "https://www.wildberries.ru/security/login", "title": "", "text": "Лови лучшие цены\nОткрыть"}
        self.assertEqual(market_skill.classify_page(shell), "unknown")
        self.assertEqual(market_skill.page_face(shell), "не форма входа")

    def test_phone_without_code_is_not_active(self):
        self._proxy()
        self.drv.login_result = {
            "ok": True, "status": "active", "authenticated": True, "auth": "code",
            "storage": {"cookies": [{"name": "sid", "value": COOKIE}]},
            "message": "Кабинет открыт",
        }
        for shop, phone in (("ozon", "+79001234579"), ("wb", "89001234579")):
            res = market_skill.begin_login(shop, phone)
            self.assertNotEqual(res.get("status"), "active")
            self.assertNotEqual(market_skill.public_status()["shops"][shop]["status"], "active")
            self.assertIsNone(market_skill._storage(shop))
        self.assertNotIn(COOKIE, json.dumps(market_skill.public_status(), ensure_ascii=False))
        self.assertNotIn(SECRET, json.dumps(market_skill.public_status()["journal"], ensure_ascii=False))

    def test_unproved_active_is_cleared_and_journal_has_step(self):
        self._proxy()
        path = Path(self.tmp.name) / "jarvis_market.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["shops"]["ozon"]["status"] = "active"
        raw["shops"]["ozon"]["storage"] = {"cookies": [{"name": "sid", "value": COOKIE}]}
        path.write_text(json.dumps(raw), encoding="utf-8")
        status = market_skill.public_status()
        self.assertEqual(status["shops"]["ozon"]["status"], "expired")
        self.assertIsNone(market_skill._storage("ozon"))
        blob = json.dumps(status["journal"], ensure_ascii=False)
        self.assertIn("status", blob)
        self.assertNotIn(COOKIE, blob)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("79001234579", blob)

    def test_offline_page_is_not_active_and_resets(self):
        info = {
            "url": "https://www.ozon.ru/my/orderlist",
            "title": "Ozon",
            "text": "Похоже, нет соединения. Выключите VPN или смените сеть",
        }
        decision = market_skill.decide_after_phone("ozon", info, code_field=True)
        self.assertFalse(decision.get("ok"))
        self.assertNotEqual(decision.get("status"), "active")
        self.assertEqual(decision.get("status"), "blocked")
        self.assertEqual(decision.get("face"), "антибот: нет соединения")
        flagged = market_skill.session_from_page(info)
        self.assertEqual(flagged["session"], "blocked")
        self.assertIn("антибот", flagged["error"].lower())
        self.assertNotIn("VPN", flagged["error"])

        self._proxy()
        shot = "m" + "ab" * 8 + ".jpg"
        self.drv.login_result = {
            "ok": False, "status": "offline", "error": market_skill.OFFLINE_ERROR,
            "url": "https://www.ozon.ru/my/orderlist?__rr=1&token=hide",
            "title": "Похоже, нет соединения",
            "summary": "Похоже, нет соединения",
            "face": "антибот: нет соединения",
            "steps": [{
                "step": "opened",
                "url": "https://www.ozon.ru/my/orderlist?__rr=1&token=hide",
                "title": "Похоже, нет соединения",
                "summary": "Похоже, нет соединения",
                "face": "антибот: нет соединения",
                "result": "blocked",
                "shot": shot,
            }],
        }
        started = market_skill.begin_login("ozon", "+79001234579")
        self.assertFalse(started["ok"])
        self.assertEqual(started["status"], "blocked")
        self.assertIn("антибот", started["error"].lower())
        card = market_skill.public_status()
        self.assertEqual(card["shops"]["ozon"]["status"], "blocked")
        self.assertEqual(card["shops"]["ozon"]["label"], "Заблокировано антиботом")
        opened = next(row for row in card["journal"] if row.get("step") == "opened")
        self.assertEqual(opened["url"], "https://www.ozon.ru/my/orderlist")
        self.assertIn("нет соединения", opened["title"])
        self.assertIn("антибот", opened["summary"])
        self.assertEqual(opened["shot"], shot)
        self.assertNotIn("token=hide", json.dumps(card["journal"], ensure_ascii=False))
        self.assertNotIn("79001234579", json.dumps(card["journal"], ensure_ascii=False))

        self._active()
        self.drv.read_result = {
            "ok": True, "session": "active",
            "url": "https://www.ozon.ru/my/orderlist?token=hide",
            "title": "Ozon",
            "text": "Похоже, нет соединения. Выключите VPN или смените сеть",
        }
        out = market_skill.execute("market_orders", {"shop": "ozon"}, None)
        self.assertEqual(market_skill.public_status()["shops"]["ozon"]["status"], "blocked")
        self.assertIn("антибот", out["error"].lower())
        self.assertNotIn("VPN", out["error"])
        self.assertNotIn("выключить", out["error"].lower())
        journal = market_skill.public_status()["journal"]
        self.assertTrue(any(row.get("step") == "read" for row in journal))
        dumped = json.dumps(journal, ensure_ascii=False)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(COOKIE, dumped)
        self.assertNotIn("token=hide", dumped)
        self.assertNotIn("79991234567", dumped)

    def test_placeholder_not_saved_or_sent_to_wb(self):
        self._proxy()
        self.drv.calls.clear()
        for raw in ("+79991234567", "79991234567", "8 999 123-45-67"):
            res = market_skill.begin_login("wb", raw)
            self.assertFalse(res["ok"])
            self.assertNotIn("79991234567", res.get("error") or "")
        self.assertEqual(self.drv.calls, [])
        self.assertEqual(market_skill.public_status()["shops"]["wb"]["status"], "none")
        raw = (Path(self.tmp.name) / "jarvis_market.json").read_text(encoding="utf-8")
        self.assertNotIn("79991234567", raw)
        self.assertNotIn("79991234567", "\n".join(market_skill._logs))

    def test_wb_phone_is_national_and_not_found_is_literal(self):
        self._proxy()
        self.drv.calls.clear()
        res = market_skill.begin_login("wb", "+79001234579")
        self.assertTrue(res["ok"])
        self.assertEqual([c for c in self.drv.calls if c[0] == "login"], [("login", "wb", "9001234579")])
        literal = market_skill.login_fail_message("wb", {
            "url": "https://www.wildberries.ru/security/login",
            "text": "Номер не найден",
        })
        self.assertIn("номер не найден", literal.lower())
        other = market_skill.login_fail_message("wb", {
            "url": "https://www.wildberries.ru/security/login",
            "title": "Вход",
            "text": "Получить код",
        })
        self.assertNotIn("не найден", other.lower())
        self.assertIn("/security/login", other)

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


    def test_wb_browser_check_waits(self):
        info = {
            "url": "https://www.wildberries.ru/security/login",
            "title": "",
            "text": "Проверяем браузер",
        }
        self.assertEqual(market_skill.classify_page(info), "pending")
        self.assertNotEqual(market_skill.classify_page(info), "challenge")
        self.assertEqual(market_skill.page_face(info), "проверка браузера")
        self.assertTrue(market_skill.wb_keep_waiting(info, 18, False))
        self.assertFalse(market_skill.wb_keep_waiting(info, 40, False))
        self.assertFalse(market_skill.wb_keep_waiting(info, 5, True))
        decision = market_skill.decide_after_phone("wb", info, False)
        self.assertNotEqual(decision.get("status"), "blocked")

    def test_ozon_warmup_reloads_on_saved_block_page(self):
        html = Path("tests/fixtures/market/ozon-antibot.html").read_text(encoding="utf-8")
        self.assertIn("<title>Похоже, нет соединения</title>", html)
        blocked = {"title": "Похоже, нет соединения", "text": "Выключите VPN, перезагрузите роутер"}
        self.assertFalse(market_skill.ozon_home_clear(blocked))
        self.assertTrue(market_skill.ozon_home_clear({"title": "OZON — интернет-магазин", "text": "каталог"}))
        self.assertFalse(market_skill.ozon_home_clear({
            "title": "OZON", "text": "Похоже, нет соединения",
        }))

        def run(clear_at):
            clock = {"t": 0.0}
            reloads = []

            def now():
                return clock["t"]

            def sleep(sec):
                clock["t"] += sec

            def reload():
                reloads.append(clock["t"])

            def snapshot():
                if clear_at is not None and clock["t"] >= clear_at:
                    return {"title": "OZON — интернет-магазин", "text": "каталог"}
                return blocked

            result = market_skill.ozon_warm_pass(snapshot, reload, sleep, now)
            return result, reloads

        stuck, reloads = run(None)
        self.assertFalse(stuck["ok"])
        self.assertEqual(len(reloads), 2)
        self.assertAlmostEqual(reloads[0], 10, delta=0.5)
        self.assertAlmostEqual(reloads[1], 18, delta=0.5)
        opened, reloads = run(19)
        self.assertTrue(opened["ok"])
        self.assertEqual([ev.get("action") for ev in opened["events"] if ev.get("action") != "see"], ["reload", "reload", "settle"])
        self.assertAlmostEqual(reloads[0], 10, delta=0.5)
        self.assertAlmostEqual(reloads[1], 18, delta=0.5)

    def test_exit_128_is_beeline_broadband(self):
        fields = json.loads(Path("tests/fixtures/market/exit-128.71.38.20.json").read_text(encoding="utf-8"))
        kind = market_skill.describe_exit(fields)
        self.assertEqual(kind["asn"], "AS16345")
        self.assertFalse(kind["mobile"])
        self.assertIn("Vimpelcom", kind["warning"])
        self.assertIn("corbina.ru", kind["warning"])
        self.assertIn("не мобильный", kind["warning"])
        self.assertIn("Ozon", kind["warning"])
        self.assertNotIn("VPN", kind["warning"])
        mobile = market_skill.describe_exit({
            "org": "PJSC Vimpelcom", "asn": "AS16345",
            "netname": "BEELINE-GPRS", "descr": "APN internet.beeline.ru", "hostname": "",
        })
        self.assertTrue(mobile["mobile"])
        self.assertEqual(mobile["warning"], "")

    def test_abt_cookies_not_in_status(self):
        self._proxy()
        market_skill.save_abt(market_skill._proxy(), "android", {"cookies": [{"name": "abt", "value": COOKIE}]})
        blob = json.dumps(market_skill.public_status(), ensure_ascii=False)
        self.assertNotIn(COOKIE, blob)
        raw = (Path(self.tmp.name) / "jarvis_market.json").read_text(encoding="utf-8")
        self.assertIn(COOKIE, raw)
        self.assertEqual(market_skill.load_abt(market_skill._proxy(), "android")["cookies"][0]["value"], COOKIE)


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
        market_html = html.split("МАРКЕТПЛЕЙСЫ")[1].split("SETTINGS VIEW")[0]
        self.assertNotIn("+79991234567", market_html)
        self.assertIn("const active = row.status === 'active'", market_html)
        self.assertIn("{!active && (", market_html)
        self.assertIn("Выйти", market_html)
        self.assertIn("market-journal", market_html)
        self.assertIn("/api/market/shot/", market_html)
        self.assertIn("blocked:", market_html)
        self.assertIn("j.message ||", market_html)


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

    def test_shot_route_is_private(self):
        import urllib.error
        import urllib.request
        blob = bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc40014100100000000000000000000000000000000ffda00080001000100003f00fbffd9"
        )
        name = market_skill.save_jpeg(blob)
        self.assertTrue(market_skill._SHOT_RE.fullmatch(name))
        self.assertEqual(market_skill.save_jpeg(b"not-a-jpeg"), "")
        opener = self._opener()
        bare = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/market/shot/{name}")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(bare, timeout=10)
        self.assertEqual(raised.exception.code, 401)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/market/shot/{name}",
            headers={"Cookie": f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"},
        )
        with opener.open(req, timeout=10) as resp:
            body = resp.read()
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(body, blob)
        missing = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/market/shot/m{'cd' * 8}.jpg",
            headers={"Cookie": f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(missing, timeout=10)
        self.assertEqual(raised.exception.code, 404)


class MarketFixtureHtmlTest(unittest.TestCase):
    """Селекторы на сохранённом HTML живых шагов, без сети и без прокси."""

    @classmethod
    def setUpClass(cls):
        chrome = "/opt/google/chrome/chrome"
        if not os.path.isfile(chrome):
            raise unittest.SkipTest("нет Chrome для разбора фикстур")
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(
            executable_path=chrome, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        cls._page = cls._browser.new_page()

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "_browser", None)
        if browser:
            browser.close()
        pw = getattr(cls, "_pw", None)
        if pw:
            pw.stop()

    def _open(self, name):
        html = Path("tests/fixtures/market", name).read_text(encoding="utf-8")
        self._page.set_content(html, wait_until="domcontentloaded")
        return html

    def _info(self, url):
        return {
            "url": url,
            "title": self._page.title(),
            "text": self._page.inner_text("body"),
        }

    def test_ozon_antibot_fixture_is_not_a_login_form(self):
        html = self._open("ozon-antibot.html")
        self.assertIn("token=REDACTED", html)
        self.assertNotRegex(html, r"token=[A-Za-z0-9_\-]{20,}")
        info = self._info("https://www.ozon.ru/my/orderlist")
        self.assertIn("нет соединения", info["title"].lower() + info["text"].lower())
        self.assertEqual(market_skill.classify_page(info), "offline")
        self.assertEqual(market_skill.page_face(info), "антибот: нет соединения")
        self.assertEqual(self._page.locator("input").count(), 0)
        self.assertEqual(self._page.locator("#wb-phone-number").count(), 0)
        self.assertGreater(self._page.locator("div.title").count(), 0)
        self.assertGreater(self._page.locator("button.btn.rb").count(), 0)

    def test_ozon_challenge_fixture_is_antibot(self):
        html = self._open("ozon-id-challenge.html")
        self.assertNotRegex(html, r'value="[^"]{8,}"')
        info = self._info("https://id.ozon.ru/")
        self.assertEqual(info["title"], "Antibot Challenge Page")
        self.assertEqual(market_skill.classify_page(info), "challenge")
        self.assertEqual(market_skill.page_face(info), "антибот")
        self.assertEqual(self._page.locator("#wb-phone-number").count(), 0)
        self.assertEqual(self._page.locator("input[type='tel']").count(), 0)

    def test_wb_shell_has_no_phone_field(self):
        self._open("wb-login-shell.html")
        info = self._info("https://www.wildberries.ru/security/login")
        self.assertEqual(market_skill.classify_page(info), "unknown")
        self.assertEqual(market_skill.page_face(info), "не форма входа")
        self.assertEqual(self._page.locator("#wb-phone-number").count(), 0)
        for sel in ("input[type='tel']", "input[name='phone']", "input[autocomplete='tel']", "input[inputmode='tel']"):
            self.assertEqual(self._page.locator(sel).count(), 0, sel)
        self.assertGreater(self._page.locator("#blockedVpn").count(), 0)

    def test_wb_id_phone_matches_new_selector_not_old_ones(self):
        self._open("wb-id-phone.html")
        info = self._info("https://id.wb.ru/login/")
        self.assertEqual(market_skill.classify_page(info), "login")
        self.assertNotEqual(market_skill.classify_page(info), "code")
        self.assertEqual(market_skill.page_face(info), "форма входа")
        # На живой форме id висит и на ui-field-phone, и на input. Берём input.
        self.assertEqual(self._page.locator("#wb-phone-number").count(), 2)
        field = self._page.locator("input#wb-phone-number")
        self.assertEqual(field.count(), 1)
        self.assertEqual(field.get_attribute("inputmode"), "numeric")
        self.assertEqual(field.get_attribute("placeholder"), "000 000-00-00")
        self.assertEqual(field.get_attribute("data-test-id"), "field_phone_input")
        self.assertIn("input#wb-phone-number", [sel for sel in market_skill._PHONE_INPUTS if self._page.locator(sel).count()])
        for sel in ("input[type='tel']", "input[name='phone']", "input[name='phoneNumber']", "input[autocomplete='tel']", "input[inputmode='tel']"):
            self.assertEqual(self._page.locator(sel).count(), 0, sel)
        for sel in market_skill._CODE_INPUTS:
            self.assertEqual(self._page.locator(sel).count(), 0, sel)
        self.assertGreater(self._page.locator("button[type='submit']").count(), 0)


if __name__ == "__main__":
    unittest.main()
