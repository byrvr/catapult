"""
Anisette data provider for Apple GSA authentication.

Critical: The device identity (X-Mme-Device-Id, X-Apple-I-MD-LU) MUST be
consistent across the entire auth session — GSA SRP, 2FA trigger, and
developer services. Only the OTP values (X-Apple-I-MD, X-Apple-I-MD-M)
come from omnisette; everything else uses our stable module-level IDs.
"""

import base64
import datetime
import logging
import platform
import uuid

logger = logging.getLogger(__name__)

# Stable device identity — created once, reused across ALL requests in this session.
# Mismatch between GSA cpd and 2FA trigger causes 401.
_DEVICE_ID = str(uuid.uuid4()).upper()
_LOCAL_USER_ID = base64.b64encode(uuid.uuid4().bytes).decode()


def _fetch_otp() -> dict:
    """Fetch just the OTP values (X-Apple-I-MD, X-Apple-I-MD-M) from any source."""
    native = _try_native_macos()
    if native:
        return native

    omnisette = _try_omnisette_server()
    if omnisette:
        return omnisette

    raise AnisetteError(
        "Could not obtain Anisette data. Options:\n"
        "  1. Run omnisette-server: docker run -d -p 6969:80 ghcr.io/sidestore/omnisette-server:latest\n"
        "  2. On macOS, ensure SIP allows access to private frameworks"
    )


def _try_native_macos() -> dict | None:
    if platform.system() != "Darwin":
        return None
    try:
        import objc
        from Foundation import NSClassFromString, NSBundle

        aoskit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AOSKit.framework")
        if aoskit and aoskit.load():
            AOSUtilities = NSClassFromString("AOSUtilities")
            if AOSUtilities and AOSUtilities.respondsToSelector_("retrieveOTPHeadersForDSID:"):
                raw = AOSUtilities.retrieveOTPHeadersForDSID_("-2")
                if raw:
                    # macOS returns X-Apple-MD / X-Apple-MD-M (no "I-" prefix)
                    h = {str(k): str(v) for k, v in raw.items()}
                    result = {
                        "X-Apple-I-MD": h.get("X-Apple-I-MD") or h.get("X-Apple-MD", ""),
                        "X-Apple-I-MD-M": h.get("X-Apple-I-MD-M") or h.get("X-Apple-MD-M", ""),
                    }
                    if result["X-Apple-I-MD"]:
                        logger.info("Got Anisette from AOSKit (native)")
                        return result

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


def _build_common_headers() -> dict:
    """Build the consistent set of Anisette headers with fresh OTP."""
    raw = _fetch_otp()
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        # OTP values — fresh from omnisette/native (short-lived, ~30s)
        "X-Apple-I-MD": raw.get("X-Apple-I-MD", ""),
        "X-Apple-I-MD-M": raw.get("X-Apple-I-MD-M", ""),
        # Stable device identity — MUST match across all requests
        "X-Apple-I-MD-RINFO": "17106176",
        "X-Apple-I-MD-LU": _LOCAL_USER_ID,
        "X-Mme-Device-Id": _DEVICE_ID,
        "X-Apple-I-SRL-NO": "0",
        # Timestamps
        "X-Apple-I-Client-Time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "X-Apple-I-TimeZone": "UTC",
        # Locale
        "X-Apple-Locale": "en_US",
        "loc": "en_US",
    }


def get_anisette_headers() -> dict:
    """Return Anisette cpd dict for GSA SRP requests."""
    headers = _build_common_headers()
    # GSA cpd needs extra flags
    headers.update({
        "bootstrap": True,
        "icscrec": True,
        "pbe": False,
        "prkgen": True,
        "svct": "iCloud",
    })
    return headers


def get_anisette_http_headers() -> dict:
    """Return Anisette as HTTP headers for 2FA trigger / developer services.

    Uses the SAME device identity as get_anisette_headers() to ensure
    Apple sees a consistent client across the auth session.
    """
    headers = _build_common_headers()
    # Add X-MMe-Client-Info (used by 2FA trigger and dev services)
    headers["X-MMe-Client-Info"] = (
        "<MacBookPro18,3> <Mac OS X;13.4.1;22F8> "
        "<com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>"
    )
    return headers


class AnisetteError(RuntimeError):
    pass
