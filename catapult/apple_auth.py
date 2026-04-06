"""Apple ID authentication with 2FA and Anisette support."""

import logging
from dataclasses import dataclass, field

import httpx

from catapult.anisette import get_anisette_headers

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"
AUTH_CONFIG_URL = "https://appstoreconnect.apple.com/olympus/v1/app/config?hostname=itunesconnect.apple.com"

# Apple OAuth client ID used by the developer portal / Xcode
OAUTH_CLIENT_ID = "XABBG36SBA"


@dataclass
class AuthSession:
    apple_id: str = ""
    session_token: str = ""
    scnt: str = ""
    session_id: str = ""
    auth_type: str = ""
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    authenticated: bool = False


class AppleAuthClient:
    def __init__(self):
        self.session: AuthSession | None = None
        self._widget_key: str | None = None
        self._client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
        )

    async def _get_widget_key(self) -> str:
        """Fetch Apple's auth service key (widget key) from the config endpoint."""
        if self._widget_key:
            return self._widget_key

        logger.info("Fetching auth service key from Apple")
        try:
            resp = await self._client.get(AUTH_CONFIG_URL, headers={
                "User-Agent": "Xcode",
            })
            if resp.status_code == 200:
                data = resp.json()
                self._widget_key = data.get("authServiceKey", "")
                if self._widget_key:
                    logger.info("Got widget key: %s...", self._widget_key[:12])
                    return self._widget_key
        except Exception as e:
            logger.warning("Failed to fetch widget key: %s", e)

        # Hardcoded fallback — this is the publicly-known Xcode auth key.
        # It rotates occasionally; the config endpoint above is the canonical source.
        self._widget_key = "e0b80c3bf78523bfe80e1b01571ca94d63c63c2dabc2cb4a7dbb0e17aa7f5e85"
        logger.info("Using fallback widget key")
        return self._widget_key

    async def _common_headers(self) -> dict:
        widget_key = await self._get_widget_key()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Xcode",
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Widget-Key": widget_key,
            "X-Apple-OAuth-Client-Id": OAUTH_CLIENT_ID,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": "https://developer.apple.com",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "Origin": "https://developer.apple.com",
            "Referer": "https://developer.apple.com/",
        }
        headers.update(get_anisette_headers())
        return headers

    async def _session_headers(self) -> dict:
        """Common headers plus session identifiers for follow-up requests."""
        headers = await self._common_headers()
        if self.session:
            if self.session.scnt:
                headers["scnt"] = self.session.scnt
            if self.session.session_id:
                headers["X-Apple-ID-Session-Id"] = self.session.session_id
        return headers

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        headers = await self._common_headers()
        body = {
            "accountName": apple_id,
            "password": password,
            "rememberMe": False,
        }

        logger.info("Authenticating %s with Apple ID", apple_id)
        resp = await self._client.post(f"{AUTH_ENDPOINT}/signin", json=body, headers=headers)

        if resp.status_code == 200:
            self._capture_session(resp)
            self.session.authenticated = True
            logger.info("Auth succeeded (no 2FA)")
            return {"status": "ok"}

        if resp.status_code == 409:
            self._capture_session(resp)
            self.session.auth_type = resp.headers.get("X-Apple-Auth-Type", "")
            logger.info("2FA required (type: %s)", self.session.auth_type)
            return {"status": "2fa_required", "auth_type": self.session.auth_type}

        if resp.status_code == 401:
            return {"status": "error", "message": "Incorrect Apple ID or password"}

        if resp.status_code == 403:
            return {"status": "error", "message": "Account locked or requires verification at appleid.apple.com"}

        msg = self._extract_error(resp)
        logger.error("Auth failed (%d): %s", resp.status_code, msg)
        return {"status": "error", "message": msg}

    async def submit_2fa(self, code: str) -> dict:
        if not self.session:
            return {"status": "error", "message": "No pending auth session"}

        headers = await self._session_headers()
        body = {"securityCode": {"code": code}}

        logger.info("Submitting 2FA code")
        resp = await self._client.post(
            f"{AUTH_ENDPOINT}/verify/trusteddevice/securitycode",
            json=body,
            headers=headers,
        )

        if resp.status_code not in (200, 204):
            msg = self._extract_error(resp)
            logger.error("2FA failed: %s", msg)
            return {"status": "error", "message": msg}

        # Trust this session so future requests don't need 2FA
        trust_headers = await self._session_headers()
        trust_resp = await self._client.get(f"{AUTH_ENDPOINT}/2sv/trust", headers=trust_headers)
        self._capture_session(trust_resp)
        self.session.authenticated = True
        logger.info("2FA verified, session trusted")
        return {"status": "ok"}

    def _capture_session(self, resp: httpx.Response):
        if "scnt" in resp.headers:
            self.session.scnt = resp.headers["scnt"]
        if "X-Apple-ID-Session-Id" in resp.headers:
            self.session.session_id = resp.headers["X-Apple-ID-Session-Id"]
        if "X-Apple-Session-Token" in resp.headers:
            self.session.session_token = resp.headers["X-Apple-Session-Token"]
        for cookie in resp.cookies.jar:
            self.session.cookies[cookie.name] = cookie.value

    def _extract_error(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
            errors = data.get("serviceErrors", [])
            if errors:
                return errors[0].get("message", resp.text[:200])
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
