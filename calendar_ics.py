"""Local recurrence expansion and isolated edits of iCalendar occurrences."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar
import recurring_ical_events


MSK = ZoneInfo("Europe/Moscow")


def parse(text):
    return Calendar.from_ical(text)


def occurrences(calendar, start, end):
    # Floating times have the same Moscow interpretation as the rest of Jarvis.
    calendar = deepcopy(calendar)
    for event in calendar.walk("VEVENT"):
        for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            prop = event.get(name)
            if prop is not None and isinstance(prop.dt, datetime) and prop.dt.tzinfo is None:
                prop.dt = prop.dt.replace(tzinfo=MSK)
    return [e for e in recurring_ical_events.of(calendar).between(start, end)
            if str(e.get("STATUS", "")).upper() != "CANCELLED"]


def recurrence_id(event):
    prop = event.get("RECURRENCE-ID")
    return prop.dt.isoformat() if prop is not None else ""


def recurring(calendar):
    return any(any(name in e for name in ("RRULE", "RDATE", "RECURRENCE-ID"))
               for e in calendar.walk("VEVENT"))


def edit_occurrence(text, uid, recurrence, patcher):
    """Return a resource with one detached exception; retain the original master.

    recurrence is the original occurrence identity, even after earlier moves.
    patcher edits a single VEVENT, including its existing alarms and metadata.
    """
    calendar = parse(text)
    try:
        wanted = (date.fromisoformat(recurrence) if len(recurrence) == 10
                  else datetime.fromisoformat(recurrence.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        raise ValueError("Нужен recurrenceId из calendar_list_events.")
    if isinstance(wanted, datetime) and wanted.tzinfo is None:
        wanted = wanted.replace(tzinfo=MSK)

    events = [e for e in calendar.walk("VEVENT") if str(e.get("UID")) == uid]
    if any(e.get("RECURRENCE-ID") is not None and
           e["RECURRENCE-ID"].params.get("RANGE") for e in events):
        raise ValueError("Серия содержит изменение «это и следующие»; отдельная правка пока не поддерживается.")

    def matches(event):
        prop = event.get("RECURRENCE-ID")
        if prop is None:
            return False
        actual = prop.dt
        if isinstance(actual, datetime) and actual.tzinfo is None:
            actual = actual.replace(tzinfo=MSK)
        return actual == wanted

    existing = next((e for e in events if matches(e)), None)
    target = existing
    if target is None:
        stop = wanted + (timedelta(seconds=1) if isinstance(wanted, datetime) else timedelta(days=1))
        target = next((e for e in occurrences(calendar, wanted, stop)
                       if str(e.get("UID")) == uid and matches(e)), None)
    if target is None or str(target.get("STATUS", "")).upper() == "CANCELLED":
        raise ValueError("Это вхождение серии не найдено. Повтори calendar_list_events за нужный день.")
    target = deepcopy(target)
    for name in ("RRULE", "RDATE", "EXDATE", "EXRULE"):
        target.pop(name, None)
    if "DURATION" in target:
        if "DTEND" not in target:
            target.add("DTEND", target.decoded("DTSTART") + target.decoded("DURATION"))
        target.pop("DURATION")
    # Only serialize the exception; leave the master, VTIMEZONE and siblings intact.
    replacement = patcher(target.to_ical().decode("utf-8")).strip()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if existing is not None:
        index = events.index(existing)
        spans, begin = [], None
        for i, line in enumerate(lines):
            if line.upper() == "BEGIN:VEVENT":
                begin = i
            elif line.upper() == "END:VEVENT" and begin is not None:
                spans.append((begin, i + 1))
        begin, end = spans[index]
        lines[begin:end] = replacement.splitlines()
    else:
        end = next(i for i, line in enumerate(lines) if line.upper() == "END:VCALENDAR")
        lines[end:end] = replacement.splitlines()
    return "\r\n".join(lines)
