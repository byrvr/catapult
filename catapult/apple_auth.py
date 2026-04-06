"""Apple ID authentication via GSA (Grand Slam Authentication) using SRP-6a.

The web-based idmsa.apple.com flow is blocked for non-browser clients.
This module uses the native GSA protocol (gsa.apple.com/grandslam/GsService2)
which is the same auth path used by macOS, Xcode, and AltServer.
"""

import asyncio
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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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
    dsprsid: str = ""
    idms_token: str = ""
    gs_token: str = ""  # App-specific token from step 3 (apptokens)
    sk: bytes = b""  # Session key from spd (for token decryption)
    c: bytes = b""  # Cookie from spd (for apptokens request)
    session_token: str = ""
    cookies: dict = field(default_factory=dict)
    authenticated: bool = False

    @property
    def identity_token(self) -> str:
        """base64(adsid:GsIdmsToken) — for 2FA endpoints only."""
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

                raw_adsid = decrypted.get("adsid", "")
                raw_token = decrypted.get("GsIdmsToken", "")
                raw_dsprsid = decrypted.get("DsPrsId", "")

                # Ensure string types (plistlib may return bytes for <data> elements)
                self.session.adsid = raw_adsid.decode() if isinstance(raw_adsid, bytes) else str(raw_adsid)
                self.session.idms_token = raw_token.decode() if isinstance(raw_token, bytes) else str(raw_token)
                self.session.dsprsid = raw_dsprsid.decode() if isinstance(raw_dsprsid, bytes) else str(raw_dsprsid)

                # Save sk (session key) and c (cookie) for step 3 (apptokens)
                raw_sk = decrypted.get("sk", b"")
                raw_c = decrypted.get("c", b"")
                self.session.sk = raw_sk if isinstance(raw_sk, bytes) else raw_sk.encode()
                self.session.c = raw_c if isinstance(raw_c, bytes) else raw_c.encode()

                logger.info("adsid=%s, idms_token=%d chars, sk=%d bytes, c=%d bytes",
                            self.session.adsid[:16],
                            len(self.session.idms_token),
                            len(self.session.sk),
                            len(self.session.c))
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

        # ── Phase 3: Get app-specific token ──
        token_result = await self._get_app_token()
        if token_result.get("status") == "error":
            return token_result

        self.session.authenticated = True
        logger.info("Auth complete, adsid=%s, gs_token=%d chars",
                    self.session.adsid[:8] if self.session.adsid else "?",
                    len(self.session.gs_token))
        return {"status": "ok"}

    async def _get_app_token(self) -> dict:
        """Step 3: Exchange GsIdmsToken for an app-specific GS token."""
        if not self.session or not self.session.sk:
            return {"status": "error", "message": "Missing session key (sk) from auth"}

        app_id = "com.apple.gs.xcode.auth"

        # Compute HMAC checksum: HMAC-SHA256(sk, "apptokens" + adsid + app_id)
        h = hmac_mod.new(self.session.sk, digestmod=hashlib.sha256)
        h.update(b"apptokens")
        h.update(self.session.adsid.encode("utf-8"))
        h.update(app_id.encode("utf-8"))
        checksum = h.digest()

        try:
            cpd = get_anisette_headers()
        except AnisetteError as e:
            return {"status": "error", "message": str(e)}

        logger.info("Requesting app token (step 3)")
        resp = await self._gsa_request({
            "u": self.session.adsid,
            "app": [app_id],
            "c": self.session.c,
            "t": self.session.idms_token,
            "checksum": checksum,
            "cpd": cpd,
            "o": "apptokens",
        })

        resp_data = resp.get("Response", {})
        status = resp.get("Status") or resp_data.get("Status") or {}
        ec = status.get("ec") if isinstance(status, dict) else None
        if ec is not None and ec != 0:
            msg = status.get("em", f"apptokens error {ec}")
            logger.error("App token request failed: %s", msg)
            return {"status": "error", "message": msg}

        # Decrypt the encrypted token (et) using AES-GCM with sk
        et = resp_data.get("et", {}).get(app_id)
        if not et:
            # Try nested structure
            t_data = resp_data.get("t", {})
            if isinstance(t_data, dict):
                et = t_data.get(app_id, {}).get("token")
                if et and isinstance(et, str):
                    # Already decrypted string token
                    self.session.gs_token = et
                    logger.info("Got app token directly (no decryption needed)")
                    return {"status": "ok"}

            logger.error("No encrypted token in response. Keys: %s", list(resp_data.keys()))
            return {"status": "error", "message": "No app token in Apple response"}

        try:
            token_data = self._decrypt_gcm(self.session.sk, et)
            token_plist = plistlib.loads(token_data)
            app_entry = token_plist.get("t", {}).get(app_id, {})
            self.session.gs_token = app_entry.get("token", "")
            if not self.session.gs_token:
                logger.error("Decrypted token plist has no token: %s", list(token_plist.keys()))
                return {"status": "error", "message": "Empty token after decryption"}
            logger.info("Got app token (%d chars)", len(self.session.gs_token))
            return {"status": "ok"}
        except Exception as e:
            logger.error("Token decryption failed: %s", e)
            return {"status": "error", "message": f"Token decryption failed: {e}"}

    def _decrypt_gcm(self, sk: bytes, encrypted_data: bytes) -> bytes:
        """Decrypt AES-256-GCM encrypted app token. Format: XYZ + 16b IV + ciphertext + 16b tag."""
        if encrypted_data[:3] != b"XYZ":
            raise ValueError(f"Wrong token header: {encrypted_data[:3]!r}")
        nonce = encrypted_data[3:19]
        tag = encrypted_data[-16:]
        ciphertext = encrypted_data[19:-16]
        gcm = AESGCM(sk)
        return gcm.decrypt(nonce, ciphertext + tag, encrypted_data[:3])

    async def _trigger_2fa(self):
        """Request Apple to push 2FA code to trusted devices."""
        if not self.session or not self.session.identity_token:
            logger.warning("Cannot trigger 2FA — no identity token")
            return

        try:
            anisette = get_anisette_http_headers()
        except Exception:
            anisette = {}

        identity_token = self.session.identity_token
        logger.info("2FA identity token: %s...(%d chars)", identity_token[:30], len(identity_token))

        # Headers must match what every working implementation sends
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "Accept-Language": "en-us",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "11.2 (11B41)",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "X-Apple-Identity-Token": identity_token,
            **anisette,
        }

        url = f"{GSA_AUTH_ENDPOINT}/auth/verify/trusteddevice"
        try:
            # httpx client shares cookie jar from GSA session
            resp = await self._client.get(url, headers=headers)
            logger.info("2FA trigger: HTTP %d, body=%s", resp.status_code, resp.text[:200] if resp.text else "empty")
            if resp.status_code == 200:
                return
        except Exception as e:
            logger.warning("2FA trigger failed: %s", e)

        # If trusted device trigger failed, try requesting SMS
        try:
            sms_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Xcode",
                "X-Xcode-Version": "11.2 (11B41)",
                "X-Apple-App-Info": "com.apple.gs.xcode.auth",
                "X-Apple-Identity-Token": identity_token,
                **anisette,
            }
            sms_resp = await self._client.put(
                f"{GSA_AUTH_ENDPOINT}/auth/verify/phone/",
                json={"phoneNumber": {"id": 1}, "mode": "sms"},
                headers=sms_headers,
            )
            logger.info("2FA SMS: HTTP %d, body=%s", sms_resp.status_code, sms_resp.text[:200] if sms_resp.text else "empty")
        except Exception as e:
            logger.debug("SMS fallback failed: %s", e)

    async def submit_2fa(self, code: str) -> dict:
        if not self.session or not self.session.identity_token:
            return {"status": "error", "message": "No pending auth session"}

        try:
            anisette = get_anisette_http_headers()
        except Exception:
            anisette = {}

        # Must use same headers as 2FA trigger — consistent device identity
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "11.2 (11B41)",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "X-Apple-Identity-Token": self.session.identity_token,
            "security-code": code,
            **anisette,
        }

        logger.info("Submitting 2FA code via GSA validate")
        resp = await self._client.get(
            f"{GSA_AUTH_ENDPOINT}/grandslam/GsService2/validate",
            headers=headers,
        )
        logger.info("2FA validate: HTTP %d, body=%s", resp.status_code, resp.text[:200] if resp.text else "empty")

        if resp.status_code != 200:
            return {"status": "error", "message": f"2FA validation failed (HTTP {resp.status_code})"}

        # Re-authenticate — should succeed without 2FA this time
        logger.info("Re-authenticating after 2FA")
        result = await self._gsa_authenticate(self.session.apple_id, self._last_password)

        if result.get("status") == "2fa_required":
            return {"status": "error", "message": "2FA still required — code may be incorrect"}

        return result
