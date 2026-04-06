"""Apple Developer Services API — certificates, profiles, device registration."""

import logging
import plistlib
import ssl
import uuid

import httpx
import truststore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from catapult.anisette import get_anisette_http_headers
from catapult.apple_auth import AuthSession

logger = logging.getLogger(__name__)

DEV_SERVICES = "https://developerservices2.apple.com/services/QH65B2"


class DeveloperServicesError(RuntimeError):
    """Raised when Apple's developer services returns an error."""


class DeveloperServices:
    def __init__(self):
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True, verify=ctx)
        self._private_key: rsa.RSAPrivateKey | None = None
        self._cert_id: str | None = None

    def _auth_headers(self, session: AuthSession) -> dict:
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "Accept-Language": "en-us",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "11.2 (11B41)",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
        }
        # Developer services uses X-Apple-I-Identity-Id + X-Apple-GS-Token
        # NOT X-Apple-Identity-Token or myacinfo cookies
        if session.adsid:
            headers["X-Apple-I-Identity-Id"] = session.adsid
        if session.gs_token:
            headers["X-Apple-GS-Token"] = session.gs_token
        # Anisette headers with consistent device identity
        try:
            headers.update(get_anisette_http_headers())
        except Exception as e:
            logger.warning("Could not fetch Anisette for dev services: %s", e)
        return headers

    async def _request(self, session: AuthSession, endpoint: str, fields: dict | None = None) -> dict:
        payload = {
            "clientId": "XABBG36SBA",
            "protocolVersion": "QH65B2",
            "requestId": str(uuid.uuid4()).upper(),
        }
        if fields:
            payload.update(fields)

        body = plistlib.dumps(payload)
        url = f"{DEV_SERVICES}/{endpoint}.action?clientId=XABBG36SBA"
        resp = await self._client.post(url, content=body, headers=self._auth_headers(session))
        logger.debug("%s: HTTP %d (%d bytes)", endpoint, resp.status_code, len(resp.content))

        try:
            data = plistlib.loads(resp.content)
        except Exception:
            logger.error("%s: non-plist response: %s", endpoint, resp.text[:300])
            raise DeveloperServicesError(f"{endpoint}: invalid response (HTTP {resp.status_code})")

        # Check for API-level errors
        rc = data.get("resultCode", 0)
        if rc != 0:
            msg = data.get("userString") or data.get("resultString") or f"resultCode={rc}"
            # 35 = already exists (device, app id, etc.) — not fatal
            if rc == 35:
                logger.info("%s: already exists, continuing", endpoint)
                return data
            raise DeveloperServicesError(f"{endpoint}: {msg}")

        return data

    async def get_team(self, session: AuthSession) -> dict:
        data = await self._request(session, "listTeams")
        teams = data.get("teams", [])
        if not teams:
            raise DeveloperServicesError("No development teams found for this Apple ID")
        team = teams[0]
        logger.info("Using team: %s (%s)", team.get("name"), team.get("teamId"))
        return team

    async def get_or_create_cert(self, session: AuthSession, team_id: str) -> tuple[bytes, rsa.RSAPrivateKey]:
        """Generate a new signing key + CSR and submit to Apple. Returns (cert_pem, private_key)."""

        # Generate fresh keypair
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Catapult")]))
            .sign(self._private_key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        logger.info("Submitting CSR to Apple")
        try:
            data = await self._request(
                session,
                "ios/submitDevelopmentCSR",
                {"teamId": team_id, "csrContent": csr_pem, "machineId": "catapult-local"},
            )
        except DeveloperServicesError as e:
            if "already have" in str(e).lower():
                logger.info("Cert limit reached, revoking existing certs and retrying")
                await self._revoke_certs(session, team_id)
                data = await self._request(
                    session,
                    "ios/submitDevelopmentCSR",
                    {"teamId": team_id, "csrContent": csr_pem, "machineId": "catapult-local"},
                )
            else:
                raise

        cert_req = data.get("certRequest", {})
        self._cert_id = cert_req.get("certificateId", "")
        logger.info("CSR accepted, certificateId=%s", self._cert_id)

        # The CSR response doesn't include cert content — fetch it from cert list
        cert_content = await self._download_cert(session, team_id, self._cert_id)
        if not cert_content:
            raise DeveloperServicesError("Apple accepted CSR but certificate content not available")

        # certContent is DER-encoded; convert to PEM for codesign tooling
        if isinstance(cert_content, bytes) and not cert_content.startswith(b"-----"):
            cert_obj = x509.load_der_x509_certificate(cert_content)
            cert_pem = cert_obj.public_bytes(serialization.Encoding.PEM)
        else:
            cert_pem = cert_content if isinstance(cert_content, bytes) else cert_content.encode()

        logger.info("Signing certificate issued (id: %s)", self._cert_id)
        return cert_pem, self._private_key

    async def _download_cert(self, session: AuthSession, team_id: str, cert_id: str) -> bytes | None:
        """Fetch cert content by listing all certs and finding by ID."""
        data = await self._request(session, "ios/listAllDevelopmentCerts", {"teamId": team_id})
        for cert in data.get("certificates", []):
            if cert.get("certificateId") == cert_id:
                content = cert.get("certContent")
                if content:
                    logger.info("Downloaded cert %s (%d bytes)", cert_id, len(content))
                    return content
        logger.warning("Cert %s not found in list (%d certs)", cert_id, len(data.get("certificates", [])))
        return None

    async def _revoke_certs(self, session: AuthSession, team_id: str):
        """Revoke development certs to make room for a new one."""
        try:
            data = await self._request(session, "ios/listAllDevelopmentCerts", {"teamId": team_id})
            certs = data.get("certificates", [])
            logger.info("Found %d existing dev cert(s)", len(certs))
            for cert in certs:
                cid = cert.get("certificateId")
                name = cert.get("machineName", "?")
                logger.info("Revoking cert %s (%s)", cid, name)
                try:
                    await self._request(
                        session,
                        "ios/revokeDevelopmentCert",
                        {"teamId": team_id, "certificateId": cid, "serialNumber": cert.get("serialNumber", "")},
                    )
                except Exception as e:
                    logger.warning("Failed to revoke cert %s: %s", cid, e)
        except Exception as e:
            logger.debug("Cert list/revoke failed: %s", e)

    async def register_device(self, session: AuthSession, team_id: str, udid: str, name: str) -> dict:
        logger.info("Registering device %s (%s)", name, udid)
        return await self._request(
            session,
            "ios/addDevice",
            {"teamId": team_id, "deviceNumber": udid, "name": name or "Catapult Device"},
        )

    @staticmethod
    def sideload_bundle_id(team_id: str, original_bundle_id: str) -> str:
        """Create a unique bundle ID for sideloading (free accounts can't use taken IDs)."""
        safe = original_bundle_id.replace(".", "-")
        return f"com.catapult.{team_id}.{safe}"

    async def register_app_id(self, session: AuthSession, team_id: str, bundle_id: str) -> dict:
        sideload_id = self.sideload_bundle_id(team_id, bundle_id)
        logger.info("Registering app ID %s (original: %s)", sideload_id, bundle_id)
        try:
            data = await self._request(
                session,
                "ios/addAppId",
                {
                    "teamId": team_id,
                    "identifier": sideload_id,
                    "name": f"Catapult {bundle_id.rsplit('.', 1)[-1]}",
                    "enabledFeatures": {},
                "entitlements": {},
            },
        )
            return data.get("appId", data)
        except DeveloperServicesError as e:
            if "not available" in str(e).lower():
                logger.info("App ID already exists, looking it up")
                return await self._find_app_id(session, team_id, sideload_id)
            raise

    async def _find_app_id(self, session: AuthSession, team_id: str, identifier: str) -> dict:
        data = await self._request(session, "ios/listAppIds", {"teamId": team_id})
        for app in data.get("appIds", []):
            if app.get("identifier") == identifier:
                logger.info("Found existing app ID: %s", app.get("appIdId"))
                return app
        raise DeveloperServicesError(f"App ID {identifier} not found in list")

    async def create_profile(
        self,
        session: AuthSession,
        team_id: str,
        app_id: dict,
        cert_bytes: bytes,
        device_udid: str,
    ) -> bytes:
        app_id_id = app_id.get("appIdId", "")
        cert_ids = [self._cert_id] if self._cert_id else []

        # Delete existing profile for this app to ensure fresh cert/device
        await self._delete_profiles_for_app(session, team_id, app_id_id)

        logger.info("Creating provisioning profile (app=%s, cert=%s)", app_id_id, self._cert_id)
        data = await self._request(
            session,
            "ios/createProvisioningProfile",
            {
                "teamId": team_id,
                "appIdId": app_id_id,
                "certificateIds": cert_ids,
                "deviceIds": [device_udid],
                "distributionType": "limited",
                "template": "DEVELOPMENT",
            },
        )

        profile = data.get("provisioningProfile", {})
        encoded = profile.get("encodedProfile", b"")
        if not encoded:
            raise DeveloperServicesError("Apple did not return a provisioning profile")

        logger.info("Provisioning profile created (uuid: %s)", profile.get("UUID", "?"))
        return encoded

    async def _delete_profiles_for_app(self, session: AuthSession, team_id: str, app_id_id: str):
        """Delete existing provisioning profiles for an app ID so we can create a fresh one."""
        try:
            data = await self._request(session, "ios/listProvisioningProfiles", {
                "teamId": team_id,
                "includeInactiveProfiles": True,
            })
            for p in data.get("provisioningProfiles", []):
                if p.get("appIdId") == app_id_id:
                    pid = p.get("provisioningProfileId")
                    logger.info("Deleting old profile %s", pid)
                    await self._request(session, "ios/deleteProvisioningProfile", {
                        "teamId": team_id,
                        "provisioningProfileId": pid,
                    })
        except Exception as e:
            logger.debug("Profile cleanup skipped: %s", e)
