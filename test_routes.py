"""Traffic routing and assistant/HTTP integration, with no live providers."""
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import jarvis
import route_skill as routes
import tool_groups

A = {"lat": 55.61, "lon": 37.72, "label": "Vegas"}
B = {"lat": 55.75, "lon": 37.61, "label": "Дом"}


def provider(*durations, traffic="live_and_typical", tolls=False):
    return {"code": "Ok", "routes": [
        {"duration": n, "distance": 23000, **({"duration_typical": n + 30} if traffic != "disabled" else {}),
         "legs": [{"summary": "МКАД, Ленинский проспект", "steps": [{"intersections": [{"classes": ["toll"] if tolls else []}]}],
                   "annotation": {"distance": [23000], "congestion": ["unknown" if traffic == "unknown" else "low"]}}]}
        for n in durations]}


def response(body, status=200):
    r = Mock(ok=status == 200, status_code=status)
    r.json.return_value = body
    return r


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name, "MAPBOX_TOKEN": "ROUTE-SECRET",
                                      "TOMTOM_KEY": "GEO-SECRET"})
        env.start(); self.addCleanup(env.stop)
        self.now = 1800000000.0
        clock = patch.object(routes.time, "time", side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.get = patch.object(routes.requests, "get", return_value=response(provider(4500, 3900))).start()
        self.addCleanup(patch.stopall)
        self.notify = Mock(return_value=True)
        self.app = {"telegramSkills": {}}
        jarvis.tg_bot.set_ctx(None)

    def watch(self, **extra):
        r = routes.execute("route_watch", {"origin": A, "destination": B, "threshold_minutes": 60, **extra}, {}, 42)
        self.assertTrue(r.get("ok"), r)
        return r

    def run_poll(self):
        routes.poll(lambda: self.app, self.notify)

    def test_places_persist_and_resolve_exact_names(self):
        self.assertTrue(routes.execute("route_save_place", {"name": "Дом", "address": "Москва", "point": B})["ok"])
        self.assertEqual(routes.point("дом")["lat"], B["lat"])
        self.assertIn("не сохранено", routes.execute("route_check", {"origin": "Vegas", "destination": "Дом"})["error"])
        self.assertEqual(len(routes.snapshot()["places"]), 1)
        routes.execute("route_save_place", {"name": "дом", "point": A})
        self.assertEqual(len(routes.snapshot()["places"]), 1)
        routes.execute("route_delete_place", {"name": "ДОМ"})
        self.assertEqual(routes.snapshot()["places"], [])

    def test_geocoder_returns_candidates_without_saving_or_picking(self):
        self.get.return_value = response({"results": [
            {"position": {"lon": 37.7, "lat": 55.6}, "poi": {"name": "Vegas"}},
            {"position": {"lon": 37.3, "lat": 55.8}, "poi": {"name": "Vegas"}}]})
        r = routes.execute("route_find_place", {"query": "Vegas МКАД"})
        self.assertTrue(r["needs_choice"])
        self.assertEqual(r["candidates"][0]["lon"], 37.7)
        self.assertEqual(routes.snapshot()["places"], [])

    def test_fresh_alternatives_sorted_and_coordinates_order(self):
        r = routes.check(A, B, True)
        self.assertEqual([x["duration_seconds"] for x in r["routes"]], [3900, 4500])
        self.assertTrue(self.get.call_args.args[0].endswith("37.72,55.61;37.61,55.75"))
        params = self.get.call_args.kwargs["params"]
        self.assertEqual(params["alternatives"], "true")
        self.assertIn("/driving-traffic/", self.get.call_args.args[0])
        self.assertEqual(params["exclude"], "toll")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_existing_home_work_and_search_key_are_reused(self):
        settings = {"homeAddress": "Москва, дом", "workAddress": "Москва, работа", "tomtomKey": "EXISTING-KEY"}
        result = routes.execute("route_places", {}, settings=settings)
        self.assertEqual({p["name"]: p["address"] for p in result["places"]},
                         {"Дом": "Москва, дом", "Работа": "Москва, работа"})
        with patch.dict(os.environ, {"TOMTOM_KEY": ""}):
            self.assertTrue(routes.snapshot(settings)["geocoder_ready"])
            self.get.return_value = response({"results": []})
            routes.find_place("Vegas", settings)
        self.assertEqual(self.get.call_args.kwargs["params"]["key"], "EXISTING-KEY")
        routes.execute("route_save_place", {"name": "Дом", "point": B})
        self.assertEqual(len([p for p in routes.places(settings) if p["name"] == "Дом"]), 1)

    def test_changed_home_setting_does_not_use_old_coordinates(self):
        settings = {"homeAddress": "Старый адрес"}
        routes.execute("route_save_place", {"name": "Дом", "point": B}, settings=settings)
        self.assertEqual(routes.point("Дом", settings)["lat"], B["lat"])
        settings["homeAddress"] = "Новый адрес"
        result = routes.execute("route_check", {"origin": A, "destination": "Дом"}, settings=settings)
        self.assertIn("Новый адрес", result["error"])
        self.get.assert_not_called()

    def test_hard_free_quota_counts_errors_and_survives_restart(self):
        with patch.dict(routes.LIMITS, {"mapbox": 2}):
            routes.check(A, B)
            self.get.side_effect = requests.Timeout("SECRET")
            with self.assertRaises(routes.RouteError):
                routes.check(A, B)
            self.assertEqual(routes.snapshot()["usage"]["mapbox"]["used"], 2)
            self.get.reset_mock()
            routes.configure({"mapbox": "NEW-KEY"})
            with self.assertRaises(routes.QuotaError):
                routes.check(A, B)
            self.get.assert_not_called()
            self.now += routes.WINDOW + 1
            self.get.side_effect = None
            self.get.return_value = response(provider(4000))
            self.assertTrue(routes.check(A, B)["ok"])

    def test_quota_notification_stops_background_network_calls(self):
        with patch.dict(routes.LIMITS, {"mapbox": 1}):
            self.watch()
            self.get.reset_mock()
            self.run_poll()
            self.get.assert_not_called()
            self.assertEqual(routes.snapshot()["watches"][0]["status"], "quota_exhausted")
            self.assertIn("лимит", self.notify.call_args.args[1])
            self.now += 301
            self.run_poll()
            self.get.assert_not_called()
            self.notify.assert_called_once()

    def test_provider_free_access_limit_also_stops_watching(self):
        self.watch()
        self.get.return_value = response({}, status=429)
        self.run_poll()
        self.assertEqual(routes.snapshot()["watches"][0]["status"], "quota_exhausted")
        calls = self.get.call_count
        self.now += 301
        self.run_poll()
        self.assertEqual(self.get.call_count, calls)

    def test_unknown_traffic_does_not_arm_and_never_means_empty_roads(self):
        self.get.return_value = response(provider(3000, traffic="unknown"))
        r = routes.execute("route_watch", {"origin": A, "destination": B, "threshold_minutes": 60}, {}, 42)
        self.assertIn("Недостаточно", r["error"])
        self.assertEqual(routes.snapshot()["watches"], [])

    def test_background_ignores_faster_variant_with_unknown_traffic(self):
        self.watch()
        data = provider(3000, 4000)
        data["routes"][0]["legs"][0]["annotation"]["congestion"] = ["unknown"]
        self.get.return_value = response(data)
        self.run_poll()
        self.notify.assert_not_called()

    def test_invalid_route_and_disabled_traffic_cannot_trigger(self):
        self.watch()
        for data in (provider(3000, traffic="disabled"), provider(3000, traffic="unknown"), provider(float("nan")),
                     provider(-1), provider(0), {"code": "Ok", "routes": []},
                     {"code": "Ok", "routes": [{"legs": []}]}):
            self.get.return_value = response(data)
            self.now += 301
            self.run_poll()
            self.assertEqual(routes.snapshot()["watches"][0]["status"], "watching")
        self.notify.assert_not_called()

    def test_strict_hour_threshold_and_one_notification(self):
        self.watch()
        self.get.return_value = response(provider(3600, 4000))
        self.run_poll()
        self.notify.assert_not_called()
        self.get.return_value = response(provider(3599, 4100))
        self.now += 301
        self.run_poll()
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.notify.call_args.args[0], 42)
        self.assertIn("строго меньше", self.notify.call_args.args[1])
        self.assertEqual(routes.snapshot()["watches"][0]["status"], "done")
        self.now += 301
        self.run_poll()
        self.assertEqual(self.notify.call_count, 1)

    def test_watch_already_below_is_due_on_first_tick(self):
        self.get.return_value = response(provider(3000))
        self.assertTrue(self.watch()["already_below"])
        self.run_poll()
        self.notify.assert_called_once()

    def test_retry_delivery_requires_fresh_route(self):
        self.watch()
        self.get.return_value = response(provider(3200))
        self.notify.return_value = False
        self.run_poll()
        self.assertEqual(routes.snapshot()["watches"][0]["status"], "watching")
        self.notify.return_value = True
        self.get.return_value = response(provider(4200))
        self.now += 301
        self.run_poll()
        self.assertEqual(self.notify.call_count, 1)
        self.get.return_value = response(provider(3100))
        self.now += 301
        self.run_poll()
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(routes.snapshot()["watches"][0]["status"], "done")

    def test_error_does_not_reuse_previous_result_or_expose_key(self):
        self.watch()
        self.get.side_effect = requests.Timeout("https://example?apikey=ROUTE-SECRET")
        self.run_poll()
        self.notify.assert_not_called()
        r = routes.execute("route_check", {"origin": A, "destination": B})
        self.assertNotIn("SECRET", str(r))
        self.assertTrue(routes.snapshot()["watches"][0]["last_error"])

    def test_interval_expiry_and_restart(self):
        self.watch(expires_in_hours=0.1)
        self.run_poll()
        calls = self.get.call_count
        self.now += 100
        self.run_poll()
        self.assertEqual(self.get.call_count, calls)
        self.now += 301
        self.run_poll()
        self.assertEqual(self.get.call_count, calls)
        self.assertEqual(routes.snapshot()["watches"][0]["status"], "expired")
        self.assertEqual(self.notify.call_count, 1)

    def test_cancel_during_network_and_disable_during_network(self):
        w = self.watch()
        def cancel(*a, **k):
            routes.execute("route_cancel", {"id": w["id"]})
            return response(provider(3000))
        self.get.side_effect = cancel
        self.run_poll()
        self.notify.assert_not_called()
        self.get.side_effect = None
        self.watch()
        def disable(*a, **k):
            self.app["telegramSkills"]["routes"] = False
            return response(provider(3000))
        self.get.side_effect = disable
        self.run_poll()
        self.notify.assert_not_called()

    def test_filters_tolls_and_reports_source_honestly(self):
        self.watch(avoid_tolls=True)
        self.get.return_value = response(provider(3000, tolls=True))
        self.run_poll()
        self.notify.assert_not_called()
        self.now += 301
        self.get.return_value = response(provider(3000))
        self.run_poll()
        self.assertIn("Mapbox с пробками и статистикой", self.notify.call_args.args[1])

    def test_missing_keys_validation_and_unattended(self):
        with patch.dict(os.environ, {"MAPBOX_TOKEN": ""}):
            self.assertIn("токен", routes.execute("route_check", {"origin": A, "destination": B})["error"])
        for threshold in (float("nan"), float("inf"), -1, True, "bad"):
            self.assertIn("error", routes.execute("route_watch", {"origin": A, "destination": B, "threshold_minutes": threshold}, {}, 42))
        self.assertIn("error", routes.execute("route_watch", {}, {}, None))
        self.assertIn("error", routes.execute("route_watch", {}, {}, 42, unattended=True))
        self.assertIn("error", routes.execute("route_save_place", {"name": "Дом", "point": {"lat": float("nan"), "lon": 0}}))

    def test_skill_off_every_entrypoint_and_groups(self):
        off = {"telegramSkills": {"routes": False}}
        self.assertTrue(routes.TOOL_NAMES.isdisjoint(t["name"] for t in jarvis._assistant_tool_list(off, False)))
        self.assertNotIn("МЕСТА И МАРШРУТЫ", jarvis.build_assistant_system_prompt(off))
        for name in routes.TOOL_NAMES:
            self.assertIn("выключен", jarvis.execute_assistant_tool(name, {}, off, "")["error"])
        self.assertEqual(routes.voice_payload(off["telegramSkills"]), {"tools": [], "prompt": ""})
        self.assertIn("routes", {s["id"] for s in jarvis.assistant_skills_catalog()})
        self.assertEqual(tool_groups.unassigned(routes.TOOLS), [])
        self.assertIn("routes", tool_groups.pick("Я в вегас на мкаде, сообщи как только до дома меньше часа"))
        self.watch()
        self.app = off
        self.run_poll()
        self.notify.assert_not_called()

    def test_private_addresses_blocked_for_nonowner(self):
        jarvis.tg_bot.set_ctx(jarvis.tg_bot.Ctx("TOKEN", -100))
        self.addCleanup(jarvis.tg_bot.set_ctx, None)
        with patch.object(jarvis.tg_user, "is_owner_chat", return_value=False):
            self.assertIn("владельцу", jarvis.execute_assistant_tool("route_places", {}, {}, "")["error"])
            self.assertTrue(routes.TOOL_NAMES.isdisjoint(t["name"] for t in jarvis._assistant_tool_list({}, True)))

    def test_keys_stay_private_and_data_backed_up(self):
        routes.configure({"mapbox": "SAVED-SECRET", "tomtom": "OTHER-SECRET"})
        self.watch()
        self.assertNotIn("SECRET", json.dumps(routes.snapshot()))
        self.assertEqual(routes._path(routes.KEYS_NAME).stat().st_mode & 0o777, 0o600)
        # Include a credential file alongside source, to exercise code/ exclusion.
        fake_code = Path(self.tmp.name) / "code"
        fake_code.mkdir()
        (fake_code / routes.KEYS_NAME).write_text("SAVED-SECRET")
        with patch.object(jarvis, "DATA_DIR", Path(self.tmp.name)), patch.object(jarvis, "DIR", fake_code):
            z = zipfile.ZipFile(io.BytesIO(jarvis.build_backup_zip()))
        self.assertIn("data/" + routes.STORE_NAME, z.namelist())
        self.assertFalse(any(routes.KEYS_NAME in n for n in z.namelist()))

    def test_assistant_creates_and_delivers_watch_for_both_models(self):
        for model in ("claude", "openai"):
            routes._path().unlink(missing_ok=True)
            self.notify.reset_mock()
            jarvis.tg_bot.set_ctx(jarvis.tg_bot.Ctx("TOKEN", 42))
            inp = {"origin": A, "destination": B, "threshold_minutes": 60}
            if model == "claude":
                replies = [response({"content": [{"type": "tool_use", "id": "t", "name": "route_watch", "input": inp}]}),
                           response({"content": [{"type": "text", "text": "Проверяю дорогу."}]})]
                fn = jarvis._assistant_loop_claude
            else:
                replies = [response({"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "t", "type": "function", "function": {"name": "route_watch", "arguments": json.dumps(inp)}}]}}]}),
                           response({"choices": [{"message": {"content": "Проверяю дорогу."}, "finish_reason": "stop"}]})]
                fn = jarvis._assistant_loop_openai
            self.get.return_value = response(provider(4500))
            with patch.object(jarvis.tg_user, "is_owner_chat", return_value=True), \
                 patch.object(jarvis, "get_token", return_value="TOKEN"), \
                 patch.object(jarvis.requests, "post", side_effect=replies):
                answer, _ = fn([{"role": "user", "content": "Сообщи, когда до дома меньше часа"}], "key", "model", {}, "")
            self.assertEqual(answer, "Проверяю дорогу.")
            self.assertEqual(routes._read()["watches"][0]["chat_id"], 42)
            self.get.return_value = response(provider(3300))
            self.run_poll()
            self.notify.assert_called_once()
            jarvis.tg_bot.set_ctx(None)


class RoutesHTTPTest(unittest.TestCase):
    setUp = RoutesTest.setUp
    def test_http_auth_config_tools_and_status(self):
        # Use stdlib HTTP client; requests.get is mocked for the map provider.
        from http.client import HTTPConnection
        server = ThreadingHTTPServer(("127.0.0.1", 0), jarvis.JarvisHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
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
            self.assertEqual(call("/api/routes/status")[0], 401)
            self.assertEqual(call("/api/routes/config", {"mapbox": "SECRET"})[0], 401)
        with patch.object(jarvis.JarvisHandler, "_is_authed", return_value=True):
            self.assertEqual(call("/api/routes/config", {"mapbox": "SECRET"})[0], 200)
            status, body = call("/api/routes/status")
            self.assertEqual(status, 200)
            self.assertNotIn(b"SECRET", body)
            body = json.loads(call("/api/routes/tools")[1])
            self.assertEqual({t["name"] for t in body["tools"]}, routes.TOOL_NAMES)
            self.assertEqual(call("/api/routes/tool", {"name": "route_places", "input": []})[0], 200)
            self.assertEqual(call("/api/routes/tool", {"name": "route_places", "input": [1]})[0], 400)
            for url in ("/jarvis_route_keys.json", "/jarvis_route_%6beys.json", "/jarvis_route_keys.json.tmp"):
                self.assertEqual(call(url)[0], 404)


if __name__ == "__main__":
    unittest.main()
