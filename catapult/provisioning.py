"""Provisioning profile parsing.

A `.mobileprovision` is a CMS signed blob wrapping an XML plist. Two callers
need that plist: the signer, for entitlements, and the refresh scheduler, for
the real `ExpirationDate`.

Reading the real expiry matters because a free-account profile's 7-day clock
starts when Apple issues it, not when we install it. `last_installed + 7 days`
is therefore optimistic by however long signing and installation took, plus any
delay between them.
"""

from __future__ import annotations

import datetime as _dt
import logging
import plistlib

logger = logging.getLogger(__name__)

_PLIST_START = "<?xml"
_PLIST_END = "</plist>"


def parse_profile_plist(profile_bytes: bytes) -> dict:
    """Extract the plist payload from a provisioning profile.

    Raises ValueError when the blob does not contain a parseable plist —
    signing must fail loudly rather than sign against a profile we misread.
    """
    raw = profile_bytes.decode("latin-1")
    start = raw.find(_PLIST_START)
    end = raw.find(_PLIST_END)
    if start < 0 or end < 0:
        raise ValueError("Could not parse provisioning profile")
    end += len(_PLIST_END)

    try:
        return plistlib.loads(raw[start:end].encode("latin-1"))
    except Exception as e:
        raise ValueError(f"Could not parse provisioning profile: {e}") from e


def profile_expiration_ts(profile_bytes: bytes) -> float | None:
    """Return the profile's ExpirationDate as a UTC timestamp, or None.

    Deliberately lenient: a profile we cannot read must not take down the
    refresh loop, which falls back to install time + 7 days.
    """
    try:
        plist = parse_profile_plist(profile_bytes)
    except ValueError:
        logger.debug("Could not read provisioning profile expiry", exc_info=True)
        return None

    expires = plist.get("ExpirationDate")
    if not isinstance(expires, _dt.datetime):
        return None
    if expires.tzinfo is None:
        # plistlib returns naive datetimes; Apple encodes them in UTC.
        expires = expires.replace(tzinfo=_dt.timezone.utc)
    return expires.timestamp()
