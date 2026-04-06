"""Apple ID authentication with 2FA support.

Uses curl for HTTP requests to bypass TLS fingerprinting — Apple's CDN
rejects Python's TLS stack (httpx/requests) with 503. macOS curl uses
SecureTransport which Apple recognizes as legitimate.
"""

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://idmsa.apple.com/appleauth/auth"
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
        self._cookie_jar = Path(tempfile.mkdtemp(prefix="catapult_")) / "cookies.txt"

    async def _curl(self, method: str, url: str, headers: dict | None = None,
                    json_body: dict | None = None, include_headers: bool = True) -> tuple[int, dict, str]:
        """Run a curl request, return (status_code, response_headers, body)."""
        cmd = [
            "curl", "-s", "-S",
            "-X", method,
            "-b", str(self._cookie_jar),
            "-c", str(self._cookie_jar),
            "-L",  # follow redirects
            "-w", "\n%{http_code}",  # append status code
        ]

        if include_headers:
            cmd += ["-D", "-"]  # dump response headers to stdout

        if headers:
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]

        if json_body is not None:
            cmd += ["-d", json.dumps(json_body)]

        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if stderr:
            logger.debug("curl stderr: %s", stderr.decode().strip())

        raw = stdout.decode("utf-8", errors="replace")

        # Parse: headers are separated from body by \r\n\r\n, status code is last line
        lines = raw.rstrip().rsplit("\n", 1)
        status_code = int(lines[-1]) if lines[-1].isdigit() else 0
        content = lines[0] if len(lines) > 1 else ""

        resp_headers = {}
        body = content
        if include_headers and "\r\n\r\n" in content:
            # May have multiple header blocks (redirects)
            parts = content.split("\r\n\r\n")
            body = parts[-1]
            # Parse last header block before body
            if len(parts) >= 2:
                for line in parts[-2].splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        resp_headers[k.strip()] = v.strip()

        return status_code, resp_headers, body

    async def _get_widget_key(self) -> str:
        if self._widget_key:
            return self._widget_key

        logger.info("Fetching auth service key from Apple")
        status, _, body = await self._curl("GET", AUTH_CONFIG_URL, include_headers=False)
        if status == 200:
            try:
                data = json.loads(body)
                key = data.get("authServiceKey", "")
                if key:
                    self._widget_key = key
                    logger.info("Got widget key: %s...", key[:12])
                    return key
            except json.JSONDecodeError:
                pass

        self._widget_key = "e0b80c3bf78523bfe80e1b01571ca94d63c63c2dabc2cb4a7dbb0e17aa7f5e85"
        logger.info("Using fallback widget key")
        return self._widget_key

    async def _auth_headers(self) -> dict:
        widget_key = await self._get_widget_key()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Widget-Key": widget_key,
            "X-Apple-OAuth-Client-Id": widget_key,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": "https://appstoreconnect.apple.com",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "Origin": "https://idmsa.apple.com",
            "Referer": "https://idmsa.apple.com/",
        }

    async def authenticate(self, apple_id: str, password: str) -> dict:
        self.session = AuthSession(apple_id=apple_id)

        headers = await self._auth_headers()
        body = {
            "accountName": apple_id,
            "password": password,
            "rememberMe": False,
        }

        logger.info("Authenticating %s", apple_id)
        status, resp_headers, resp_body = await self._curl(
            "POST", f"{AUTH_ENDPOINT}/signin", headers=headers, json_body=body,
        )
        logger.info("Signin response: HTTP %d", status)

        self._capture_headers(resp_headers)

        if status == 200:
            self.session.authenticated = True
            logger.info("Auth succeeded (no 2FA)")
            return {"status": "ok"}

        if status == 409:
            self.session.auth_type = resp_headers.get("X-Apple-Auth-Type", "")
            logger.info("2FA required (type: %s)", self.session.auth_type)
            return {"status": "2fa_required", "auth_type": self.session.auth_type}

        if status == 401:
            return {"status": "error", "message": "Incorrect Apple ID or password"}

        if status == 403:
            return {"status": "error", "message": "Account locked or requires verification at appleid.apple.com"}

        msg = self._extract_error(status, resp_body)
        logger.error("Auth failed (%d): %s", status, msg)
        return {"status": "error", "message": msg}

    async def submit_2fa(self, code: str) -> dict:
        if not self.session:
            return {"status": "error", "message": "No pending auth session"}

        headers = await self._auth_headers()
        if self.session.scnt:
            headers["scnt"] = self.session.scnt
        if self.session.session_id:
            headers["X-Apple-ID-Session-Id"] = self.session.session_id

        body = {"securityCode": {"code": code}}

        logger.info("Submitting 2FA code")
        status, resp_headers, resp_body = await self._curl(
            "POST",
            f"{AUTH_ENDPOINT}/verify/trusteddevice/securitycode",
            headers=headers,
            json_body=body,
        )

        if status not in (200, 204):
            msg = self._extract_error(status, resp_body)
            logger.error("2FA failed: %s", msg)
            return {"status": "error", "message": msg}

        # Trust the session
        trust_headers = dict(headers)
        status, resp_headers, _ = await self._curl(
            "GET", f"{AUTH_ENDPOINT}/2sv/trust", headers=trust_headers,
        )
        self._capture_headers(resp_headers)
        self.session.authenticated = True
        logger.info("2FA verified, session trusted")
        return {"status": "ok"}

    def _capture_headers(self, headers: dict):
        if "scnt" in headers:
            self.session.scnt = headers["scnt"]
        if "X-Apple-ID-Session-Id" in headers:
            self.session.session_id = headers["X-Apple-ID-Session-Id"]
        if "X-Apple-Session-Token" in headers:
            self.session.session_token = headers["X-Apple-Session-Token"]

    def _extract_error(self, status: int, body: str) -> str:
        try:
            data = json.loads(body)
            errors = data.get("serviceErrors", [])
            if errors:
                return errors[0].get("message", body[:200])
        except (json.JSONDecodeError, KeyError):
            pass
        return f"HTTP {status}: {body[:200]}"
