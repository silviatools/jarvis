"""Calendar read → edit → read regressions with an in-memory CalDAV server."""

import html
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import calendar_ics
import jarvis


def resource(*events):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR", ""])


SERIES = "\r\n".join([
    "BEGIN:VEVENT", "UID:series", "SUMMARY:WB + OZON", "SEQUENCE:4",
    "DTSTART;TZID=Europe/Moscow:20261103T090000",
    "DTEND;TZID=Europe/Moscow:20261103T103000", "RRULE:FREQ=WEEKLY;COUNT=4",
    "LOCATION:Office", "X-APPLE-CUSTOM:keep-me", "URL:https://example.org/call",
    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "DESCRIPTION:Reminder",
    "END:VALARM", "END:VEVENT",
])


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.text = resource(SERIES)
        self.etag = '"v1"'
        self.writes = []
        self.cal = {"name": "Работа", "href": "https://calendar.test/work/"}
        for target, value in (
            ("apple_discover", lambda: {"calendars": [self.cal], "todoLists": []}),
            ("_apple_dav", self.dav),
        ):
            mock = patch.object(jarvis, target, value)
            mock.start()
            self.addCleanup(mock.stop)

    def dav(self, method, url, body=None, **kwargs):
        if method == "REPORT":
            xml = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                   '<d:response><d:href>/work/series.ics</d:href><d:propstat><d:prop>'
                   f'<d:getetag>{html.escape(self.etag)}</d:getetag>'
                   f'<c:calendar-data>{html.escape(self.text)}</c:calendar-data>'
                   '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                   '</d:response></d:multistatus>')
            return SimpleNamespace(status_code=207, content=xml.encode())
        self.assertEqual(method, "PUT")
        self.assertEqual(kwargs["headers"]["If-Match"], self.etag)
        self.writes.append(body)
        self.text = body
        self.etag = '"v2"'
        return SimpleNamespace(status_code=204)

    def read(self, start="2026-11-10", end="2026-11-10"):
        return jarvis.apple_list_events(start, end)["events"]

    def move(self, event, **fields):
        return jarvis.execute_apple_tool("calendar_update_event", {
            "id": event["id"], "calendar": event["calendar"],
            "recurrenceId": event["recurrenceId"], "confirmed": True, **fields,
        })

    def test_today_is_expanded_from_old_master(self):
        event, = self.read()
        self.assertEqual(event["start"], "2026-11-10 09:00")
        self.assertTrue(event["recurring"])
        self.assertEqual(event["recurrenceId"], "2026-11-10T09:00:00+03:00")

    def test_move_one_occurrence_preserves_series_duration_and_alarm(self):
        result = self.move(self.read()[0], start="2026-11-11 12:00", notes="New notes")
        self.assertTrue(result.get("ok"), result)
        self.assertIn(SERIES, self.text)
        self.assertEqual(self.read(), [])
        moved, = self.read("2026-11-11", "2026-11-11")
        self.assertEqual((moved["start"], moved["end"]), ("2026-11-11 12:00", "2026-11-11 13:30"))
        self.assertEqual(moved["recurrenceId"], "2026-11-10T09:00:00+03:00")
        events = self.read("2026-11-01", "2026-11-30")
        self.assertEqual([e["start"] for e in events], [
            "2026-11-03 09:00", "2026-11-11 12:00", "2026-11-17 09:00", "2026-11-24 09:00"])
        override = calendar_ics.parse(self.text).walk("VEVENT")[1]
        self.assertNotIn("RRULE", override)
        self.assertEqual(str(override["URL"]), "https://example.org/call")
        self.assertEqual(str(override["X-APPLE-CUSTOM"]), "keep-me")
        self.assertEqual(str(override.subcomponents[0]["DESCRIPTION"]), "Reminder")

    def test_move_existing_exception_twice(self):
        self.assertTrue(self.move(self.read()[0], start="2026-11-11 12:00")["ok"])
        moved = self.read("2026-11-11", "2026-11-11")[0]
        self.assertTrue(self.move(moved, start="2026-11-12 14:00", title="Changed")["ok"])
        self.assertEqual(len(calendar_ics.parse(self.text).walk("VEVENT")), 2)
        self.assertEqual(self.read("2026-11-11", "2026-11-11"), [])
        event, = self.read("2026-11-12", "2026-11-12")
        self.assertEqual(event["end"], "2026-11-12 15:30")
        self.assertIn(SERIES, self.text)

    def test_missing_wrong_or_excluded_identity_does_not_write(self):
        self.text = self.text.replace("RRULE:", "EXDATE;TZID=Europe/Moscow:20261117T090000\r\nRRULE:")
        for identity in (None, "bad", "2026-11-09T09:00:00+03:00", "2026-11-17T09:00:00+03:00"):
            result = jarvis.execute_apple_tool("calendar_update_event", {
                "id": "series", "recurrenceId": identity, "start": "2026-11-11 12:00", "confirmed": True})
            self.assertIn("error", result)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.read("2026-11-17", "2026-11-17"), [])

    def test_confirmation_required(self):
        result = jarvis.execute_apple_tool("calendar_update_event", {
            "id": "series", "start": "2026-11-11 12:00"})
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(self.writes, [])

    def test_all_day_rdate_and_exdate(self):
        self.text = resource("\r\n".join([
            "BEGIN:VEVENT", "UID:series", "SUMMARY:All day", "DTSTART;VALUE=DATE:20261103",
            "DTEND;VALUE=DATE:20261104", "RRULE:FREQ=WEEKLY;COUNT=4",
            "RDATE;VALUE=DATE:20261112", "EXDATE;VALUE=DATE:20261117", "END:VEVENT"]))
        event, = self.read()
        self.assertEqual(event["recurrenceId"], "2026-11-10")
        self.assertTrue(self.move(event, start="2026-11-11")["ok"])
        self.assertEqual(self.read(), [])
        self.assertEqual(self.read("2026-11-11", "2026-11-11")[0]["end"], "2026-11-12")
        self.assertEqual(len(self.read("2026-11-12", "2026-11-12")), 1)
        self.assertEqual(self.read("2026-11-17", "2026-11-17"), [])

    def test_utc_identity_and_floating_times(self):
        for start, end in (("DTSTART:20261103T060000Z", "DTEND:20261103T073000Z"),
                           ("DTSTART:20261103T090000", "DTEND:20261103T103000")):
            self.text = resource(SERIES.replace("DTSTART;TZID=Europe/Moscow:20261103T090000", start)
                                 .replace("DTEND;TZID=Europe/Moscow:20261103T103000", end))
            event, = self.read()
            self.assertEqual(event["start"], "2026-11-10 09:00")
            self.assertTrue(self.move(event, start="2026-11-11 12:00")["ok"])
            self.assertEqual(self.read("2026-11-11", "2026-11-11")[0]["end"], "2026-11-11 13:30")

    def test_one_off_and_cancelled_event(self):
        self.text = resource(SERIES.replace("RRULE:FREQ=WEEKLY;COUNT=4\r\n", ""))
        event, = self.read("2026-11-03", "2026-11-03")
        self.assertFalse(event["recurring"])
        self.assertEqual(event["recurrenceId"], "")
        self.assertTrue(self.move(event, start="2026-11-10 12:00")["ok"])
        self.assertEqual(self.read()[0]["end"], "2026-11-10 13:30")
        self.text = self.text.replace("END:VEVENT", "STATUS:CANCELLED\r\nEND:VEVENT")
        self.assertEqual(self.read(), [])

    def test_multiday_duration_and_other_exception_are_preserved(self):
        self.text = resource("\r\n".join([
            "BEGIN:VEVENT", "UID:series", "SUMMARY:Trip", "DTSTART;VALUE=DATE:20261103",
            "DURATION:P3D", "RRULE:FREQ=WEEKLY;COUNT=4", "END:VEVENT"]))
        event, = self.read()
        self.assertTrue(self.move(event, start="2026-11-11")["ok"])
        moved, = self.read("2026-11-11", "2026-11-11")
        self.assertEqual(moved["end"], "2026-11-14")
        before = self.text
        another = self.read("2026-11-17", "2026-11-17")[0]
        self.assertTrue(self.move(another, start="2026-11-18")["ok"])
        self.assertEqual(self.read("2026-11-11", "2026-11-11"), [moved])
        # The earlier exception is retained byte for byte.
        previous_override = before.split("BEGIN:VEVENT")[2].split("END:VEVENT")[0]
        self.assertIn(previous_override, self.text)

    def test_rdate_only_and_missing_etag(self):
        self.text = resource(SERIES.replace("RRULE:FREQ=WEEKLY;COUNT=4", "RDATE;TZID=Europe/Moscow:20261110T090000"))
        event, = self.read()
        self.assertTrue(event["recurring"])
        self.etag = ""
        self.assertIn("error", self.move(event, start="2026-11-11 12:00"))
        self.assertEqual(self.writes, [])
        self.etag = '"v1"'
        self.assertTrue(self.move(event, start="2026-11-11 12:00")["ok"])
        self.assertEqual(self.read(), [])

    def test_frontend_and_backend_update_schemas_match(self):
        import json
        import subprocess
        from pathlib import Path
        page = Path(jarvis.DIR / "index (9).html").read_text()
        js = page.split("const APPLE_AGENT_TOOLS = ", 1)[1].split("\n];", 1)[0] + "\n]"
        result = subprocess.run(["node", "-e", "console.log(JSON.stringify(" + js + "))"],
                                check=True, capture_output=True, text=True)
        tools = json.loads(result.stdout)
        for name in ("calendar_list", "calendar_create_event", "calendar_update_event"):
            browser = next(t for t in tools if t["name"] == name)
            server = next(t for t in jarvis.APPLE_TOOLS if t["name"] == name)
            self.assertEqual(browser, server)

    def test_failed_read_is_not_an_empty_calendar(self):
        with patch.object(jarvis, "_apple_report", return_value=SimpleNamespace(status_code=503)):
            result = jarvis.execute_apple_tool("calendar_list_events", {"from": "2026-11-10"})
        self.assertIn("503", result["error"])

    def test_conflict_does_not_report_success(self):
        event = self.read()[0]
        with patch.object(jarvis, "_apple_dav", wraps=self.dav) as dav:
            def conflict(method, *args, **kwargs):
                return SimpleNamespace(status_code=412) if method == "PUT" else self.dav(method, *args, **kwargs)
            dav.side_effect = conflict
            result = self.move(event, start="2026-11-11 12:00")
        self.assertIn("error", result)
        self.assertEqual(self.writes, [])

    def test_find_requires_exact_uid(self):
        self.text = self.text.replace("UID:series", "UID:series-longer")
        with self.assertRaises(jarvis.AppleError):
            jarvis._apple_find("series", "VEVENT")


class CalendarChoiceTest(unittest.TestCase):
    def setUp(self):
        self.puts = []
        self.cals = [
            {"name": "Питание", "href": "https://calendar.test/food/"},
            {"name": "Работа", "href": "https://calendar.test/work/"},
            {"name": "Рабочие", "href": "https://calendar.test/jobs/"},
            {"name": "Здоровье", "href": "https://calendar.test/health/"},
            {"name": "Ёлки", "href": "https://calendar.test/trees/"},
        ]
        for target, value in (
            ("apple_discover", lambda force=False: {"calendars": list(self.cals), "todoLists": []}),
            ("_apple_dav", self.dav),
        ):
            mock = patch.object(jarvis, target, value)
            mock.start()
            self.addCleanup(mock.stop)

    def dav(self, method, url, body=None, **kwargs):
        self.assertEqual(method, "PUT")
        self.puts.append((url, body))
        return SimpleNamespace(status_code=201)

    def create(self, **fields):
        return jarvis.execute_apple_tool("calendar_create_event", fields)

    def test_list_is_the_discovered_account_not_a_constant(self):
        listed = jarvis.execute_apple_tool("calendar_list", {})
        self.assertEqual(listed["calendars"], [c["name"] for c in self.cals])
        self.cals.append({"name": "Спорт", "href": "https://calendar.test/sport/"})
        self.assertEqual(jarvis.execute_apple_tool("calendar_list", {})["calendars"][-1], "Спорт")
        self.assertEqual(self.puts, [])

    def test_missing_calendar_does_not_write_to_the_first(self):
        result = self.create(title="Стоматолог", start="2026-11-14 15:00", confirmed=True)
        self.assertTrue(result["needs_choice"])
        self.assertNotIn("needs_confirmation", result)
        self.assertEqual(result["calendars"], [c["name"] for c in self.cals])
        self.assertIn("не календарь по умолчанию", result["message"])
        self.assertIn("«Питание»", result["message"])
        self.assertEqual(self.puts, [])

    def test_request_names_work_and_confirmation_says_so(self):
        result = self.create(title="Созвон", start="2026-11-12 10:00", request="созвон по работе")
        self.assertEqual(result["calendar"], "Работа")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["message"], "Добавить «Созвон» (2026-11-12 10:00) в календарь «Работа»?")
        self.assertNotIn("«Питание»", result["message"])
        self.assertEqual(self.puts, [])
        saved = self.create(title="Созвон", start="2026-11-12 10:00", request="созвон по работе", confirmed=True)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["calendar"], "Работа")
        self.assertEqual(len(self.puts), 1)
        self.assertTrue(self.puts[0][0].startswith("https://calendar.test/work/"))
        self.assertIn("SUMMARY:Созвон", self.puts[0][1])

    def test_model_picks_health_after_seeing_the_list(self):
        pending = self.create(title="Стоматолог", start="2026-11-14 15:00", calendar="Здоровье")
        self.assertIn("«Здоровье»", pending["message"])
        self.assertEqual(self.puts, [])
        saved = self.create(title="Стоматолог", start="2026-11-14 15:00", calendar="Здоровье", confirmed=True)
        self.assertEqual(saved["calendar"], "Здоровье")
        self.assertTrue(self.puts[0][0].startswith("https://calendar.test/health/"))

    def test_equal_names_and_unknown_name_ask_instead_of_writing(self):
        tie = self.create(title="План", start="2026-11-12", request="и питание, и работу")
        self.assertTrue(tie["needs_clarification"])
        self.assertEqual(tie["candidates"], ["Питание", "Работа"])
        self.assertIn("одинаково подходят", tie["message"])
        partial = self.create(title="План", start="2026-11-12", calendar="рабо")
        self.assertEqual(partial["candidates"], ["Работа", "Рабочие"])
        missing = self.create(title="План", start="2026-11-12", calendar="Фитнес", confirmed=True)
        self.assertIn("«Фитнес»", missing["message"])
        self.assertEqual(missing["calendars"][0], "Питание")
        self.assertEqual(self.puts, [])

    def test_explicit_calendar_wins_only_after_confirmation(self):
        conflict = self.create(title="Обед", start="2026-11-12 13:00", calendar="Питание", request="добавь в работу")
        self.assertTrue(conflict["needs_clarification"])
        self.assertEqual(conflict["candidates"], ["Питание", "Работа"])
        self.assertEqual(self.puts, [])
        saved = self.create(title="Обед", start="2026-11-12 13:00", calendar="Питание",
                            request="добавь в работу", confirmed=True)
        self.assertEqual(saved["calendar"], "Питание")
        self.assertTrue(self.puts[0][0].startswith("https://calendar.test/food/"))

    def test_inflection_and_yo_use_the_real_name(self):
        work = self.create(title="Планёрка", start="2026-11-12 09:00", request="планёрка на работе")
        self.assertEqual(work["calendar"], "Работа")
        trees = self.create(title="Базар", start="2026-11-20", request="купить елки")
        self.assertEqual(trees["calendar"], "Ёлки")
        self.assertIn("«Ёлки»", trees["message"])
        self.assertEqual(self.puts, [])

    def test_only_calendar_is_named_in_the_confirmation(self):
        self.cals[:] = [{"name": "Питание", "href": "https://calendar.test/food/"}]
        result = self.create(title="Обед", start="2026-11-12 13:00")
        self.assertEqual(result["message"], "Добавить «Обед» (2026-11-12 13:00) в календарь «Питание»?")
        self.assertEqual(self.puts, [])
        saved = self.create(title="Обед", start="2026-11-12 13:00", confirmed=True)
        self.assertTrue(self.puts[0][0].startswith("https://calendar.test/food/"))
        self.assertEqual(saved["calendar"], "Питание")

    def test_confirmation_buttons_name_the_calendar(self):
        sent = {}

        def buttons_send(ctx, text, buttons):
            sent["text"] = text
            sent["buttons"] = buttons
            return {"ok": True}

        with patch.object(jarvis.tg_bot, "get_ctx", return_value=SimpleNamespace(token="t", chat_id=1)), \
             patch.object(jarvis.tg_bot, "buttons_send", buttons_send):
            result = self.create(title="Планёрка", start="2026-11-12 10:00", calendar="Работа")
            choice = self.create(title="Стоматолог", start="2026-11-14 15:00")
        self.assertTrue(result["buttons"])
        self.assertIn("«Работа»", sent["text"])
        self.assertIn("«Работа»", sent["buttons"][0][0]["text"])
        self.assertIn("календарь «Работа»", sent["buttons"][0][0]["reply"])
        self.assertTrue(choice["needs_choice"])
        self.assertNotIn("buttons", choice)
        self.assertEqual(self.puts, [])

    def test_clarification_buttons_offer_the_close_calendars(self):
        sent = {}

        def buttons_send(ctx, text, buttons):
            sent["buttons"] = buttons
            return {"ok": True}

        with patch.object(jarvis.tg_bot, "get_ctx", return_value=SimpleNamespace(token="t", chat_id=1)), \
             patch.object(jarvis.tg_bot, "buttons_send", buttons_send):
            result = self.create(title="План", start="2026-11-12", calendar="рабо")
        self.assertTrue(result["buttons"])
        labels = [b["text"] for row in sent["buttons"] for b in row]
        self.assertEqual(labels[:2], ["Работа", "Рабочие"])
        self.assertEqual(self.puts, [])

    def test_prompts_tell_the_model_to_choose_from_the_real_list(self):
        full = jarvis.build_assistant_system_prompt({"telegramSkills": {}}, False)
        compact = jarvis.build_assistant_system_prompt({"telegramSkills": {}}, False, compact=True)
        page = (jarvis.DIR / "index (9).html").read_text(encoding="utf-8")
        for blob in (full, compact, page):
            self.assertIn("calendar_list", blob)
            self.assertIn("одинаково близки", blob)
            self.assertIn("название календаря", blob)
        self.assertIn("не календарь по умолчанию", full)
        self.assertIn("не первый", compact)


if __name__ == "__main__":
    unittest.main()
