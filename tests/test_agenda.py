"""Key tests for the /agenda endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//agenda-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def agenda(
    ics: str,
    tz: str = "Europe/Berlin",
    window_start="2026-03-01T00:00:00Z",
    window_end="2026-04-01T00:00:00Z",
):
    return client.post(
        "/agenda",
        json={
            "ics": ics,
            "window_start": window_start,
            "window_end": window_end,
            "timezone": tz,
        },
    )


def compact(resp) -> list[tuple]:
    return [
        (
            s["date"],
            s["uid"],
            s["all_day"],
            s["start"],
            s["end"],
            s["continues_before"],
            s["continues_after"],
        )
        for s in resp.json()["segments"]
    ]


# ---------------------------------------------------------------------------
# Timed events: local wall times and midnight splitting
# ---------------------------------------------------------------------------

def test_timed_event_is_projected_to_local_wall_time():
    ics = cal(event(
        "UID:mtg",
        "DTSTART:20260310T140000Z",
        "DTEND:20260310T153000Z",
        "SUMMARY:Sync",
    ))
    resp = agenda(ics, tz="Europe/Berlin")  # UTC+1 in March
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == "Europe/Berlin"
    assert body["count"] == 1
    seg = body["segments"][0]
    assert seg["date"] == "2026-03-10"
    assert seg["uid"] == "mtg"
    assert seg["summary"] == "Sync"
    assert seg["all_day"] is False
    # Naive wall-clock times in the display timezone, no offset suffix.
    assert seg["start"] == "2026-03-10T15:00:00"
    assert seg["end"] == "2026-03-10T16:30:00"
    assert seg["continues_before"] is False
    assert seg["continues_after"] is False


def test_timed_event_crossing_local_midnight_is_split():
    # 22:30 Berlin -> 01:30 next day (21:30Z -> 00:30Z next day).
    ics = cal(event(
        "UID:night",
        "DTSTART;TZID=Europe/Berlin:20260310T223000",
        "DTEND;TZID=Europe/Berlin:20260311T013000",
        "SUMMARY:Late call",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    assert compact(resp) == [
        ("2026-03-10", "night", False,
         "2026-03-10T22:30:00", "2026-03-11T00:00:00", False, True),
        ("2026-03-11", "night", False,
         "2026-03-11T00:00:00", "2026-03-11T01:30:00", True, False),
    ]


def test_multi_day_timed_event_has_middle_fragment_continuing_both_ways():
    # Three-day timed span across two midnight boundaries.
    ics = cal(event(
        "UID:workshop",
        "DTSTART;TZID=Europe/Berlin:20260310T080000",
        "DTEND;TZID=Europe/Berlin:20260312T180000",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [s["date"] for s in segs] == ["2026-03-10", "2026-03-11", "2026-03-12"]
    assert (segs[0]["continues_before"], segs[0]["continues_after"]) == (False, True)
    assert (segs[1]["continues_before"], segs[1]["continues_after"]) == (True, True)
    assert (segs[2]["continues_before"], segs[2]["continues_after"]) == (True, False)
    assert segs[1]["start"] == "2026-03-11T00:00:00"
    assert segs[1]["end"] == "2026-03-12T00:00:00"


def test_event_ending_exactly_at_local_midnight_is_a_single_fragment():
    ics = cal(event(
        "UID:before-midnight",
        "DTSTART;TZID=Europe/Berlin:20260310T220000",
        "DTEND;TZID=Europe/Berlin:20260311T000000",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["date"] == "2026-03-10"
    assert segs[0]["end"] == "2026-03-11T00:00:00"
    assert segs[0]["continues_after"] is False


def test_midnight_split_is_done_in_display_timezone_not_utc():
    # 15:00Z -> 18:00Z on Mar 10: entirely within one day in Berlin
    # (16:00-19:00) but the same UTC span crosses Tokyo midnight twice
    # (Mar 11 00:00-03:00 local) -> one segment there too, on Mar 11.
    ics = cal(event(
        "UID:z",
        "DTSTART:20260310T150000Z",
        "DTEND:20260310T180000Z",
    ))
    berlin = agenda(ics, tz="Europe/Berlin")
    tokyo = agenda(ics, tz="Asia/Tokyo")  # UTC+9
    b = berlin.json()["segments"]
    t = tokyo.json()["segments"]
    assert len(b) == 1 and b[0]["date"] == "2026-03-10"
    assert len(t) == 1 and t[0]["date"] == "2026-03-11"
    assert t[0]["start"] == "2026-03-11T00:00:00"
    assert t[0]["end"] == "2026-03-11T03:00:00"


def test_dst_spring_forward_split_is_wall_clock_correct():
    # America/New_York springs forward at 2026-03-08 02:00 local (that wall
    # time does not exist). A fixed-duration event from 23:00 Mar 7 to
    # 23:00 Mar 8 local anchors is converted to UTC by icalendar: it spans
    # Mar 7 23:00 wall -> Mar 9 00:00 wall, i.e. its tail lands exactly on
    # the next midnight and produces no Mar 9 fragment.
    ics = cal(event(
        "UID:dst",
        "DTSTART;TZID=America/New_York:20260307T230000",
        "DTEND;TZID=America/New_York:20260308T230000",
    ))
    resp = agenda(ics, tz="America/New_York")
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [(s["date"], s["start"], s["end"]) for s in segs] == [
        ("2026-03-07", "2026-03-07T23:00:00", "2026-03-08T00:00:00"),
        ("2026-03-08", "2026-03-08T00:00:00", "2026-03-09T00:00:00"),
    ]
    assert segs[0]["continues_after"] is True
    assert segs[1]["continues_before"] is True


def test_dst_fall_back_midnight_split_uses_wall_boundaries():
    # America/New_York falls back 2026-11-01 02:00 -> 01:00. The instance
    # runs 20:00 Oct 31 wall to 20:00 Nov 1 wall; icalendar resolves the
    # ambiguous end to its first (EDT) occurrence, i.e. 00:00Z Nov 2, so
    # the second fragment ends at 19:00 once shown in final EST. The split
    # itself is at the wall-clock midnight regardless.
    ics = cal(event(
        "UID:dst",
        "DTSTART;TZID=America/New_York:20261031T200000",
        "DTEND;TZID=America/New_York:20261101T200000",
    ))
    resp = agenda(
        ics,
        tz="America/New_York",
        window_start="2026-10-01T00:00:00Z",
        window_end="2026-11-10T00:00:00Z",
    )
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [(s["date"], s["start"], s["end"]) for s in segs] == [
        ("2026-10-31", "2026-10-31T20:00:00", "2026-11-01T00:00:00"),
        ("2026-11-01", "2026-11-01T00:00:00", "2026-11-01T19:00:00"),
    ]
    assert segs[0]["continues_after"] is True
    assert segs[1]["continues_before"] is True


# ---------------------------------------------------------------------------
# DATE all-day events
# ---------------------------------------------------------------------------

def test_single_day_all_day_event_uses_its_date_not_utc_midnight():
    # DATE 20260310 shown to a UTC+9 viewer: must stay on Mar 10 even
    # though midnight UTC is 09:00 local on Mar 10.
    ics = cal(event(
        "UID:holiday",
        "DTSTART;VALUE=DATE:20260310",
        "SUMMARY:Holiday",
    ))
    resp = agenda(ics, tz="Asia/Tokyo")
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert len(segs) == 1
    seg = segs[0]
    assert seg["date"] == "2026-03-10"
    assert seg["all_day"] is True
    assert seg["start"] == "2026-03-10T00:00:00"
    assert seg["end"] == "2026-03-11T00:00:00"
    assert seg["continues_before"] is False
    assert seg["continues_after"] is False


def test_multi_day_all_day_event_emits_one_segment_per_date():
    # DTEND is exclusive: Mar 10 and Mar 11 (not Mar 12).
    ics = cal(event(
        "UID:trip",
        "DTSTART;VALUE=DATE:20260310",
        "DTEND;VALUE=DATE:20260312",
        "SUMMARY:Trip",
    ))
    resp = agenda(ics, tz="America/Los_Angeles")  # UTC-8: UTC midnight would
                                                  # shift to the previous day
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [(s["date"], s["all_day"]) for s in segs] == [
        ("2026-03-10", True),
        ("2026-03-11", True),
    ]
    assert segs[0]["start"] == "2026-03-10T00:00:00"
    assert segs[0]["end"] == "2026-03-11T00:00:00"
    assert segs[0]["continues_after"] is True
    assert segs[1]["continues_before"] is True


def test_all_day_dates_do_not_change_between_opposite_offsets():
    ics = cal(event(
        "UID:trip",
        "DTSTART;VALUE=DATE:20260310",
        "DTEND;VALUE=DATE:20260311",
    ))
    tokyo = agenda(ics, tz="Asia/Tokyo").json()["segments"]
    la = agenda(ics, tz="America/Los_Angeles").json()["segments"]
    assert [s["date"] for s in tokyo] == ["2026-03-10"]
    assert [s["date"] for s in la] == ["2026-03-10"]


# ---------------------------------------------------------------------------
# Ordering: date, all-day first, start, uid
# ---------------------------------------------------------------------------

def test_segments_are_sorted_date_then_allday_then_start_then_uid():
    ics = cal(
        event("UID:zzz-late",
              "DTSTART;TZID=Europe/Berlin:20260310T223000",
              "DTEND;TZID=Europe/Berlin:20260311T013000"),
        event("UID:day-two",
              "DTSTART;VALUE=DATE:20260311"),
        event("UID:day-one",
              "DTSTART;VALUE=DATE:20260310"),
        event("UID:aaa-morning",
              "DTSTART;TZID=Europe/Berlin:20260310T090000",
              "DTEND;TZID=Europe/Berlin:20260310T100000"),
        event("UID:bbb-morning",
              "DTSTART;TZID=Europe/Berlin:20260310T090000",
              "DTEND;TZID=Europe/Berlin:20260310T100000"),
    )
    resp = agenda(ics)
    assert resp.status_code == 200
    order = [(s["date"], s["all_day"], s["uid"]) for s in resp.json()["segments"]]
    assert order == [
        ("2026-03-10", True, "day-one"),       # all-day first on the date
        ("2026-03-10", False, "aaa-morning"),  # same start -> uid
        ("2026-03-10", False, "bbb-morning"),
        ("2026-03-10", False, "zzz-late"),     # evening fragment
        ("2026-03-11", True, "day-two"),       # all-day before the carryover
        ("2026-03-11", False, "zzz-late"),     # continues_after midnight
    ]


# ---------------------------------------------------------------------------
# Window handling, recurrence reuse, cancellations
# ---------------------------------------------------------------------------

def test_agenda_reuses_recurrence_expansion():
    ics = cal(event(
        "UID:daily",
        "DTSTART;TZID=Europe/Berlin:20260309T090000",
        "DTEND;TZID=Europe/Berlin:20260309T100000",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE;TZID=Europe/Berlin:20260310T090000",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    assert [s["date"] for s in resp.json()["segments"]] == [
        "2026-03-09",
        "2026-03-11",
    ]


def test_cancelled_instances_never_appear():
    ics = cal(
        event("UID:one", "DTSTART:20260310T090000Z", "STATUS:CANCELLED"),
        event("UID:two", "DTSTART:20260310T100000Z", "DTEND:20260310T110000Z"),
    )
    resp = agenda(ics)
    assert [s["uid"] for s in resp.json()["segments"]] == ["two"]


def test_window_overlap_uses_utc_window():
    # Mar 9 23:00Z -> Mar 10 01:00Z; a window starting Mar 10 00:00Z still
    # includes it. Berlin (UTC+1): fragments Mar 10 00:00-01:00 and
    # Mar 10 01:00-02:00 -> both on the local Mar 10.
    ics = cal(event(
        "UID:edge",
        "DTSTART:20260309T230000Z",
        "DTEND:20260310T010000Z",
    ))
    resp = agenda(
        ics,
        window_start="2026-03-10T00:00:00Z",
        window_end="2026-03-11T00:00:00Z",
        tz="Europe/Berlin",
    )
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [(s["date"], s["continues_before"]) for s in segs] == [
        ("2026-03-10", False),
    ]
    # The instance began before the window but at/after local midnight here,
    # so no "continues_before"; its wall start is 00:00 local.
    assert segs[0]["start"] == "2026-03-10T00:00:00"
    assert segs[0]["end"] == "2026-03-10T02:00:00"


def test_carryover_from_before_window_start_is_flagged():
    # Event starts Mar 9 21:00Z (22:00 Berlin) and ends Mar 10 05:00Z
    # (06:00 Berlin). The window begins at Mar 10 00:00Z (01:00 Berlin);
    # the instance overlaps it, so both of its local-date fragments are
    # shown with their full wall-clock extent (the window only selects
    # instances, it never clips a segment's display times).
    ics = cal(event(
        "UID:long",
        "DTSTART:20260309T210000Z",
        "DTEND:20260310T050000Z",
    ))
    resp = agenda(
        ics,
        window_start="2026-03-10T00:00:00Z",
        window_end="2026-03-10T12:00:00Z",
        tz="Europe/Berlin",
    )
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert [(s["date"], s["start"], s["end"]) for s in segs] == [
        ("2026-03-09", "2026-03-09T22:00:00", "2026-03-10T00:00:00"),
        ("2026-03-10", "2026-03-10T00:00:00", "2026-03-10T06:00:00"),
    ]
    assert segs[0]["continues_before"] is False
    assert segs[0]["continues_after"] is True
    assert segs[1]["continues_before"] is True
    assert segs[1]["continues_after"] is False


def test_empty_window_produces_empty_agenda():
    ics = cal(event(
        "UID:may",
        "DTSTART;VALUE=DATE:20260501",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["segments"] == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_unknown_timezone_returns_422():
    ics = cal(event("UID:a", "DTSTART:20260310T090000Z"))
    resp = agenda(ics, tz="Mars/Olympus_Mons")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]
    assert "Mars/Olympus_Mons" in resp.json()["detail"]


def test_invalid_calendar_returns_422():
    resp = agenda("not an icalendar document", tz="Europe/Berlin")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_unknown_tzid_inside_ics_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260310T090000",
    ))
    resp = agenda(ics, tz="Europe/Berlin")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_inverted_window_returns_422():
    ics = cal(event("UID:a", "DTSTART:20260310T090000Z"))
    resp = agenda(
        ics,
        tz="Europe/Berlin",
        window_start="2026-04-01T00:00:00Z",
        window_end="2026-03-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_missing_field_is_a_schema_422():
    resp = client.post(
        "/agenda",
        json={"ics": "x", "window_start": "2026-03-01T00:00:00Z"},
    )
    assert resp.status_code == 422
