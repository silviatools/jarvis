"""Учёт денег на карте друга: курс на дату операции, остаток, гостевая ссылка."""

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date
from decimal import Decimal

import card_ledger
import jarvis

TODAY = date(2026, 11, 16)
SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<ValCurs Date="14.11.2026" name="Foreign Currency Market">
  <Valute ID="R01235"><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name><Value>90,0000</Value></Valute>
  <Valute ID="R01239"><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name><Value>100,0000</Value></Valute>
  <Valute ID="R01335"><CharCode>KZT</CharCode><Nominal>100</Nominal><Name>Казахстанский тенге</Name><Value>18,0000</Value></Valute>
  <Valute ID="R01060"><CharCode>AMD</CharCode><Nominal>100</Nominal><Name>Армянский драм</Name><Value>23,0000</Value></Valute>
</ValCurs>
""".encode("utf-8")

NEXT = SAMPLE.replace(b"90,0000", b"110,0000").replace(b"18,0000", b"20,0000").replace(b"Date=\"14.11.2026\"", b"Date=\"15.11.2026\"")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self._fetch = card_ledger.fetch_cbr
        self._today = card_ledger.today_msk
        self.calls = []

        def fetch(day):
            self.calls.append(day.isoformat())
            if day.isoformat() == "2026-11-15":
                return NEXT
            if day.isoformat() == "2026-11-01":
                raise OSError("cbr down")
            return SAMPLE

        card_ledger.fetch_cbr = fetch
        card_ledger.today_msk = lambda: TODAY

    def tearDown(self):
        card_ledger.fetch_cbr = self._fetch
        card_ledger.today_msk = self._today
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def token(self):
        return card_ledger.owner_view()["ledger"]["shareToken"]

    def add(self, token, **kw):
        body = {"authorName": "Борис", "date": "2026-11-16", "comment": "", "kind": "topup", "currency": "KZT", "amount": "100000"}
        body.update(kw)
        return card_ledger.add_entry(token, body)

    def test_balance_is_own_money_at_frozen_rate(self):
        token = self.token()
        code, view = self.add(token, amount="100000", currency="KZT", comment="перевод на карту")
        self.assertEqual(code, 200, view)
        entry = view["entries"][0]
        self.assertEqual(entry["amountRub"], "18000.00")
        self.assertEqual(entry["rateValue"], "18.0000")
        self.assertEqual(entry["nominal"], "100")
        self.assertEqual(entry["cbrDate"], "2026-11-14")
        self.assertEqual(entry["rateDate"], "2026-11-16")
        self.assertEqual(entry["authorName"], "Борис")
        self.assertEqual(entry["comment"], "перевод на карту")
        self.assertNotIn("shareToken", view["ledger"])

        code, view = self.add(token, kind="purchase", amount="10", currency="USD", comment="кофе", authorName="Друг")
        self.assertEqual(code, 200, view)
        self.assertEqual(view["balance"]["topupRub"], "18000.00")
        self.assertEqual(view["balance"]["purchaseRub"], "900.00")
        self.assertEqual(view["balance"]["balanceRub"], "17100.00")
        self.assertEqual(self.calls, ["2026-11-16"])

        card_ledger.fetch_cbr = lambda day: NEXT
        code, again = card_ledger.public_view(token)
        self.assertEqual(code, 200)
        by_cur = {e["currency"]: e["amountRub"] for e in again["entries"]}
        self.assertEqual(by_cur["KZT"], "18000.00")
        self.assertEqual(by_cur["USD"], "900.00")
        self.assertEqual(again["balance"]["balanceRub"], "17100.00")

    def test_edit_amount_keeps_stored_rate_even_if_cache_changes(self):
        token = self.token()
        code, view = self.add(token, amount="100000", currency="KZT")
        entry_id = view["entries"][0]["id"]
        path = card_ledger.store_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rates"]["2026-11-16"]["table"]["KZT"]["value"] = "99.0000"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.calls.clear()
        code, view = card_ledger.update_entry(token, {
            "id": entry_id, "kind": "topup", "amount": "10000", "currency": "KZT",
            "date": "2026-11-16", "comment": "уточнил сумму", "authorName": "Друг",
        })
        self.assertEqual(code, 200, view)
        entry = view["entries"][0]
        self.assertEqual(entry["amountRub"], "1800.00")
        self.assertEqual(entry["authorName"], "Борис")
        self.assertEqual(entry["editedBy"], "Друг")
        self.assertEqual(entry["comment"], "уточнил сумму")
        self.assertEqual(self.calls, [])

    def test_edit_currency_uses_same_day_snapshot_not_a_new_fetch(self):
        token = self.token()
        _, view = self.add(token, amount="100", currency="USD")
        entry_id = view["entries"][0]["id"]
        self.assertEqual(view["entries"][0]["amountRub"], "9000.00")
        card_ledger.fetch_cbr = lambda day: (_ for _ in ()).throw(AssertionError("сеть"))
        code, view = card_ledger.update_entry(token, {
            "id": entry_id, "kind": "topup", "amount": "100", "currency": "EUR",
            "date": "2026-11-16", "comment": "", "authorName": "Борис",
        })
        self.assertEqual(code, 200, view)
        self.assertEqual(view["entries"][0]["amountRub"], "10000.00")
        self.assertEqual(view["entries"][0]["currency"], "EUR")

    def test_edit_date_takes_the_rate_of_the_new_day(self):
        token = self.token()
        _, view = self.add(token, amount="100", currency="USD", date="2026-11-16")
        entry_id = view["entries"][0]["id"]
        code, view = card_ledger.update_entry(token, {
            "id": entry_id, "kind": "topup", "amount": "100", "currency": "USD",
            "date": "2026-11-15", "comment": "", "authorName": "Борис",
        })
        self.assertEqual(code, 200, view)
        self.assertEqual(view["entries"][0]["amountRub"], "11000.00")
        self.assertEqual(view["entries"][0]["cbrDate"], "2026-11-15")
        self.assertIn("2026-11-15", self.calls)

    def test_void_drops_out_of_balance_and_stays_in_history(self):
        token = self.token()
        self.add(token, amount="1000", currency="RUB")
        _, view = self.add(token, kind="purchase", amount="200", currency="RUB", comment="продукты")
        buy = next(e for e in view["entries"] if e["kind"] == "purchase")
        code, view = card_ledger.void_entry(token, {"id": buy["id"], "authorName": "Борис"})
        self.assertEqual(code, 200, view)
        self.assertEqual(view["balance"]["balanceRub"], "1000.00")
        voided = next(e for e in view["entries"] if e["id"] == buy["id"])
        self.assertEqual(voided["status"], "void")
        self.assertEqual(voided["voidedBy"], "Борис")
        self.assertEqual(voided["amountRub"], "200.00")
        code, denied = card_ledger.update_entry(token, {
            "id": buy["id"], "kind": "purchase", "amount": "1", "currency": "RUB",
            "date": "2026-11-16", "authorName": "Борис",
        })
        self.assertEqual(code, 409)
        code, view = card_ledger.restore_entry(token, {"id": buy["id"], "authorName": "Друг"})
        self.assertEqual(code, 200, view)
        self.assertEqual(view["balance"]["balanceRub"], "800.00")
        restored = next(e for e in view["entries"] if e["id"] == buy["id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["restoredBy"], "Друг")

    def test_rub_does_not_need_cbr_and_foreign_does(self):
        token = self.token()
        card_ledger.fetch_cbr = lambda day: (_ for _ in ()).throw(OSError("down"))
        code, view = self.add(token, amount="1500.5", currency="RUB", comment="нал")
        self.assertEqual(code, 200, view)
        self.assertEqual(view["balance"]["balanceRub"], "1500.50")
        self.assertEqual(view["entries"][0]["rateSource"], "rub")
        code, err = self.add(token, amount="100", currency="KZT")
        self.assertEqual(code, 503)
        self.assertIn("недоступен", err["error"])
        self.assertEqual(len(card_ledger.public_view(token)[1]["entries"]), 1)

    def test_rejects_bad_input_and_unknown_currency(self):
        token = self.token()
        self.assertEqual(self.add(token, amount="0")[0], 400)
        self.assertEqual(self.add(token, amount="-5", currency="RUB")[0], 400)
        self.assertEqual(self.add(token, authorName="   ")[0], 400)
        self.assertEqual(self.add(token, date="2026-11-17", currency="RUB")[0], 400)
        self.assertEqual(self.add(token, currency="XYZ")[0], 400)
        code, view = card_ledger.public_view("nope")
        self.assertEqual(code, 404)
        long = "я" * 800
        code, view = self.add(token, currency="RUB", amount="10", comment=long, authorName="А" * 80)
        self.assertEqual(code, 200, view)
        self.assertEqual(len(view["entries"][0]["comment"]), 500)
        self.assertEqual(len(view["entries"][0]["authorName"]), 60)

    def test_disabled_and_regenerated_link_is_gone(self):
        token = self.token()
        self.add(token, amount="10", currency="RUB")
        code, owner = card_ledger.update_settings({"shareEnabled": False})
        self.assertEqual(code, 200)
        self.assertFalse(owner["ledger"]["shareEnabled"])
        self.assertEqual(card_ledger.public_view(token)[0], 404)
        self.assertEqual(card_ledger.add_entry(token, {"kind": "topup", "amount": "1", "currency": "RUB", "date": "2026-11-16", "authorName": "Борис"})[0], 404)
        code, owner = card_ledger.update_settings({"regenerate": True, "title": "KZ", "ownerName": "Борис", "friendName": "Айдос"})
        new = owner["ledger"]["shareToken"]
        self.assertNotEqual(new, token)
        self.assertTrue(owner["ledger"]["shareEnabled"])
        self.assertEqual(owner["ledger"]["title"], "KZ")
        self.assertEqual(card_ledger.public_view(token)[0], 404)
        code, view = card_ledger.public_view(new)
        self.assertEqual(code, 200)
        self.assertEqual(view["balance"]["balanceRub"], "10.00")
        self.assertEqual(view["ledger"]["friendName"], "Айдос")
        mode = card_ledger.store_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_quote_caches_the_day(self):
        token = self.token()
        code, q = card_ledger.quote(token, "kzt", "2026-11-16", "100000")
        self.assertEqual(code, 200, q)
        self.assertEqual(q["amountRub"], "18000.00")
        codes = [c["code"] for c in q["currencies"]]
        self.assertEqual(codes[:5], ["RUB", "USD", "EUR", "KZT", "AMD"])
        self.calls.clear()
        code, again = card_ledger.quote(token, "USD", "2026-11-16", None)
        self.assertEqual(code, 200, again)
        self.assertEqual(again["value"], "90.0000")
        self.assertNotIn("amountRub", again)
        self.assertEqual(self.calls, [])

    def test_backup_contains_the_file(self):
        card_ledger.owner_view()
        old = jarvis.DATA_DIR
        jarvis.DATA_DIR = card_ledger.store_path().parent
        try:
            blob = jarvis.build_backup_zip()
        finally:
            jarvis.DATA_DIR = old
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        self.assertIn("data/jarvis_card_ledger.json", names)


class LedgerHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self._fetch = card_ledger.fetch_cbr
        self._today = card_ledger.today_msk
        card_ledger.fetch_cbr = lambda day: SAMPLE
        card_ledger.today_msk = lambda: TODAY
        self.srv = jarvis.JarvisServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.thread.join(timeout=3)
        self.srv.server_close()
        card_ledger.fetch_cbr = self._fetch
        card_ledger.today_msk = self._today
        if self._env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._env
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=False):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"jarvis_auth={jarvis.AUTH_COOKIE_VALUE}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw.decode())
                return resp.status, raw.decode()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw.decode())
            except Exception:
                return e.code, raw.decode(errors="replace")

    def test_guest_link_without_site_login(self):
        code, body = self.req("GET", "/api/card-ledger")
        self.assertEqual(code, 401)
        code, owner = self.req("GET", "/api/card-ledger", cookie=True)
        self.assertEqual(code, 200, owner)
        token = owner["ledger"]["shareToken"]
        self.assertTrue(jarvis.route_is_public(f"/card/{token}"))
        self.assertTrue(jarvis.route_is_public(f"/api/card/{token}/entry"))
        self.assertFalse(jarvis.route_is_public("/api/card-ledger"))
        self.assertFalse(jarvis.route_is_public("/cards"))
        self.assertFalse(jarvis.route_is_public("/card/short"))

        code, page = self.req("GET", f"/card/{token}")
        self.assertEqual(code, 200)
        self.assertIn("Остаток ваших денег", page)
        self.assertNotIn("Введите код доступа", page)

        code, denied = self.req("GET", "/api/data")
        self.assertEqual(code, 401)

        code, created = self.req("POST", f"/api/card/{token}/entry", {
            "kind": "topup", "amount": "50000", "currency": "KZT",
            "date": "2026-11-16", "comment": "сбер", "authorName": "Борис",
        })
        self.assertEqual(code, 200, created)
        self.assertEqual(created["balance"]["balanceRub"], "9000.00")

        code, quoted = self.req("GET", f"/api/card/{token}/quote?currency=AMD&date=2026-11-16&amount=100")
        self.assertEqual(code, 200, quoted)
        self.assertEqual(quoted["amountRub"], "23.00")

        entry_id = created["entries"][0]["id"]
        code, voided = self.req("POST", f"/api/card/{token}/entry-void", {"id": entry_id, "authorName": "Борис"})
        self.assertEqual(code, 200, voided)
        self.assertEqual(voided["balance"]["balanceRub"], "0.00")
        self.assertEqual(voided["entries"][0]["status"], "void")

        code, _ = self.req("POST", "/api/card-ledger", {"shareEnabled": False}, cookie=True)
        self.assertEqual(code, 200)
        code, missing = self.req("GET", f"/api/card/{token}")
        self.assertEqual(code, 404)
        code, page = self.req("GET", f"/card/{token}")
        self.assertEqual(code, 200)
        self.assertIn("Остаток ваших денег", page)

        code, spa = self.req("GET", "/card-ledger", cookie=True)
        self.assertEqual(code, 200)
        self.assertIn("Карта друга", spa)
        code, login = self.req("GET", "/card-ledger")
        self.assertEqual(code, 200)
        self.assertIn("Введите код доступа", login)


if __name__ == "__main__":
    unittest.main()
