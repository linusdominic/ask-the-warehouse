"""Business time. "Today" and "last week" mean one thing, defined here, in the company's zone."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, model_validator

Preset = Literal[
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_7_days",
    "last_30_days",
]

PRESET_HELP = {
    "today": "midnight today up to now",
    "yesterday": "the whole of yesterday",
    "this_week": "Monday of this week up to now",
    "last_week": "Monday to Sunday of the previous week",
    "this_month": "the 1st of this month up to now",
    "last_month": "the whole previous calendar month",
    "last_7_days": "the 7 complete days before today (today excluded)",
    "last_30_days": "the 30 complete days before today (today excluded)",
}


class TimeRange(BaseModel):
    """Either a named preset, or explicit dates (both inclusive) in the business time zone."""

    preset: Preset | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _one_form(self) -> "TimeRange":
        dated = self.start_date is not None and self.end_date is not None
        if (self.preset is not None) == dated or (self.start_date is None) != (
            self.end_date is None
        ):
            raise ValueError("give either preset, or both start_date and end_date")
        if dated and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self


@dataclass(frozen=True)
class Window:
    start: datetime  # inclusive
    end: datetime  # exclusive
    label: str


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _first_of_previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _shift_days(moment: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Move back by whole days on the wall clock, so a daylight-saving change does not
    shift the time of day."""
    local = moment.astimezone(tz).replace(tzinfo=None) - timedelta(days=days)
    return local.replace(tzinfo=tz)


_DAYS_BACK = {
    "today": 1,
    "yesterday": 1,
    "this_week": 7,
    "last_week": 7,
    "last_7_days": 7,
    "last_30_days": 30,
}


def previous(time_range: TimeRange, window: Window, tz: ZoneInfo) -> Window:
    """The period a window is compared with: the same span, one period earlier.

    A to-date window is compared with the same point in the earlier period: today up to
    14:00 against yesterday up to 14:00, not against the whole of yesterday.
    """
    first, last = window.start.astimezone(tz), window.end.astimezone(tz)
    if time_range.preset in ("this_month", "last_month"):
        start = _midnight(_first_of_previous_month(first.date()), tz)
        # Same elapsed wall-clock time into the earlier month, never past its end.
        elapsed = last.replace(tzinfo=None) - first.replace(tzinfo=None)
        end = min((start.replace(tzinfo=None) + elapsed).replace(tzinfo=tz), window.start)
    else:
        days = _DAYS_BACK.get(time_range.preset or "", max((last.date() - first.date()).days, 1))
        start, end = _shift_days(window.start, days, tz), _shift_days(window.end, days, tz)

    begin, finish = start.astimezone(tz), end.astimezone(tz)
    if (finish.hour, finish.minute, finish.second) != (0, 0, 0):
        same_day = begin.date() == finish.date()
        label = (
            f"{begin:%a %d %b} up to {finish:%H:%M}"
            if same_day
            else f"{begin:%a %d %b} to {finish:%a %d %b %H:%M}"
        )
    elif finish - begin <= timedelta(days=1, hours=1):
        label = f"{begin:%a %d %b}"
    else:
        label = f"{begin:%a %d %b} to {finish - timedelta(days=1):%a %d %b}"
    return Window(start, end, f"the previous period ({label})")


def resolve(time_range: TimeRange, now: datetime, tz: ZoneInfo) -> Window:
    """Turn a time range into exact UTC-comparable bounds, as of ``now``."""
    local = now.astimezone(tz)
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    stamp = local.strftime("%H:%M %Z")

    def span(first: date, after_last: date, label: str) -> Window:
        return Window(_midnight(first, tz), _midnight(after_last, tz), label)

    def to_date(first: date, label: str) -> Window:
        return Window(_midnight(first, tz), now, f"{label}, up to {stamp}")

    if time_range.preset is None:
        first, last = time_range.start_date, time_range.end_date
        label = f"{first:%d %b %Y}" if first == last else f"{first:%d %b %Y} to {last:%d %b %Y}"
        return span(first, last + timedelta(days=1), label)

    match time_range.preset:
        case "today":
            return to_date(today, f"today ({today:%a %d %b})")
        case "yesterday":
            day = today - timedelta(days=1)
            return span(day, today, f"yesterday ({day:%a %d %b})")
        case "this_week":
            return to_date(monday, f"this week (from Mon {monday:%d %b})")
        case "last_week":
            first = monday - timedelta(days=7)
            last = monday - timedelta(days=1)
            return span(first, monday, f"last week (Mon {first:%d %b} to Sun {last:%d %b})")
        case "this_month":
            return to_date(today.replace(day=1), f"this month ({today:%B %Y})")
        case "last_month":
            first = _first_of_previous_month(today)
            return span(first, today.replace(day=1), f"last month ({first:%B %Y})")
        case "last_7_days" | "last_30_days":
            days = 7 if time_range.preset == "last_7_days" else 30
            first = today - timedelta(days=days)
            last = today - timedelta(days=1)
            return span(
                first, today, f"the last {days} complete days ({first:%d %b} to {last:%d %b})"
            )
    raise ValueError(f"unknown preset {time_range.preset}")
