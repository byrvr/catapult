"""
Anisette data provider for Apple authentication.

On macOS, pulls machine-specific identifiers from AOSKit/AuthKit frameworks
via pyobjc. These headers are required by Apple's auth endpoints.
"""

import datetime
import locale
import logging
import uuid

logger = logging.getLogger(__name__)

# Fallback Anisette headers when native frameworks are unavailable.
# These are generic and may trigger additional verification from Apple.
_FALLBACK_MACHINE_ID = str(uuid.uuid4()).upper()
_FALLBACK_ONE_TIME_PASSWORD = str(uuid.uuid4()).upper()
_FALLBACK_LOCAL_USER_ID = str(uuid.uuid4()).upper()
_FALLBACK_ROUTING_INFO = "17106176"
_FALLBACK_DEVICE_ID = str(uuid.uuid4()).upper()
_FALLBACK_SERIAL = "C02X1234ABCD"


def _try_native() -> dict | None:
    """Attempt to pull Anisette data from macOS native frameworks."""
    try:
        import objc
        from Foundation import NSClassFromString

        # Try AOSKit first
        AOSUtilities = NSClassFromString("AOSUtilities")
        if AOSUtilities and AOSUtilities.respondsToSelector_("retrieveOTPHeadersForDSID:"):
            headers = AOSUtilities.retrieveOTPHeadersForDSID_("-2")
            if headers:
                return dict(headers)

        # Try AuthKit (newer macOS)
        AKDevice = NSClassFromString("AKDevice")
        if AKDevice and AKDevice.respondsToSelector_("currentDevice"):
            device = AKDevice.currentDevice()
            if device:
                mid = device.serverFriendlyDescription() if device.respondsToSelector_("serverFriendlyDescription") else None
                serial = device.serialNumber() if device.respondsToSelector_("serialNumber") else None
                unique = device.uniqueDeviceIdentifier() if device.respondsToSelector_("uniqueDeviceIdentifier") else None
                if mid:
                    return {
                        "X-Apple-I-MD-M": mid,
                        "X-Apple-I-SRL-NO": serial or _FALLBACK_SERIAL,
                        "X-Apple-I-MD-RINFO": _FALLBACK_ROUTING_INFO,
                        "X-Apple-I-Client-Time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "X-Apple-I-MD": unique or "",
                    }

    except Exception as e:
        logger.debug("Native Anisette unavailable: %s", e)

    return None


def get_anisette_headers() -> dict:
    """
    Return Anisette headers for Apple auth requests.

    Tries native macOS frameworks first, falls back to generated values.
    """
    native = _try_native()
    if native:
        logger.info("Using native Anisette data")
        return native

    logger.warning("Using fallback Anisette data — Apple may require additional verification")

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        current_locale = locale.getdefaultlocale()[0] or "en_US"
    except Exception:
        current_locale = "en_US"

    return {
        "X-Apple-I-MD-M": _FALLBACK_MACHINE_ID,
        "X-Apple-I-MD": _FALLBACK_ONE_TIME_PASSWORD,
        "X-Apple-I-MD-LU": _FALLBACK_LOCAL_USER_ID,
        "X-Apple-I-MD-RINFO": _FALLBACK_ROUTING_INFO,
        "X-Apple-I-SRL-NO": _FALLBACK_SERIAL,
        "X-Apple-I-Client-Time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "X-Apple-Locale": current_locale,
        "X-Apple-I-TimeZone": "UTC",
    }
