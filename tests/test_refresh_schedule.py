"""Refresh scheduling: wall-clock waiting, backoff, and real profile expiry.

The bug these guard against: run_refresh_loop slept on asyncio's clock, which is
time.monotonic() -> mach_absolute_time() on macOS and does not advance while the
system is asleep. On the author's Mac that lost 44.1 days out of 129 days of
uptime, so a laptop that sleeps nightly checked far less often than hourly.
"""

import pytest

from catapult import refresh


class FakeClock:
    """A wall clock the test can jump forward, the way system sleep does."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.slept: list[float] = []
        self.jump_on_sleep = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        # Real sleep advances the wall clock by at least the requested amount.
        self.now += seconds + self.jump_on_sleep
        self.jump_on_sleep = 0.0


async def test_sleep_until_returns_immediately_when_deadline_passed():
    clock = FakeClock()

    await refresh.sleep_until(clock.now - 1, now=clock.time, sleep=clock.sleep)

    assert clock.slept == []


async def test_sleep_until_waits_in_slices_not_one_long_sleep():
    """Long single sleeps are what let suspended time go uncounted."""
    clock = FakeClock()
    deadline = clock.now + 3600

    await refresh.sleep_until(deadline, now=clock.time, sleep=clock.sleep)

    assert max(clock.slept) <= refresh.SLEEP_SLICE_SECONDS
    assert clock.time() >= deadline


async def test_sleep_until_stops_early_when_system_sleep_skips_the_deadline():
    """The regression test for the real bug.

    The machine suspends mid-wait and wakes up hours past the deadline. The loop
    must notice on its next slice and run, rather than continuing to wait out a
    duration measured on a clock that was frozen.
    """
    clock = FakeClock()
    deadline = clock.now + 3600
    clock.jump_on_sleep = 8 * 3600  # 8 hours of system sleep

    await refresh.sleep_until(deadline, now=clock.time, sleep=clock.sleep)

    assert len(clock.slept) == 1
    assert clock.time() >= deadline


def test_retry_delay_grows_exponentially():
    delays = [refresh.retry_delay_seconds(n) for n in (1, 2, 3, 4)]

    assert delays == sorted(delays)
    assert delays[1] == delays[0] * 2
    assert delays[2] == delays[0] * 4


def test_retry_delay_is_capped():
    assert refresh.retry_delay_seconds(50) == refresh.RETRY_MAX_SECONDS


def test_retry_delay_is_zero_for_no_failures():
    assert refresh.retry_delay_seconds(0) == 0


def test_due_installs_includes_expiring_record():
    now = refresh._now_ts()
    state = {"installs": [{"device_udid": "A", "last_installed": now - 6.5 * 86400}]}

    assert len(refresh.due_installs(state, now=now)) == 1


def test_due_installs_skips_record_that_is_not_close_to_expiry():
    now = refresh._now_ts()
    state = {"installs": [{"device_udid": "A", "last_installed": now - 3600}]}

    assert refresh.due_installs(state, now=now) == []


def test_due_installs_respects_backoff_window():
    now = refresh._now_ts()
    state = {"installs": [{
        "device_udid": "A",
        "last_installed": now - 6.5 * 86400,
        "fail_count": 2,
        "next_attempt_at": now + 1800,
    }]}

    assert refresh.due_installs(state, now=now) == []


def test_due_installs_retries_once_backoff_elapses():
    now = refresh._now_ts()
    state = {"installs": [{
        "device_udid": "A",
        "last_installed": now - 6.5 * 86400,
        "fail_count": 2,
        "next_attempt_at": now - 1,
    }]}

    assert len(refresh.due_installs(state, now=now)) == 1


def test_repeated_failure_never_permanently_retires_a_record():
    """Previously MAX_CONSECUTIVE_FAILURES = 3 retired an app forever, so a
    weekend away from the device silently ended auto-refresh."""
    now = refresh._now_ts()
    state = {"installs": [{
        "device_udid": "A",
        "last_installed": now - 6.5 * 86400,
        "fail_count": 99,
        "next_attempt_at": now - 1,
    }]}

    assert len(refresh.due_installs(state, now=now)) == 1


def test_uses_profile_expiry_when_known():
    """A free-account profile's 7-day clock starts at issuance, not install, so
    last_installed + 7d is optimistic by however long signing and install took."""
    rec: dict = {}
    installed_at = 1_000_000.0
    profile_expiry = installed_at + 6 * 86400  # issued a day before we installed

    refresh._stamp_refresh_schedule(rec, installed_at, expires_at=profile_expiry)

    assert rec["expires_at"] == profile_expiry
    assert rec["refresh_after"] == profile_expiry - refresh.REFRESH_WINDOW_SECONDS


def test_falls_back_to_install_time_when_profile_expiry_unknown():
    rec: dict = {}
    installed_at = 1_000_000.0

    refresh._stamp_refresh_schedule(rec, installed_at)

    assert rec["expires_at"] == installed_at + refresh.REFRESH_VALID_SECONDS


def test_seconds_until_expiry_prefers_stored_expiry_over_recomputing():
    now = 1_000_000.0
    rec = {"last_installed": now, "expires_at": now + 3600}

    assert refresh.seconds_until_expiry(rec, now) == 3600
