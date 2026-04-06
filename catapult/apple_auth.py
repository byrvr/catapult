"""Apple ID authentication with 2FA support."""

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"
AUTH_INIT_URL = "https://idmsa.apple.com/appleauth/auth/authorize/signin"
AUTH_CONFIG_URL = "https://appstoreconnect.apple.com/olympus/v1/app/config?hostname=itunesconnect.apple.com"


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
        # Use a plain client (no http2) — Apple's idmsa CDN is picky
        self._client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
        )
        self._session_initialized = False

    async def _get_widget_key(self) -> str:
        if self._widget_key:
            return self._widget_key

        logger.info("Fetching auth service key from Apple")
        try:
            resp = await self._client.get(AUTH_CONFIG_URL, headers={"User-Agent": "Xcode"})
            if resp.status_code == 200:
                data = resp.json()
                key = data.get("authServiceKey", "")
                if key:
                    self._widget_key = key
                    logger.info("Got widget key: %s...", key[:12])
                    return key
        except Exception as e:
            logger.warning("Config fetch failed: %s", e)

        self._widget_key = "e0b80c3bf78523bfe80e1b01571ca94d63c63c2dabc2cb4a7dbb0e17aa7f5e85"
        logger.info("Using fallback widget key")
        return self._widget_key

    async def _init_session(self):
        """Hit the auth page to establish session cookies before signin."""
        if self._session_initialized:
            return

        widget_key = await self._get_widget_key()
        logger.info("Initializing auth session")

        # Visit the sign-in page to get JSESSIONID and other cookies
        try:
            resp = await self._client.get(
                AUTH_INIT_URL,
                params={
                    "appIdKey": widget_key,
                    "path": "/account/",
                    "directSignIn": "true",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                  "Version/17.0 Safari/605.1.15",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            # Capture any cookies Apple set
            for cookie in resp.cookies.jar:
                self._client.cookies.set(cookie.name, cookie.value, domain=cookie.domain)
            logger.info("Session init: HTTP %d, captured cookies", resp.status_code)
            self._session_initialized = True
        except Exception as e:
            logger.warning("Session init failed: %s (continuing anyway)", e)

    async def _auth_headers(self) -> dict:
        widget_key = await self._get_widget_key()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Safari/605.1.15",
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Widget-Key": widget_key,
            "X-Apple-OAuth-Client-Id": widget_key,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": "https://appstoreconnect.apple.com",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": '{"appId":"XABBG36SBA"}',
            "Origin": "https://idmsa.apple.com",
            "Referer": "https://idmsa.apple.com/",
        }

    async def _session_headers(self) -> dict:
        headers = await self._auth_headers()
        if self.session:
            if self.session.scnt:
                headers["scnt"] = self.session.scnt
            if self.session.session_id:
                headers["X-Apple-ID-Session-Id"] = self.session.session_id
        return headers

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        # Phase 1: establish session cookies
        await self._init_session()

        # Phase 2: actual sign-in
        headers = await self._auth_headers()
        body = {
            "accountName": apple_id,
            "password": password,
            "rememberMe": False,
        }

        logger.info("Authenticating %s", apple_id)
        resp = await self._client.post(f"{AUTH_ENDPOINT}/signin", json=body, headers=headers)
        logger.info("Signin response: HTTP %d", resp.status_code)

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
        # Also grab from the client's cookie jar
        for cookie in self._client.cookies.jar:
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
