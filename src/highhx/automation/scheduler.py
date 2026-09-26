"""A foreground scheduler for cron-style schedules (no system cron required)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from highhx.automation.cron import CronExpression
from highhx.config.schema import ScheduleConfig
from highhx.execution.cancellation import CancellationToken
from highhx.storage.cache import StateStore
from highhx.utils.time import parse_iso, utc_now


@dataclass
class ScheduleInfo:
    name: str
    cron: str
    target: str
    last_run: str | None
    next_run: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ScheduleRunner:
    def __init__(
        self,
        schedules: list[ScheduleConfig],
        run: Callable[[ScheduleConfig], bool],
        state: StateStore | None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.schedules = schedules
        self.expressions = {s.name: CronExpression.parse(s.cron) for s in schedules}
        self.run_callback = run
        self.state = state
        self.clock = clock
        self._memory: dict[str, str] = {}

    def _key(self, name: str) -> str:
        return f"schedule.{name}.last_run"

    def last_run(self, name: str) -> str | None:
        if self.state is not None:
            value = self.state.get(self._key(name))
            return str(value) if value else None
        return self._memory.get(name)

    def _set_last_run(self, name: str, value: str) -> None:
        if self.state is not None:
            self.state.set(self._key(name), value)
        else:
            self._memory[name] = value

    def info(self) -> list[ScheduleInfo]:
        now = self.clock()
        rows = []
        for schedule in self.schedules:
            expr = self.expressions[schedule.name]
            rows.append(
                ScheduleInfo(
                    schedule.name,
                    schedule.cron,
                    f"workflow {schedule.workflow}" if schedule.workflow else str(schedule.run),
                    self.last_run(schedule.name),
                    expr.next_after(now).isoformat(timespec="minutes"),
                )
            )
        return rows

    def _now(self, now: datetime | None) -> datetime:
        moment = now or self.clock()
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    def due(self, now: datetime | None = None) -> list[ScheduleConfig]:
        """Schedules whose next run (after their last run, or after start) has arrived."""
        now = self._now(now)
        due = []
        for schedule in self.schedules:
            last = self.last_run(schedule.name)
            reference = parse_iso(last) if last else now.replace(second=0, microsecond=0)
            if last is None:
                # Never ran: due only if this exact minute matches.
                if self.expressions[schedule.name].matches(now):
                    due.append(schedule)
                continue
            if self.expressions[schedule.name].next_after(reference) <= now:
                due.append(schedule)
        return due

    def tick(self, now: datetime | None = None) -> list[tuple[str, bool]]:
        now = self._now(now)
        results = []
        for schedule in self.due(now):
            self._set_last_run(schedule.name, now.replace(second=0, microsecond=0).isoformat())
            results.append((schedule.name, self.run_callback(schedule)))
        return results

    def run_forever(self, cancel: CancellationToken, *, poll: float = 15.0) -> None:
        while not cancel.cancelled:
            self.tick()
            if cancel.wait(poll):
                break
