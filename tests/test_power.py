"""Power assertions around a refresh cycle.

A refresh that starts just before the machine idles out can be suspended
mid-flight, leaving a half-signed IPA and a failed install. Holding a
PreventUserIdleSystemSleep assertion keeps the machine up for the duration.

This needs no entitlement and, unlike caffeinate -s, is valid on battery.
"""

from catapult import power


def test_keeps_machine_awake_for_the_duration():
    with power.prevent_idle_sleep("catapult test") as assertion:
        assert assertion.active
    assert not assertion.active


def test_releases_even_when_the_body_raises():
    try:
        with power.prevent_idle_sleep("catapult test") as assertion:
            raise RuntimeError("refresh blew up")
    except RuntimeError:
        pass
    assert not assertion.active


def test_degrades_to_a_noop_when_iokit_is_unavailable(monkeypatch):
    """A missing framework must not stop a refresh from running."""
    monkeypatch.setattr(power, "_iokit", lambda: None)

    with power.prevent_idle_sleep("catapult test") as assertion:
        assert not assertion.active

    assert not assertion.active


def test_nested_assertions_are_independent():
    with power.prevent_idle_sleep("outer") as outer:
        with power.prevent_idle_sleep("inner") as inner:
            assert outer.active and inner.active
        assert outer.active and not inner.active
    assert not outer.active


def test_wake_schedule_command_is_generated_not_executed():
    """Catapult must never run this itself: pmset schedule requires root."""
    command = power.wake_schedule_command(hour=3, minute=30)

    assert command.startswith("sudo pmset repeat wake")
    assert "03:30:00" in command
    assert "MTWRFSU" in command


def test_wake_schedule_command_rejects_a_nonsense_time():
    for bad in ((24, 0), (-1, 0), (0, 60), (0, -1)):
        try:
            power.wake_schedule_command(hour=bad[0], minute=bad[1])
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
