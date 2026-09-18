"""Local-date agenda projection.

Wraps the recurrence expander (:mod:`app.expander`) and re-projects its UTC
instances onto an IANA display timezone as one *segment per local date*:

* a timed instance that crosses local midnight is split at each midnight
  boundary, and every fragment reports ``continues_before`` /
  ``continues_after`` when the same event continues on an adjacent date;
* a ``DATE``-valued (all-day) instance is expanded day by day over its
  floating local date range. Its dates come straight from the ``DATE``
  values and are never re-derived from UTC (a positive-offset display
  timezone would otherwise shift the event to the previous day).

The returned segments are ordered by local date, then all-day first, then
local start time, then ``UID``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import ExpansionError, Instance, expand_calendar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)


class AgendaError(Exception):
    """Invalid agenda input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class AgendaSegment:
    """One event fragment shown on a single local date.

    ``start``/``end`` are naive wall-clock times in the display timezone;
    for an all-day segment they run from local midnight to the next local
    midnight.
    """

    date: dt.date
    uid: str
    summary: str | None
    all_day: bool
    start: dt.datetime
    end: dt.datetime
    continues_before: bool
    continues_after: bool


def build_agenda(
    ics_text: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    tzname: str,
) -> list[AgendaSegment]:
    """Expand ``ics_text`` over the UTC window and project every overlapping
    instance onto local-date segments in the IANA timezone ``tzname``."""
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise AgendaError(f"unknown timezone: {tzname!r}") from exc

    try:
        instances = expand_calendar(ics_text, window_start, window_end)
    except ExpansionError as exc:
        raise AgendaError(str(exc)) from exc

    segments: list[AgendaSegment] = []
    for instance in instances:
        if instance.all_day:
            segments.extend(_all_day_segments(instance))
        else:
            segments.extend(_timed_segments(instance, tz))

    # Local date ascending; within a date: all-day first, then start, then uid.
    segments.sort(key=lambda s: (s.date, not s.all_day, s.start, s.uid))
    return segments


def _timed_segments(instance: Instance, tz) -> list[AgendaSegment]:
    """Split one timed instance at local-midnight boundaries in ``tz``."""
    local_start = instance.start.astimezone(tz)
    local_end = instance.end.astimezone(tz)
    if local_end <= local_start:
        # A zero-duration timed instance carries no span to display.
        return []

    segments: list[AgendaSegment] = []
    date = local_start.date()
    while True:
        day_start = dt.datetime.combine(date, dt.time.min, tzinfo=tz)
        day_end = day_start + DAY
        seg_start = max(local_start, day_start)
        seg_end = min(local_end, day_end)
        if seg_end > seg_start:
            segments.append(
                AgendaSegment(
                    date=date,
                    uid=instance.uid,
                    summary=instance.summary,
                    all_day=False,
                    start=seg_start.replace(tzinfo=None),
                    end=seg_end.replace(tzinfo=None),
                    continues_before=local_start < day_start,
                    continues_after=local_end > day_end,
                )
            )
        if local_end <= day_end:
            break
        date += DAY
    return segments


def _all_day_segments(instance: Instance) -> list[AgendaSegment]:
    """Emit one segment per floating local date of a DATE-valued instance.

    DATE values carry no timezone: the expander parked them at midnight UTC
    for timeline calculations only, so here we read the local dates directly
    rather than converting the UTC interval to the display timezone.
    """
    first_date = instance.start.astimezone(UTC).date()
    day_count = (instance.end - instance.start) // DAY
    if day_count < 1:
        day_count = 1

    segments: list[AgendaSegment] = []
    for index in range(day_count):
        date = first_date + index * DAY
        segments.append(
            AgendaSegment(
                date=date,
                uid=instance.uid,
                summary=instance.summary,
                all_day=True,
                start=dt.datetime.combine(date, dt.time.min),
                end=dt.datetime.combine(date + DAY, dt.time.min),
                continues_before=index > 0,
                continues_after=index < day_count - 1,
            )
        )
    return segments
