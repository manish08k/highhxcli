"""A small, dependency-free cron expression parser (5 fields)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTH_NAMES = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
DAY_NAMES = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


def _parse_field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            raise ValueError("empty list element")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"invalid step '{step_text}'")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names), _value(b, names)
        else:
            start = _value(part, names)
            end = high if step > 1 else start
        if start < low or end > high or start > end:
            raise ValueError(f"value out of range {low}-{high}: '{part}'")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _value(text: str, names: dict[str, int] | None) -> int:
    if names and text in names:
        return names[text]
    if not text.isdigit():
        raise ValueError(f"invalid value '{text}'")
    return int(text)


@dataclass(frozen=True)
class CronExpression:
    """Parsed cron expression: minute hour day-of-month month day-of-week."""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        text = MACROS.get(expression.strip().lower(), expression.strip())
        fields = text.split()
        if len(fields) != 5:
            raise ValueError(f"expected 5 fields (minute hour day month weekday), got {len(fields)}")
        minute, hour, day, month, weekday = fields
        weekdays = _parse_field(weekday, 0, 7, DAY_NAMES)
        if 7 in weekdays:
            weekdays = frozenset((weekdays - {7}) | {0})
        return cls(
            source=expression,
            minutes=_parse_field(minute, 0, 59),
            hours=_parse_field(hour, 0, 23),
            days=_parse_field(day, 1, 31),
            months=_parse_field(month, 1, 12, MONTH_NAMES),
            weekdays=weekdays,
            day_restricted=day != "*",
            weekday_restricted=weekday != "*",
        )

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours or moment.month not in self.months:
            return False
        cron_weekday = (moment.weekday() + 1) % 7  # Python: Monday=0 → cron: Sunday=0
        day_ok = moment.day in self.days
        weekday_ok = cron_weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, moment: datetime) -> datetime:
        """First matching minute strictly after ``moment``."""
        current = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = current + timedelta(days=366 * 5)
        while current < limit:
            if current.month not in self.months:
                year = current.year + (1 if current.month == 12 else 0)
                month = 1 if current.month == 12 else current.month + 1
                current = current.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(current):
                current = (current + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if current.hour not in self.hours:
                current = (current + timedelta(hours=1)).replace(minute=0)
                continue
            if current.minute not in self.minutes:
                current += timedelta(minutes=1)
                continue
            return current
        raise ValueError(f"cron expression '{self.source}' never matches")

    def _day_matches(self, moment: datetime) -> bool:
        cron_weekday = (moment.weekday() + 1) % 7
        day_ok = moment.day in self.days
        weekday_ok = cron_weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok
