"""
Anisette data provider for Apple GSA authentication.

Critical: The device identity (X-Mme-Device-Id, X-Apple-I-MD-LU) MUST be
consistent across the entire auth session — GSA SRP, 2FA trigger, and
developer services. Only the OTP values (X-Apple-I-MD, X-Apple-I-MD-M)
come from the OTP source; everything else uses stable per-Mac IDs.

Sources, in order:

1. AOSKit on this Mac. Free and personal, but macOS 26+ gates it behind
   private entitlements (``AOSKit WARN: A: Info request failed: -45070``),
   so on macOS 27 it never answers.
2. A local omnisette server on 127.0.0.1:6969, if someone runs one.
3. The SideStore anisette v3 protocol against a remote server
   (``ani.sidestore.zip`` by default): a one-time provisioning handshake in
   which THIS Mac talks to Apple's MidService and the server runs Apple's
   ADI library, after which the server signs OTPs for our own device
   identity. The identity is persisted in ``~/.catapult/anisette-v3.json``.

With v3 the device identity is derived from the provisioned identifier, so
``X-Mme-Device-Id`` / ``X-Apple-I-MD-LU`` come from the provider rather
than from the module-level defaults.
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import platform
import plistlib
import re
import threading
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


def device_identifier() -> str:
    """The persisted UUID this Mac presents to Apple.

    Also used as the ``machineId`` on certificate requests, so Apple's
    certificate list shows which Mac minted which certificate and recovery
    from a blocked CSR can be limited to this Mac's own stale entries.
    """
    return _DEVICE_ID


def _fetch_otp() -> dict:
    """Fetch the OTP values (X-Apple-I-MD, X-Apple-I-MD-M) from any source.

    A source may also return identity headers (``X-Mme-Device-Id``,
    ``X-Apple-I-MD-LU``, ``X-Apple-I-MD-RINFO``, ``X-MMe-Client-Info``); the
    v3 provider does, because its OTPs are only valid for the identity it was
    provisioned with.
    """
    native = _try_native_macos()
    if native:
        return native

    omnisette = _try_omnisette_server()
    if omnisette:
        return omnisette

    v3_error: Exception | None = None
    try:
        v3 = _try_anisette_v3()
        if v3:
            return v3
    except Exception as e:  # network, server, or Apple refusing to provision
        v3_error = e
        logger.warning("Anisette v3 (%s) failed: %s", ANISETTE_V3_SERVER, e)

    raise AnisetteError(
        "Could not obtain Anisette data. AOSKit no longer answers on macOS 26+, "
        f"no local omnisette server on 127.0.0.1:6969, and the anisette v3 "
        f"server {ANISETTE_V3_SERVER} "
        + (f"failed: {v3_error}" if v3_error else "returned nothing")
        + ". Set CATAPULT_ANISETTE_SERVER to another v3 server to change it."
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


# ── SideStore anisette v3 ──

ANISETTE_V3_SERVER = os.environ.get("CATAPULT_ANISETTE_SERVER", "https://ani.sidestore.zip").rstrip("/")
_V3_STATE_FILE = Path.home() / ".catapult" / "anisette-v3.json"
_GSA_LOOKUP_URL = "https://gsa.apple.com/grandslam/GsService2/lookup"

# What GSA is told about the client. Since September 2026 GsService2 answers
# 503 to any X-MMe-Client-Info that names com.apple.dt.Xcode (AltServer 1.7.6
# switched to akd for the same reason), so the Xcode identifier is replaced in
# whatever client info we present, our own default included.
DEFAULT_CLIENT_INFO = "<MacBookPro18,3> <Mac OS X;13.4.1;22F8> <com.apple.AOSKit/282 (com.apple.akd/1.0)>"
_XCODE_CLIENT_RE = re.compile(r"\(com\.apple\.dt\.Xcode/[^)]*\)")


def gsa_client_info(client_info: str | None = None) -> str:
    """The X-MMe-Client-Info to send to GSA and developer services."""
    return _XCODE_CLIENT_RE.sub("(com.apple.akd/1.0)", client_info or DEFAULT_CLIENT_INFO)


class AnisetteV3Client:
    """The SideStore anisette v3 protocol, synchronous.

    The identity (a 16-byte identifier and the ``adi_pb`` blob the server
    hands back after provisioning) lives in ``state_path``. The device id is
    the identifier as a UUID and the local user id is its SHA-256, exactly as
    SideStore derives them, so the OTPs the server signs match the headers we
    send. The identifier is seeded from this Mac's existing device id so
    ``X-Mme-Device-Id`` does not change when the OTP source does.
    """

    def __init__(self, server: str, state_path: Path, seed_device_id: str):
        self.server = server.rstrip("/")
        self.state_path = state_path
        self.identifier: bytes = b""
        self.adi_pb: bytes | None = None
        self.client_info: str = ""
        self.user_agent: str = ""
        self._lock = threading.Lock()
        self._load_state(seed_device_id)

    # ── identity ──

    @property
    def device_id(self) -> str:
        return str(uuid.UUID(bytes=self.identifier)).upper()

    @property
    def local_user_id(self) -> str:
        return hashlib.sha256(self.identifier).hexdigest()

    def _load_state(self, seed_device_id: str) -> None:
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
            self.identifier = base64.b64decode(data["identifier"])
            self.adi_pb = base64.b64decode(data["adi_pb"]) if data.get("adi_pb") else None
            self.client_info = data.get("client_info", "")
            self.user_agent = data.get("user_agent", "")
            if len(self.identifier) == 16 and data.get("server", self.server) == self.server:
                return
            logger.info("Anisette v3 state is for another server or malformed — starting over")
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Anisette v3 state unreadable — starting over", exc_info=True)
        try:
            self.identifier = uuid.UUID(seed_device_id).bytes
        except (ValueError, AttributeError, TypeError):
            self.identifier = os.urandom(16)
        self.adi_pb = None
        self.client_info = ""
        self.user_agent = ""

    def _save_state(self) -> None:
        payload = json.dumps({
            "server": self.server,
            "identifier": base64.b64encode(self.identifier).decode("ascii"),
            "adi_pb": base64.b64encode(self.adi_pb).decode("ascii") if self.adi_pb else None,
            "client_info": self.client_info,
            "user_agent": self.user_agent,
        })
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError:
            logger.warning("Could not persist anisette v3 state to %s", self.state_path, exc_info=True)

    # ── HTTP plumbing ──

    def _http(self):
        import httpx

        return httpx.Client(timeout=30, follow_redirects=True)

    def _ensure_client_info(self, http) -> None:
        if self.client_info and self.user_agent:
            return
        resp = http.get(f"{self.server}/v3/client_info")
        resp.raise_for_status()
        data = resp.json()
        self.client_info = data["client_info"]
        self.user_agent = data["user_agent"]

    def _apple_headers(self, client_info: str) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc)
        return {
            "X-Mme-Client-Info": client_info,
            "User-Agent": self.user_agent,
            "Content-Type": "text/x-xml-plist",
            "X-Apple-I-MD-LU": self.local_user_id,
            "X-Mme-Device-Id": self.device_id,
            "X-Apple-I-Client-Time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "X-Apple-I-TimeZone": "UTC",
            "X-Apple-Locale": "en_US",
        }

    @staticmethod
    def _plist_response(content: bytes) -> dict:
        data = plistlib.loads(content)
        response = data.get("Response")
        if not isinstance(response, dict):
            raise AnisetteError(f"Unexpected reply from Apple: {data!r}"[:300])
        return response

    # ── protocol ──

    def provision(self) -> None:
        """Run the one-time provisioning handshake and store ``adi_pb``.

        Apple is shown the server's own client info first, verbatim, because
        the ADI library on the server was set up for that machine description.
        If GSA answers 503 to it (the Xcode identifier block), the handshake is
        repeated with the akd-sanitised description.
        """
        import httpx

        with self._http() as http:
            self._ensure_client_info(http)
        candidates = [self.client_info]
        if gsa_client_info(self.client_info) != self.client_info:
            candidates.append(gsa_client_info(self.client_info))
        last: Exception | None = None
        for client_info in candidates:
            try:
                self._provision_as(client_info)
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 503 or client_info == candidates[-1]:
                    raise
                logger.info("GSA answered 503 to %r — retrying provisioning as akd", client_info)
                last = e
        raise AnisetteError(f"Anisette v3 provisioning failed: {last}")

    def _provision_as(self, client_info: str) -> None:
        from websockets.sync.client import connect as ws_connect

        with self._http() as http:
            lookup = http.get(_GSA_LOOKUP_URL, headers=self._apple_headers(client_info))
            lookup.raise_for_status()
            urls = plistlib.loads(lookup.content).get("urls", {})
            start_url = urls.get("midStartProvisioning")
            finish_url = urls.get("midFinishProvisioning")
            if not start_url or not finish_url:
                raise AnisetteError("GSA lookup did not return the MidService provisioning URLs")

            ws_url = re.sub(r"^http", "ws", f"{self.server}/v3/provisioning_session")
            logger.info("Provisioning anisette v3 identity with %s", self.server)
            with ws_connect(ws_url, open_timeout=30, close_timeout=10) as ws:
                for _ in range(8):  # the handshake is four messages; leave slack
                    msg = json.loads(ws.recv(timeout=60))
                    result = msg.get("result")
                    if result == "GiveIdentifier":
                        ws.send(json.dumps({"identifier": base64.b64encode(self.identifier).decode("ascii")}))
                    elif result == "GiveStartProvisioningData":
                        body = plistlib.dumps({"Header": {}, "Request": {}})
                        resp = http.post(start_url, content=body, headers=self._apple_headers(client_info))
                        resp.raise_for_status()
                        spim = self._plist_response(resp.content).get("spim")
                        if not spim:
                            raise AnisetteError("Apple did not return spim for the provisioning session")
                        ws.send(json.dumps({"spim": spim}))
                    elif result == "GiveEndProvisioningData":
                        body = plistlib.dumps({"Header": {}, "Request": {"cpim": msg.get("cpim", "")}})
                        resp = http.post(finish_url, content=body, headers=self._apple_headers(client_info))
                        resp.raise_for_status()
                        reply = self._plist_response(resp.content)
                        if not reply.get("ptm") or not reply.get("tk"):
                            raise AnisetteError("Apple did not return ptm/tk for the provisioning session")
                        ws.send(json.dumps({"ptm": reply["ptm"], "tk": reply["tk"]}))
                    elif result == "ProvisioningSuccess":
                        self.adi_pb = base64.b64decode(msg["adi_pb"])
                        self._save_state()
                        logger.info("Anisette v3 identity provisioned (device %s)", self.device_id)
                        return
                    else:
                        raise AnisetteError(
                            f"Anisette v3 server aborted provisioning: {msg.get('message') or result}"
                        )
        raise AnisetteError("Anisette v3 provisioning did not complete")

    def _get_headers_once(self, http) -> dict | None:
        """One /v3/get_headers call. None means the identity must be re-provisioned."""
        resp = http.post(
            f"{self.server}/v3/get_headers",
            json={
                "identifier": base64.b64encode(self.identifier).decode("ascii"),
                "adi_pb": base64.b64encode(self.adi_pb or b"").decode("ascii"),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "Headers":
            return data
        message = str(data.get("message", data))
        if "-45061" in message:
            return None
        raise AnisetteError(f"Anisette v3 server refused to sign: {message}"[:300])

    def headers(self) -> dict:
        """Fresh OTP plus the identity it is bound to."""
        with self._lock, self._http() as http:
            self._ensure_client_info(http)
            if self.adi_pb is None:
                self.provision()
            data = self._get_headers_once(http)
            if data is None:
                logger.info("Anisette v3 identity expired (-45061) — re-provisioning")
                self.adi_pb = None
                # The server may have been set up again with a different
                # machine description; provision against the current one.
                self.client_info = self.user_agent = ""
                self.provision()
                data = self._get_headers_once(http)
                if data is None:
                    raise AnisetteError("Anisette v3 server still rejects the identity after re-provisioning")
            return {
                "X-Apple-I-MD": data.get("X-Apple-I-MD", ""),
                "X-Apple-I-MD-M": data.get("X-Apple-I-MD-M", ""),
                "X-Apple-I-MD-RINFO": data.get("X-Apple-I-MD-RINFO", "17106176"),
                "X-Apple-I-MD-LU": self.local_user_id,
                "X-Mme-Device-Id": self.device_id,
                "X-MMe-Client-Info": gsa_client_info(self.client_info),
            }


_v3_client: AnisetteV3Client | None = None
_v3_client_lock = threading.Lock()


def _anisette_v3_client() -> AnisetteV3Client:
    global _v3_client
    with _v3_client_lock:
        if _v3_client is None:
            _v3_client = AnisetteV3Client(ANISETTE_V3_SERVER, _V3_STATE_FILE, _DEVICE_ID)
        return _v3_client


def _try_anisette_v3() -> dict | None:
    data = _anisette_v3_client().headers()
    if data.get("X-Apple-I-MD") and data.get("X-Apple-I-MD-M"):
        logger.info("Got Anisette from v3 server %s", ANISETTE_V3_SERVER)
        return data
    return None


def _build_common_headers(raw: dict | None = None) -> dict:
    """Build the consistent set of Anisette headers with fresh OTP."""
    raw = raw if raw is not None else _fetch_otp()
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        # OTP values — fresh from the source (short-lived, ~30s)
        "X-Apple-I-MD": raw.get("X-Apple-I-MD", ""),
        "X-Apple-I-MD-M": raw.get("X-Apple-I-MD-M", ""),
        # Stable device identity — MUST match across all requests. A v3
        # source supplies its own; AOSKit and omnisette use ours.
        "X-Apple-I-MD-RINFO": raw.get("X-Apple-I-MD-RINFO") or "17106176",
        "X-Apple-I-MD-LU": raw.get("X-Apple-I-MD-LU") or _LOCAL_USER_ID,
        "X-Mme-Device-Id": raw.get("X-Mme-Device-Id") or _DEVICE_ID,
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
    raw = _fetch_otp()
    headers = _build_common_headers(raw)
    # X-MMe-Client-Info (used by GSA, the 2FA trigger and dev services)
    headers["X-MMe-Client-Info"] = gsa_client_info(raw.get("X-MMe-Client-Info"))
    return headers


def current_client_info() -> str:
    """The X-MMe-Client-Info for the active OTP source, without fetching an OTP.

    GSA SRP requests build their headers before the cpd, so they ask here.
    """
    if _v3_client is not None and _v3_client.client_info:
        return gsa_client_info(_v3_client.client_info)
    return gsa_client_info()


class AnisetteError(RuntimeError):
    pass
