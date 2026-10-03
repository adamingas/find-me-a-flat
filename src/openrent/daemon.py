"""Foreground cron scheduler for repeated, non-overlapping listing imports."""

import asyncio
import re
import signal
import sqlite3
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

from .client import FetchError

_PART = re.compile(r"(\*|[0-9]+|[A-Za-z]{3})(?:-([0-9]+|[A-Za-z]{3}))?(?:/([0-9]+))?")
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAYS = {
    name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}


def _field_value(value: str, position: int) -> int:
    if value.isdecimal():
        return int(value)
    names = _MONTHS if position == 3 else _DAYS if position == 4 else {}
    try:
        return names[value.lower()]
    except KeyError as exc:
        raise ValueError(f"Invalid cron field: {value!r}.") from exc


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    timezone: ZoneInfo
    expressions: tuple[str, ...]

    def next_run(self, after: datetime) -> datetime:
        """Return the next real instant whose local wall clock matches the cron.

        Nonexistent spring-forward wall times are skipped; repeated autumn wall
        times can each run. Compare timestamps, as Python's same-zone datetime
        comparisons do not distinguish the two folds of an ambiguous hour.
        """
        if after.tzinfo is None or after.utcoffset() is None:
            raise ValueError("The schedule clock must be timezone-aware.")
        candidates = []
        for expression in self.expressions:
            iterator = croniter(expression, after.astimezone(self.timezone), day_or=True)
            try:
                # croniter adjusts a missing wall time to the DST boundary. It
                # must still match the expression, otherwise it is not a tick.
                for _ in range(1024):
                    candidate = iterator.get_next(datetime)
                    local = candidate.astimezone(UTC).astimezone(self.timezone)
                    if local.timestamp() <= after.timestamp():
                        continue
                    if croniter.match(expression, local.replace(tzinfo=None), day_or=True):
                        candidates.append(local)
                        break
            except CroniterBadDateError:
                # A DOM branch such as February 31 can be impossible while a
                # restricted DOW branch still has matches under cron OR rules.
                continue
        if not candidates:
            raise ValueError("Cron schedule has no matching real date within 50 years.")
        return min(candidates, key=datetime.timestamp)


def validate_schedule(expression: str, timezone: str) -> CronSchedule:
    """Validate ordinary five-field cron syntax and an IANA timezone."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            "--cron must contain 5 fields: minute hour day-of-month month day-of-week."
        )
    for position, field in enumerate(fields):
        low, high = _BOUNDS[position]
        for part in field.split(","):
            match = _PART.fullmatch(part)
            if not match:
                raise ValueError(
                    "Use standard cron values, *, lists, ascending ranges, and positive steps; "
                    "macros, seconds, years, ?, L, W, #, H, and R are unsupported."
                )
            start, end, step = match.groups()
            if start == "*":
                if end is not None:
                    raise ValueError("A cron wildcard cannot start a range.")
            else:
                start_value = _field_value(start, position)
                end_value = _field_value(end, position) if end is not None else start_value
                if not low <= start_value <= end_value <= high:
                    raise ValueError(f"Cron field {position + 1} must be between {low} and {high}.")
            if step is not None and int(step) <= 0:
                raise ValueError("Cron steps must be positive.")
    normalized = " ".join(fields).lower()
    if not croniter.is_valid(normalized):
        raise ValueError("Invalid cron schedule.")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone!r}.") from exc

    if fields[2] != "*" and fields[4] != "*":
        by_monthday = fields.copy()
        by_monthday[4] = "*"
        by_weekday = fields.copy()
        by_weekday[2] = "*"
        expressions = (" ".join(by_monthday).lower(), " ".join(by_weekday).lower())
    else:
        expressions = (normalized,)
    schedule = CronSchedule(normalized, zone, expressions)
    try:
        schedule.next_run(datetime.now(UTC))
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        raise ValueError("Invalid cron schedule.") from exc
    return schedule


def _log(args: SimpleNamespace, message: str, *, error: bool = False) -> None:
    if error or not getattr(args, "quiet", False):
        print(message, file=sys.stderr, flush=True)


async def run_daemon(
    args: SimpleNamespace,
    scan: Callable[[SimpleNamespace], Awaitable[int]],
    *,
    now: Callable[[], datetime] | None = None,
    wait: Callable[[float], Awaitable[object]] | None = None,
    stop_event: threading.Event | None = None,
    install_signals: bool = True,
) -> int:
    """Run scans sequentially until stopped, skipping ticks elapsed in a scan.

    ``now`` and async ``wait`` are clock hooks for deterministic tests. The CLI must
    validate the fetch configuration and acquire its database lock before this
    loop; ``--check-schedule`` requires neither a database nor a network call.
    ``max_runs`` counts scan attempts, including failures and ``run_now``.
    """
    schedule = validate_schedule(args.cron, args.timezone)
    maximum = getattr(args, "max_runs", None)
    if maximum is not None and maximum <= 0:
        raise ValueError("--max-runs must be positive.")
    clock = now or (lambda: datetime.now(UTC))
    if getattr(args, "check_schedule", False):
        cursor = clock()
        for _ in range(5):
            cursor = schedule.next_run(cursor)
            print(cursor.isoformat(), flush=True)
        return 0

    stopped = stop_event if stop_event is not None else threading.Event()
    sleeper = wait or asyncio.sleep
    task = asyncio.current_task()
    previous_handlers = {}
    scanning = False
    stop_signal = None
    runs = failures = 0

    def stop(signum, _frame):
        nonlocal stop_signal
        already_stopped = stopped.is_set()
        stop_signal = signum
        stopped.set()
        if scanning and not already_stopped and task is not None:
            task.cancel()

    try:
        if install_signals and threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, stop)
        _log(args, f"Daemon schedule: {schedule.expression} ({args.timezone}); Ctrl-C to stop.")
        next_run = (
            clock().astimezone(schedule.timezone)
            if getattr(args, "run_now", False)
            else schedule.next_run(clock())
        )
        while not stopped.is_set():
            _log(args, f"Next scan: {next_run.isoformat()}")
            while not stopped.is_set():
                remaining = next_run.timestamp() - clock().timestamp()
                if remaining <= 0:
                    break
                await sleeper(min(remaining, 1.0))
            if stopped.is_set():
                break
            runs += 1
            _log(
                args, f"Starting scan {runs} at {clock().astimezone(schedule.timezone).isoformat()}"
            )
            try:
                scanning = True
                result = await scan(args)
                if result:
                    failures += 1
                    _log(
                        args,
                        f"Scan {runs} returned {result}; waiting for the next tick.",
                        error=True,
                    )
            except (FetchError, ValueError, sqlite3.Error, OSError) as exc:
                failures += 1
                _log(args, f"Scan {runs} failed: {exc}; waiting for the next tick.", error=True)
            finally:
                scanning = False
            if stopped.is_set():
                # A signal can arrive in synchronous cleanup after the
                # scan's final await. Deliver its pending cancellation
                # before returning a normal daemon status.
                await asyncio.sleep(0)
            if stopped.is_set() or (maximum is not None and runs >= maximum):
                break
            # Rebase on completion, rather than replaying ticks from an old
            # iterator. This also ensures there can never be overlapping scans.
            next_run = schedule.next_run(clock())
    except asyncio.CancelledError:
        # A signal requests a graceful daemon stop, after the awaited scan
        # has closed its HTTP client and database. Other cancellation belongs
        # to the caller and must retain its usual asyncio semantics.
        if not stopped.is_set():
            raise
    except KeyboardInterrupt:
        stopped.set()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if stopped.is_set():
        reason = signal.Signals(stop_signal).name if stop_signal is not None else "interruption"
        _log(
            args,
            f"Daemon stopped ({reason}); committed properties and images are saved.",
            error=True,
        )
        return 0
    _log(args, f"Daemon finished after {runs} scans ({failures} failed).")
    return 1 if failures else 0
