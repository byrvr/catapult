"""Apple ID authentication with 2FA and Anisette support."""

import logging
from dataclasses import dataclass, field

import httpx

from catapult.anisette import get_anisette_headers

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"


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
        self._client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            http2=True,
        )

    def _common_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "User-Agent": "Xcode",
        }
        headers.update(get_anisette_headers())
        return headers

    def _session_headers(self) -> dict:
        """Common headers plus session identifiers for follow-up requests."""
        headers = self._common_headers()
        if self.session:
            if self.session.scnt:
                headers["scnt"] = self.session.scnt
            if self.session.session_id:
                headers["X-Apple-ID-Session-Id"] = self.session.session_id
        return headers

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        headers = self._common_headers()
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

        msg = self._extract_error(resp)
        logger.error("Auth failed: %s", msg)
        return {"status": "error", "message": msg}

    async def submit_2fa(self, code: str) -> dict:
        if not self.session:
            return {"status": "error", "message": "No pending auth session"}

        headers = self._session_headers()
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
        trust_headers = self._session_headers()
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
                return errors[0].get("message", resp.text)
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
