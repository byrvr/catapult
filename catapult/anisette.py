"""
Anisette data provider for Apple GSA authentication.

Critical: The device identity (X-Mme-Device-Id, X-Apple-I-MD-LU) MUST be
consistent across the entire auth session — GSA SRP, 2FA trigger, and
developer services. Only the OTP values (X-Apple-I-MD, X-Apple-I-MD-M)
come from omnisette; everything else uses our stable module-level IDs.
"""

import base64
import datetime
import json
import logging
import platform
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Stable device identity — persisted across restarts so Apple sees a consistent client.
# Mismatch between GSA cpd and 2FA trigger causes 401.
_IDENTITY_FILE = Path.home() / ".catapult" / "device_id.json"


def _load_or_create_identity() -> tuple[str, str]:
    """Load persisted device identity or create and save a new one."""
    try:
        if _IDENTITY_FILE.exists():
            data = json.loads(_IDENTITY_FILE.read_text())
            if data.get("device_id") and data.get("local_user_id"):
                return data["device_id"], data["local_user_id"]
    except Exception:
        pass

    device_id = str(uuid.uuid4()).upper()
    local_user_id = base64.b64encode(uuid.uuid4().bytes).decode()
    try:
        _IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _IDENTITY_FILE.write_text(json.dumps({
            "device_id": device_id,
            "local_user_id": local_user_id,
        }))
    except Exception:
        logger.debug("Could not persist device identity")
    return device_id, local_user_id


_DEVICE_ID, _LOCAL_USER_ID = _load_or_create_identity()


def _fetch_otp() -> dict:
    """Fetch just the OTP values (X-Apple-I-MD, X-Apple-I-MD-M) from any source."""
    native = _try_native_macos()
    if native:
        return native

    omnisette = _try_omnisette_server()
    if omnisette:
        return omnisette

    raise AnisetteError(
        "Could not obtain Anisette data. "
        "On macOS, ensure SIP allows loading private frameworks "
        "(csrutil status should show 'enabled' — full SIP is fine, "
        "only a custom policy blocking library validation causes issues)."
    )


def _try_native_macos() -> dict | None:
    if platform.system() != "Darwin":
        return None

    # Try pyobjc approach first
    result = _try_native_pyobjc()
    if result:
        return result

    # Fallback: ctypes-based approach (no pyobjc dependency)
    result = _try_native_ctypes()
    if result:
        return result

    return None


def _try_native_pyobjc() -> dict | None:
    """Get Anisette OTP via pyobjc bridge (AOSKit / AuthKit)."""
    try:
        import objc  # noqa: F401
        from Foundation import NSClassFromString, NSBundle

        aoskit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AOSKit.framework")
        if aoskit and aoskit.load():
            AOSUtilities = NSClassFromString("AOSUtilities")
            if AOSUtilities and AOSUtilities.respondsToSelector_("retrieveOTPHeadersForDSID:"):
                raw = AOSUtilities.retrieveOTPHeadersForDSID_("-2")
                if raw:
                    h = {str(k): str(v) for k, v in raw.items()}
                    result = {
                        "X-Apple-I-MD": h.get("X-Apple-I-MD") or h.get("X-Apple-MD", ""),
                        "X-Apple-I-MD-M": h.get("X-Apple-I-MD-M") or h.get("X-Apple-MD-M", ""),
                    }
                    if result["X-Apple-I-MD"]:
                        logger.info("Got Anisette from AOSKit (pyobjc)")
                        return result

        authkit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AuthKit.framework")
        if authkit and authkit.load():
            AKAppleIDSession = NSClassFromString("AKAppleIDSession")
            if AKAppleIDSession:
                session = AKAppleIDSession.alloc().initWithIdentifier_("com.apple.dt.Xcode")
                if session and session.respondsToSelector_("appleIDHeadersForRequest:"):
                    headers = session.appleIDHeadersForRequest_(None)
                    if headers and "X-Apple-I-MD" in headers:
                        logger.info("Got Anisette from AuthKit (pyobjc)")
                        return dict(headers)
    except Exception as e:
        logger.debug("pyobjc Anisette failed: %s", e)
    return None


def _try_native_ctypes() -> dict | None:
    """Get Anisette OTP via ctypes — works without pyobjc installed."""
    try:
        import ctypes
        import ctypes.util

        # Load ObjC runtime
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cf.CFDictionaryGetCount.restype = ctypes.c_long
        cf.CFDictionaryGetCount.argtypes = [ctypes.c_void_p]
        cf.CFDictionaryGetKeysAndValues.restype = None
        cf.CFDictionaryGetKeysAndValues.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)
        ]

        def msg(obj, sel_name, *args):
            sel = objc.sel_registerName(sel_name.encode())
            return objc.objc_msgSend(obj, sel, *args)

        def cfstr_to_py(cfstr) -> str:
            s = cf.CFStringGetCStringPtr(cfstr, 0x08000100)  # kCFStringEncodingUTF8
            if s:
                return s.decode("utf-8")
            # Fallback for non-ASCII
            buf = ctypes.create_string_buffer(1024)
            cf.CFStringGetCString(cfstr, buf, 1024, 0x08000100)
            return buf.value.decode("utf-8")

        def cfdict_to_py(cfdict) -> dict:
            count = cf.CFDictionaryGetCount(cfdict)
            if count <= 0:
                return {}
            keys = (ctypes.c_void_p * count)()
            vals = (ctypes.c_void_p * count)()
            cf.CFDictionaryGetKeysAndValues(cfdict, keys, vals)
            return {cfstr_to_py(keys[i]): cfstr_to_py(vals[i]) for i in range(count)}

        def cfstr(s: str):
            return cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

        # Load AOSKit
        try:
            ctypes.cdll.LoadLibrary(
                "/System/Library/PrivateFrameworks/AOSKit.framework/AOSKit"
            )
        except OSError:
            logger.debug("ctypes: Could not load AOSKit framework")
            return None

        AOSUtilities = objc.objc_getClass(b"AOSUtilities")
        if not AOSUtilities:
            logger.debug("ctypes: AOSUtilities class not found")
            return None

        dsid = cfstr("-2")
        sel = objc.sel_registerName(b"retrieveOTPHeadersForDSID:")
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        raw = objc.objc_msgSend(AOSUtilities, sel, dsid)
        if not raw:
            logger.debug("ctypes: retrieveOTPHeadersForDSID returned nil")
            return None

        h = cfdict_to_py(raw)
        result = {
            "X-Apple-I-MD": h.get("X-Apple-I-MD") or h.get("X-Apple-MD", ""),
            "X-Apple-I-MD-M": h.get("X-Apple-I-MD-M") or h.get("X-Apple-MD-M", ""),
        }
        if result["X-Apple-I-MD"]:
            logger.info("Got Anisette from AOSKit (ctypes)")
            return result

    except Exception as e:
        logger.debug("ctypes Anisette failed: %s", e)
    return None


def _try_omnisette_server() -> dict | None:
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:6969", timeout=5)
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
