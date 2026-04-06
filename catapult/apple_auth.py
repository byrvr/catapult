"""Apple ID authentication via GSA (Grand Slam Authentication) using SRP-6a.

The web-based idmsa.apple.com flow is blocked for non-browser clients.
This module uses the native GSA protocol (gsa.apple.com/grandslam/GsService2)
which is the same auth path used by macOS, Xcode, and AltServer.
"""

import base64
import hashlib
import hmac as hmac_mod
import logging
import plistlib
from dataclasses import dataclass, field

import ssl

import httpx
import srp._pysrp as srp
import truststore
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from catapult.anisette import get_anisette_headers, get_anisette_http_headers, AnisetteError

logger = logging.getLogger(__name__)

# Configure SRP for Apple's variant
srp.rfc5054_enable()
srp.no_username_in_x()

GSA_ENDPOINT = "https://gsa.apple.com/grandslam/GsService2"
GSA_AUTH_ENDPOINT = "https://gsa.apple.com"

HEADERS = {
    "Content-Type": "text/x-xml-plist",
    "Accept": "*/*",
    "User-Agent": "akd/1.0 CFNetwork/1568.200.51 Darwin/24.1.0",
    "X-MMe-Client-Info": "<MacBookPro18,3> <Mac OS X;13.4.1;22F8> "
                         "<com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>",
}


@dataclass
class AuthSession:
    apple_id: str = ""
    adsid: str = ""
    idms_token: str = ""
    session_token: str = ""
    cookies: dict = field(default_factory=dict)
    authenticated: bool = False

    @property
    def identity_token(self) -> str:
        if self.adsid and self.idms_token:
            return base64.b64encode(f"{self.adsid}:{self.idms_token}".encode()).decode()
        return ""


def _derive_password(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    """Derive SRP password using Apple's s2k / s2k_fo scheme."""
    p = hashlib.sha256(password.encode("utf-8")).digest()
    if protocol == "s2k_fo":
        p = p.hex().encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", p, salt, iterations, dklen=32)


def _decrypt_spd(session_key: bytes, data: bytes) -> dict:
    """Decrypt the session data blob returned by GSA after SRP."""
    dk = hmac_mod.new(session_key, b"extra data key:", hashlib.sha256).digest()
    iv = hmac_mod.new(session_key, b"extra data iv:", hashlib.sha256).digest()[:16]

    logger.debug("spd decrypt: key=%d bytes, iv=%d bytes, data=%d bytes", len(dk), len(iv), len(data))

    cipher = Cipher(algorithms.AES(dk), modes.CBC(iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(data) + decryptor.finalize()

    # Remove PKCS7 padding — but if it fails, try without (Apple sometimes omits padding)
    try:
        unpadder = PKCS7(128).unpadder()
        decrypted = unpadder.update(decrypted) + unpadder.finalize()
    except ValueError:
        # Try stripping null bytes instead
        decrypted = decrypted.rstrip(b"\x00")

    logger.debug("spd decrypted: %d bytes, starts with: %s", len(decrypted), decrypted[:40])

    # Apple returns bare <dict>...</dict> without plist wrapper — add it if needed
    if decrypted.lstrip().startswith(b"<dict>"):
        decrypted = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<plist version="1.0">' + decrypted + b'</plist>'
        )

    return plistlib.loads(decrypted)


class AppleAuthClient:
    def __init__(self):
        self.session: AuthSession | None = None
        # Use macOS system trust store so Apple's CA is trusted
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._client = httpx.AsyncClient(timeout=30, verify=ctx)
        self._last_password: str = ""

    async def _gsa_request(self, request_body: dict) -> dict:
        body = plistlib.dumps({"Header": {"Version": "1.0.1"}, "Request": request_body})
        resp = await self._client.post(GSA_ENDPOINT, content=body, headers=HEADERS)
        logger.debug("GSA HTTP %d (%d bytes)", resp.status_code, len(resp.content))
        return plistlib.loads(resp.content)

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self._last_password = password
        return await self._gsa_authenticate(apple_id, password)

    async def _gsa_authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        try:
            cpd = get_anisette_headers()
        except AnisetteError as e:
            return {"status": "error", "message": str(e)}

        # ── Phase 1: SRP init ──
        logger.info("GSA init for %s", apple_id)

        # Create SRP user with empty password for init (we don't have derived pw yet)
        usr = srp.User(apple_id.encode(), b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        _, A = usr.start_authentication()

        init_resp = await self._gsa_request({
            "A2k": A,
            "cpd": cpd,
            "o": "init",
            "ps": ["s2k", "s2k_fo"],
            "u": apple_id,
        })

        status = init_resp.get("Status", {})
        if status.get("ec", 0) != 0:
            msg = status.get("em", f"GSA error {status.get('ec')}")
            logger.error("GSA init failed: %s", msg)
            return {"status": "error", "message": msg}

        resp_data = init_resp.get("Response", {})
        salt = resp_data.get("s")
        B = resp_data.get("B")
        iterations = resp_data.get("i", 0)
        cookie = resp_data.get("c", "")
        protocol = resp_data.get("sp", "s2k")

        if not salt or not B:
            return {"status": "error", "message": "Unexpected response from Apple (missing SRP params)"}

        logger.info("GSA init ok: protocol=%s iterations=%d", protocol, iterations)

        # ── Phase 2: SRP complete ──
        # Derive password using server-provided salt/iterations/protocol
        derived_pw = _derive_password(password, salt, iterations, protocol)

        # Create new SRP user with the real derived password, but we MUST
        # reuse the same private key `a` (and thus public key `A`) from
        # phase 1 — the server binds M1 verification to the A it received.
        saved_a = usr.a  # private key from phase 1
        usr = srp.User(apple_id.encode(), derived_pw, hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        usr.start_authentication()
        usr.a = saved_a
        usr.A = pow(usr.g, saved_a, usr.N)

        M = usr.process_challenge(salt, B)

        if M is None:
            return {"status": "error", "message": "SRP verification failed — incorrect password?"}

        # Fetch fresh Anisette for the complete request
        try:
            cpd2 = get_anisette_headers()
        except AnisetteError:
            cpd2 = cpd

        logger.info("GSA complete (sending proof)")
        complete_resp = await self._gsa_request({
            "M1": M,
            "c": cookie,
            "cpd": cpd2,
            "o": "complete",
            "u": apple_id,
        })

        comp_data = complete_resp.get("Response", {})
        # Status can be at top level, inside Header, or inside Response
        comp_status = (complete_resp.get("Status")
                       or complete_resp.get("Header", {}).get("Status")
                       or comp_data.get("Status")
                       or {})

        logger.info("GSA complete: response_keys=%s, status=%s", list(comp_data.keys()), comp_status)

        ec = comp_status.get("ec") if isinstance(comp_status, dict) else None
        if ec is not None and ec != 0:
            msg = comp_status.get("em", f"GSA error {ec}")
            if ec == 5000:
                msg = "Incorrect Apple ID or password"
            logger.error("GSA complete failed: %s (ec=%s)", msg, ec)
            return {"status": "error", "message": msg}

        # Verify server proof
        M2 = comp_data.get("M2")
        if M2:
            usr.verify_session(M2)
            if not usr.authenticated():
                logger.error("SRP server proof verification failed")
                return {"status": "error", "message": "Server verification failed"}
            logger.info("SRP server proof verified")
        else:
            logger.warning("No M2 in response — server proof not verified")

        # Get session key for decryption
        session_key = usr.get_session_key()

        # Decrypt session data
        spd = comp_data.get("spd")
        np = comp_data.get("np")
        logger.info("GSA complete: spd=%s, np=%s, M2=%s, token=%s",
                     f"{len(spd)}b" if spd else "missing",
                     f"{len(np)}b" if np else "missing",
                     f"{len(M2)}b" if M2 else "missing",
                     "present" if comp_data.get("tk") else "missing")
        if spd:
            try:
                decrypted = _decrypt_spd(session_key, spd)
                logger.info("spd keys: %s", list(decrypted.keys()))
                self.session.adsid = decrypted.get("adsid", "")
                self.session.idms_token = decrypted.get("GsIdmsToken", "")
                logger.info("adsid=%s, idms_token=%s",
                            self.session.adsid[:12] if self.session.adsid else "EMPTY",
                            f"{len(self.session.idms_token)} chars" if self.session.idms_token else "EMPTY")
            except Exception as e:
                logger.error("Failed to decrypt spd: %s", e)
                return {"status": "error", "message": f"Session decryption failed: {e}"}

        self.session.session_token = comp_data.get("tk", "")

        # Check for 2FA
        au = comp_status.get("au", "")
        if au in ("trustedDeviceSecondaryAuth", "secondaryAuth"):
            logger.info("2FA required (type: %s, adsid: %s)", au, self.session.adsid[:8] if self.session.adsid else "?")
            await self._trigger_2fa()
            return {"status": "2fa_required", "auth_type": au}

        self.session.authenticated = True
        logger.info("Auth succeeded (no 2FA), adsid=%s", self.session.adsid[:8] if self.session.adsid else "?")
        return {"status": "ok"}

    async def _trigger_2fa(self):
        """Request Apple to push 2FA code to trusted devices."""
        if not self.session or not self.session.identity_token:
            logger.warning("Cannot trigger 2FA — no identity token")
            return

        try:
            anisette = get_anisette_http_headers()
        except Exception:
            anisette = {}

        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "User-Agent": "akd/1.0 CFNetwork/1568.200.51 Darwin/24.1.0",
            "X-MMe-Client-Info": anisette.get("X-MMe-Client-Info",
                "<MacBookPro18,3> <Mac OS X;13.4.1;22F8> "
                "<com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>"),
            "X-Apple-Identity-Token": self.session.identity_token,
            **anisette,
        }

        url = f"{GSA_AUTH_ENDPOINT}/auth/verify/trusteddevice"
        try:
            resp = await self._client.get(url, headers=headers)
            logger.info("2FA trigger: HTTP %d, body=%s", resp.status_code, resp.text[:200] if resp.text else "empty")
        except Exception as e:
            logger.warning("2FA trigger failed: %s", e)

    async def submit_2fa(self, code: str) -> dict:
        if not self.session or not self.session.identity_token:
            return {"status": "error", "message": "No pending auth session"}

        headers = {
            **HEADERS,
            "X-Apple-Identity-Token": self.session.identity_token,
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "security-code": code,
        }

        logger.info("Submitting 2FA code via GSA validate")
        resp = await self._client.get(
            f"{GSA_AUTH_ENDPOINT}/grandslam/GsService2/validate",
            headers=headers,
        )
        logger.info("2FA validate: HTTP %d", resp.status_code)

        if resp.status_code != 200:
            return {"status": "error", "message": f"2FA validation failed (HTTP {resp.status_code})"}

        # Re-authenticate — should succeed without 2FA this time
        logger.info("Re-authenticating after 2FA")
        result = await self._gsa_authenticate(self.session.apple_id, self._last_password)

        if result.get("status") == "2fa_required":
            return {"status": "error", "message": "2FA still required — code may be incorrect"}

        return result
