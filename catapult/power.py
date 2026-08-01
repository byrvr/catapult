"""macOS power assertions for the refresh loop.

Two separate problems:

1. A refresh that starts just before the machine idles out can be suspended
   mid-signing. `prevent_idle_sleep()` holds a PreventUserIdleSystemSleep
   assertion for the duration. It needs no entitlement and, unlike
   `caffeinate -s`, is honoured on battery.

2. Getting the machine to wake up in the first place. `pmset schedule` requires
   root, so Catapult generates the command and the user runs it once — we do
   not silently escalate to install a wake schedule.

   Note what a wake schedule can and cannot do. From xnu's IOPMrootDomain,
   `shouldSleepOnRTCAlarmWake()` returns `!acAdaptorConnected &&
   !clamshellSleepDisableMask`, and the clamshell path re-sleeps unconditionally
   on RTC wake. The override needs an Apple-internal entitlement. So the real
   boundary is *plugged in*, not *lid open*: desktops always work, a MacBook on
   AC works lid-open or lid-closed, battery + lid-open works, and battery +
   lid-closed is dead at the kernel level.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_ASSERTION_TYPE = "PreventUserIdleSystemSleep"
_ASSERTION_LEVEL_ON = 255
_CF_STRING_ENCODING_UTF8 = 0x08000100

_iokit_handle = None
_corefoundation_handle = None


def _iokit():
    """Load IOKit lazily so import never fails off-macOS."""
    global _iokit_handle
    if _iokit_handle is None:
        path = ctypes.util.find_library("IOKit")
        if not path:
            return None
        try:
            handle = ctypes.CDLL(path)
            handle.IOPMAssertionCreateWithName.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            handle.IOPMAssertionCreateWithName.restype = ctypes.c_int
            handle.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
            handle.IOPMAssertionRelease.restype = ctypes.c_int
            _iokit_handle = handle
        except Exception:
            logger.debug("IOKit unavailable", exc_info=True)
            return None
    return _iokit_handle


def _corefoundation():
    global _corefoundation_handle
    if _corefoundation_handle is None:
        path = ctypes.util.find_library("CoreFoundation")
        if not path:
            return None
        try:
            handle = ctypes.CDLL(path)
            handle.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
            ]
            handle.CFStringCreateWithCString.restype = ctypes.c_void_p
            handle.CFRelease.argtypes = [ctypes.c_void_p]
            handle.CFRelease.restype = None
            _corefoundation_handle = handle
        except Exception:
            logger.debug("CoreFoundation unavailable", exc_info=True)
            return None
    return _corefoundation_handle


class _Assertion:
    """Handle for one held power assertion."""

    def __init__(self) -> None:
        self._id: int | None = None

    @property
    def active(self) -> bool:
        return self._id is not None

    def _acquire(self, reason: str) -> None:
        iokit = _iokit()
        cf = _corefoundation()
        if iokit is None or cf is None:
            return

        type_ref = cf.CFStringCreateWithCString(
            None, _ASSERTION_TYPE.encode("utf-8"), _CF_STRING_ENCODING_UTF8
        )
        name_ref = cf.CFStringCreateWithCString(
            None, reason.encode("utf-8"), _CF_STRING_ENCODING_UTF8
        )
        try:
            assertion_id = ctypes.c_uint32(0)
            result = iokit.IOPMAssertionCreateWithName(
                type_ref, _ASSERTION_LEVEL_ON, name_ref, ctypes.byref(assertion_id)
            )
            if result == 0:
                self._id = assertion_id.value
            else:
                logger.debug("IOPMAssertionCreateWithName failed: %s", result)
        finally:
            for ref in (type_ref, name_ref):
                if ref:
                    cf.CFRelease(ref)

    def release(self) -> None:
        if self._id is None:
            return
        iokit = _iokit()
        if iokit is not None:
            iokit.IOPMAssertionRelease(ctypes.c_uint32(self._id))
        self._id = None


@contextmanager
def prevent_idle_sleep(reason: str = "Catapult is refreshing an app"):
    """Hold off idle sleep for the duration of the block.

    Degrades to a no-op when IOKit is unavailable — a missing framework must
    never stop a refresh from running.
    """
    assertion = _Assertion()
    try:
        assertion._acquire(reason)
    except Exception:
        logger.debug("Could not take power assertion", exc_info=True)
    try:
        yield assertion
    finally:
        try:
            assertion.release()
        except Exception:
            logger.debug("Could not release power assertion", exc_info=True)


def wake_schedule_command(hour: int = 3, minute: int = 0) -> str:
    """Return the command the user runs once to schedule a daily wake.

    Deliberately returned rather than executed: `pmset schedule` requires root
    ("This operation must be run as root"), and Catapult's backend is a
    gui/<uid> LaunchAgent. Escalating silently to change a machine's power
    schedule is not something the app should do on the user's behalf.
    """
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid wake time {hour:02d}:{minute:02d}")
    return f'sudo pmset repeat wake MTWRFSU {hour:02d}:{minute:02d}:00'
