"""
Anisette data provider for Apple GSA authentication.

Anisette headers (X-Apple-I-MD, X-Apple-I-MD-M) are machine-specific OTP
values required by Apple's Grand Slam Authentication. On macOS, we pull
these from the native AOSKit/AuthKit frameworks. If that fails, we try a
local omnisette-server instance.
"""

import base64
import datetime
import logging
import platform
import uuid

logger = logging.getLogger(__name__)

_DEVICE_ID = str(uuid.uuid4()).upper()
_LOCAL_USER_ID = base64.b64encode(uuid.uuid4().bytes).decode()


def _try_native_macos() -> dict | None:
    """Pull Anisette OTP data from macOS native frameworks."""
    if platform.system() != "Darwin":
        return None

    try:
        import objc
        from Foundation import NSClassFromString, NSBundle

        # Try loading AOSKit
        aoskit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AOSKit.framework")
        if aoskit and aoskit.load():
            AOSUtilities = NSClassFromString("AOSUtilities")
            if AOSUtilities and AOSUtilities.respondsToSelector_("retrieveOTPHeadersForDSID:"):
                headers = AOSUtilities.retrieveOTPHeadersForDSID_("-2")
                if headers and "X-Apple-I-MD" in headers and "X-Apple-I-MD-M" in headers:
                    logger.info("Got Anisette from AOSKit")
                    return dict(headers)

        # Try AuthKit
        authkit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AuthKit.framework")
        if authkit and authkit.load():
            AKAppleIDSession = NSClassFromString("AKAppleIDSession")
            if AKAppleIDSession:
                session = AKAppleIDSession.alloc().initWithIdentifier_("com.apple.dt.Xcode")
                if session and session.respondsToSelector_("appleIDHeadersForRequest:"):
                    headers = session.appleIDHeadersForRequest_(None)
                    if headers and "X-Apple-I-MD" in headers:
                        logger.info("Got Anisette from AuthKit")
                        return dict(headers)

    except Exception as e:
        logger.debug("Native Anisette failed: %s", e)

    return None


def _try_omnisette_server() -> dict | None:
    """Try fetching from a local omnisette-server instance."""
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:6969", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if "X-Apple-I-MD" in data and "X-Apple-I-MD-M" in data:
                logger.info("Got Anisette from omnisette-server")
                return data
    except Exception:
        pass
    return None


def get_anisette_headers() -> dict:
    """
    Return Anisette headers for Apple GSA auth.

    Tries native macOS → omnisette-server → raises error.
    """
    native = _try_native_macos()
    if native:
        return _build_cpd(native)

    omnisette = _try_omnisette_server()
    if omnisette:
        return _build_cpd(omnisette)

    raise AnisetteError(
        "Could not obtain Anisette data. Options:\n"
        "  1. Run omnisette-server: docker run -d -p 6969:80 ghcr.io/sidestore/omnisette-server:latest\n"
        "  2. On macOS, ensure SIP allows access to private frameworks"
    )


class AnisetteError(RuntimeError):
    pass


def _build_cpd(headers: dict) -> dict:
    """Build the full `cpd` dictionary for GSA requests."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "X-Apple-I-MD": headers.get("X-Apple-I-MD", ""),
        "X-Apple-I-MD-M": headers.get("X-Apple-I-MD-M", ""),
        "X-Apple-I-MD-RINFO": headers.get("X-Apple-I-MD-RINFO", "17106176"),
        "X-Apple-I-MD-LU": _LOCAL_USER_ID,
        "X-Mme-Device-Id": _DEVICE_ID,
        "X-Apple-I-SRL-NO": "0",
        "X-Apple-I-Client-Time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "X-Apple-I-TimeZone": "UTC",
        "X-Apple-Locale": "en_US",
        "loc": "en_US",
        "bootstrap": True,
        "icscrec": True,
        "pbe": False,
        "prkgen": True,
        "svct": "iCloud",
    }
