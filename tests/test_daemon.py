"""Cron semantics, deterministic scheduling, and interruption of live imports."""

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from openrent import daemon
from openrent.client import FetchError
from openrent.db import Database
from openrent.models import Property


class Clock:
    def __init__(self, start="2026-10-01T00:00:00+00:00"):
        self.current = datetime.fromisoformat(start)
        self.waits = []

    def now(self):
        return self.current

    async def wait(self, seconds):
        self.waits.append(seconds)
        self.current += timedelta(seconds=seconds)


def arguments(**overrides):
    values = {
        "cron": "* * * * *",
        "timezone": "UTC",
        "run_now": False,
        "max_runs": 1,
        "check_schedule": False,
        "quiet": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def run(args, scan, clock, **options):
    return asyncio.run(
        daemon.run_daemon(
            args, scan, now=clock.now, wait=clock.wait, install_signals=False, **options
        )
    )


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * * *",
        "0 0 31 2 *",
    ],
)
def test_reject_invalid_or_nonstandard_schedule(expression):
    with pytest.raises(ValueError):
        daemon.validate_schedule(expression, "UTC")


@pytest.mark.parametrize("zone", ["Mars/Olympus"])
def test_reject_unknown_timezone(zone):
    with pytest.raises(ValueError, match="timezone"):
        daemon.validate_schedule("* * * * *", zone)


def test_standard_lists_names_ranges_steps_and_sunday():
    schedule = daemon.validate_schedule("*/15 9-17 * JAN,OCT MON-FRI", "UTC")
    base = datetime(2026, 10, 1, 9, 7, tzinfo=UTC)
    assert schedule.next_run(base) == datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    sunday_zero = daemon.validate_schedule("0 9 * * 0", "UTC")
    sunday_seven = daemon.validate_schedule("0 9 * * 7", "UTC")
    assert sunday_zero.next_run(base) == sunday_seven.next_run(base)
    assert sunday_zero.next_run(base) == datetime(2026, 10, 4, 9, tzinfo=UTC)


def test_monthday_and_weekday_match_either():
    schedule = daemon.validate_schedule("0 9 1 * MON", "UTC")
    first = schedule.next_run(datetime(2026, 9, 30, 12, tzinfo=UTC))
    second = schedule.next_run(first)
    assert first == datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert second == datetime(2026, 10, 5, 9, tzinfo=UTC)


def test_impossible_monthday_still_allows_weekday_under_or_rules():
    schedule = daemon.validate_schedule("0 9 31 2 MON", "UTC")
    assert schedule.next_run(datetime(2026, 10, 1, tzinfo=UTC)) == datetime(
        2027, 2, 1, 9, tzinfo=UTC
    )


def test_next_tick_is_strictly_after_start_and_clock_requires_timezone():
    schedule = daemon.validate_schedule("* * * * *", "UTC")
    start = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    assert schedule.next_run(start) == start + timedelta(minutes=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.next_run(start.replace(tzinfo=None))


def test_london_spring_transition_skips_missing_walltime():
    schedule = daemon.validate_schedule("30 1 * * *", "Europe/London")
    result = schedule.next_run(datetime(2026, 3, 28, 2, tzinfo=UTC))
    assert result.isoformat() == "2026-03-30T01:30:00+01:00"


def test_london_autumn_transition_runs_both_occurrences_of_repeated_walltime():
    schedule = daemon.validate_schedule("30 1 * * *", "Europe/London")
    first = schedule.next_run(datetime(2026, 10, 24, 12, tzinfo=UTC))
    second = schedule.next_run(first)
    third = schedule.next_run(second)
    assert first.isoformat() == "2026-10-25T01:30:00+01:00"
    assert second.isoformat() == "2026-10-25T01:30:00+00:00"
    assert second.timestamp() - first.timestamp() == 3600
    assert third.isoformat() == "2026-10-26T01:30:00+00:00"


def test_minute_cron_tracks_absolute_time_when_autumn_clock_goes_back():
    schedule = daemon.validate_schedule("* * * * *", "Europe/London")
    first = schedule.next_run(datetime.fromisoformat("2026-10-25T01:58:00+01:00"))
    second = schedule.next_run(first)
    assert first.isoformat() == "2026-10-25T01:59:00+01:00"
    assert second.isoformat() == "2026-10-25T01:00:00+00:00"
    assert second.timestamp() - first.timestamp() == 60


def test_run_now_counts_toward_maximum_and_does_not_wait():
    clock = Clock("2026-10-01T00:00:17+00:00")
    starts = []

    async def scan(args):
        starts.append(clock.now())
        return 0

    assert run(arguments(run_now=True), scan, clock) == 0
    assert starts == [datetime(2026, 10, 1, 0, 0, 17, tzinfo=UTC)]
    assert clock.waits == []


def test_long_scan_skips_missed_ticks_and_never_replays_jobs():
    clock = Clock()
    starts = []

    async def scan(args):
        starts.append(clock.now())
        clock.current += timedelta(seconds=185)
        return 0

    assert run(arguments(run_now=True, max_runs=2), scan, clock) == 0
    assert starts == [
        datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 0, 4, tzinfo=UTC),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        FetchError("server failed"),
        1,
    ],
)
def test_failure_is_logged_and_next_scheduled_scan_runs(failure, capsys):
    clock = Clock()
    starts = []

    async def scan(args):
        starts.append(clock.now())
        if len(starts) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return 0

    assert run(arguments(run_now=True, max_runs=2), scan, clock) == 1
    assert starts == [datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, 0, 1, tzinfo=UTC)]
    assert "waiting for the next tick" in capsys.readouterr().err


def test_invalid_schedule_or_maximum_never_calls_scan():
    clock = Clock()

    async def forbidden(args):
        pytest.fail("Invalid configuration must fail before scanning")

    with pytest.raises(ValueError):
        run(arguments(cron="@daily"), forbidden, clock)
    with pytest.raises(ValueError, match="max-runs"):
        run(arguments(max_runs=0), forbidden, clock)


@pytest.fixture
def fake_signals(monkeypatch):
    original = {signal.SIGINT: object(), signal.SIGTERM: object()}
    registered = original.copy()
    monkeypatch.setattr(daemon.signal, "getsignal", registered.__getitem__)
    monkeypatch.setattr(
        daemon.signal, "signal", lambda signum, handler: registered.update({signum: handler})
    )
    return original, registered


def test_sigterm_during_wait_stops_without_scanning_and_restores_handlers(fake_signals, capsys):
    original, handlers = fake_signals
    clock = Clock()

    async def wait(seconds):
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    async def forbidden(args):
        pytest.fail("Stopped daemon must not start a scan")

    assert asyncio.run(daemon.run_daemon(arguments(), forbidden, now=clock.now, wait=wait)) == 0
    assert handlers == original
    assert "SIGTERM" in capsys.readouterr().err


def test_sigint_unwinds_scan_preserves_committed_database_and_restores_handlers(
    fake_signals, tmp_path, capsys
):
    original, handlers = fake_signals
    clock = Clock()
    path = tmp_path / "archive.sqlite"
    finished = []

    async def scan(args):
        try:
            with Database(path) as db:
                db.upsert_property(Property(id=123, url="https://www.openrent.co.uk/123"))
                handlers[signal.SIGINT](signal.SIGINT, None)
                await asyncio.sleep(0)
                pytest.fail("SIGINT must interrupt an ongoing scan")
        finally:
            finished.append(True)

    assert (
        asyncio.run(
            daemon.run_daemon(
                arguments(run_now=True, max_runs=3), scan, now=clock.now, wait=clock.wait
            )
        )
        == 0
    )
    assert finished == [True]
    assert handlers == original
    with Database(path) as db:
        assert db.get_property(123)["id"] == 123
    assert "committed properties and images are saved" in capsys.readouterr().err


def test_default_wait_yields_to_event_loop_without_blocking_thread(monkeypatch):
    clock = Clock("2026-10-01T00:00:58+00:00")
    original_sleep = asyncio.sleep
    activity = []

    async def sleep(seconds):
        clock.current += timedelta(seconds=seconds)
        await original_sleep(0)

    def blocking_wait(*args):
        pytest.fail("The daemon must not use threading.Event.wait")

    async def scan(args):
        activity.append("scan")
        return 0

    async def observer():
        await original_sleep(0)
        activity.append("observer")

    async def scenario():
        watcher = asyncio.create_task(observer())
        result = await daemon.run_daemon(arguments(), scan, now=clock.now, install_signals=False)
        await watcher
        return result

    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    monkeypatch.setattr(daemon.threading.Event, "wait", blocking_wait)
    assert asyncio.run(scenario()) == 0
    assert activity == ["observer", "scan"]


def test_signal_awaits_scan_cleanup_before_returning_zero(fake_signals):
    original, handlers = fake_signals
    clock = Clock()
    cleaned = []

    async def scan(args):
        try:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.sleep(0)
            pytest.fail("Signal must cancel the running scan")
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    assert asyncio.run(daemon.run_daemon(arguments(run_now=True), scan, now=clock.now)) == 0
    assert cleaned == [True]
    assert handlers == original


def test_unrelated_cancellation_propagates_after_scan_cleanup(fake_signals):
    original, handlers = fake_signals
    cleaned = []

    async def scenario():
        started = asyncio.Event()

        async def scan(args):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(True)

        task = asyncio.create_task(daemon.run_daemon(arguments(run_now=True), scan))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cleaned == [True]
    assert handlers == original


def test_signal_after_scan_final_await_still_returns_zero(fake_signals):
    original, handlers = fake_signals

    async def scan(args):
        await asyncio.sleep(0)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    assert asyncio.run(daemon.run_daemon(arguments(run_now=True), scan)) == 0
    assert handlers == original


def test_signal_during_scan_failure_after_final_await_still_returns_zero(fake_signals):
    original, handlers = fake_signals

    async def scan(args):
        await asyncio.sleep(0)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        raise FetchError("Source closed during shutdown")

    assert asyncio.run(daemon.run_daemon(arguments(run_now=True), scan)) == 0
    assert handlers == original
