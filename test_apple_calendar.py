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
        browser = next(t for t in json.loads(result.stdout) if t["name"] == "calendar_update_event")
        server = next(t for t in jarvis.APPLE_TOOLS if t["name"] == "calendar_update_event")
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


if __name__ == "__main__":
    unittest.main()
