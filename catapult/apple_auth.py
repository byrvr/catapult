import plistlib
from dataclasses import dataclass, field

import httpx


AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"
GSA_ENDPOINT = "https://gsa.apple.com/grandslam/GsService2"

CLIENT_INFO = (
    "<iMac20,2> <Mac OS X;13.0;22A380> <com.apple.AuthKit/1 (com.apple.dt.Xcode/3594.4.19)>"
)
AK_CLIENT_INFO_KEY = "X-Apple-I-Client-Time"


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
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    def _common_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "User-Agent": "Xcode",
        }

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        headers = self._common_headers()
        body = {
            "accountName": apple_id,
            "password": password,
            "rememberMe": False,
        }

        resp = await self._client.post(
            f"{AUTH_ENDPOINT}/signin",
            json=body,
            headers=headers,
        )

        if resp.status_code == 200:
            self._capture_session(resp)
            self.session.authenticated = True
            return {"status": "ok"}

        if resp.status_code == 409:
            self._capture_session(resp)
            self.session.auth_type = resp.headers.get("X-Apple-Auth-Type", "")
            return {"status": "2fa_required", "auth_type": self.session.auth_type}

        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return {"status": "error", "message": data.get("serviceErrors", [{}])[0].get("message", resp.text)}

    async def submit_2fa(self, code: str) -> dict:
        if not self.session:
            return {"status": "error", "message": "No pending auth session"}

        headers = self._common_headers()
        headers["scnt"] = self.session.scnt
        headers["X-Apple-ID-Session-Id"] = self.session.session_id

        body = {"securityCode": {"code": code}}

        resp = await self._client.post(
            f"{AUTH_ENDPOINT}/verify/trusteddevice/securitycode",
            json=body,
            headers=headers,
        )

        if resp.status_code in (200, 204):
            trust_resp = await self._client.get(
                f"{AUTH_ENDPOINT}/2sv/trust",
                headers=headers,
            )
            self._capture_session(trust_resp)
            self.session.authenticated = True
            return {"status": "ok"}

        return {"status": "error", "message": f"2FA verification failed ({resp.status_code})"}

    def _capture_session(self, resp: httpx.Response):
        if "scnt" in resp.headers:
            self.session.scnt = resp.headers["scnt"]
        if "X-Apple-ID-Session-Id" in resp.headers:
            self.session.session_id = resp.headers["X-Apple-ID-Session-Id"]
        if "X-Apple-Session-Token" in resp.headers:
            self.session.session_token = resp.headers["X-Apple-Session-Token"]
        for cookie in resp.cookies.jar:
            self.session.cookies[cookie.name] = cookie.value
