"""Apple Developer Services API — certificates, profiles, device registration."""

import logging
import plistlib

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from catapult.anisette import get_anisette_headers
from catapult.apple_auth import AuthSession

logger = logging.getLogger(__name__)

DEV_SERVICES = "https://developerservices2.apple.com/services/QH65B2"


class DeveloperServicesError(RuntimeError):
    """Raised when Apple's developer services returns an error."""


class DeveloperServices:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._private_key: rsa.RSAPrivateKey | None = None
        self._cert_id: str | None = None

    def _auth_headers(self, session: AuthSession) -> dict:
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "15.0 (15A240d)",
            "Cookie": "; ".join(f"{k}={v}" for k, v in session.cookies.items()),
        }
        headers.update(get_anisette_headers())
        return headers

    async def _request(self, session: AuthSession, endpoint: str, fields: dict | None = None) -> dict:
        payload = {
            "clientId": "XABBG36SBA",
            "protocolVersion": "QH65B2",
            "requestId": endpoint,
        }
        if fields:
            payload.update(fields)

        body = plistlib.dumps(payload)
        resp = await self._client.post(
            f"{DEV_SERVICES}/{endpoint}",
            content=body,
            headers=self._auth_headers(session),
        )

        try:
            data = plistlib.loads(resp.content)
        except Exception:
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
        # Revoke old Catapult certs to stay under the free-account limit
        await self._cleanup_old_certs(session, team_id)

        # Generate fresh keypair
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Catapult")]))
            .sign(self._private_key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        logger.info("Submitting CSR to Apple")
        data = await self._request(
            session,
            "submitDevelopmentCSR",
            {"teamId": team_id, "csrContent": csr_pem, "machineId": "catapult-local"},
        )

        cert_info = data.get("certRequest", data)
        cert_content = cert_info.get("certContent") or cert_info.get("certificate", {}).get("certContent")
        if not cert_content:
            raise DeveloperServicesError("Apple did not return a certificate")

        self._cert_id = cert_info.get("certificateId") or cert_info.get("certificate", {}).get("certificateId")

        # certContent is DER-encoded; convert to PEM for codesign tooling
        if isinstance(cert_content, bytes) and not cert_content.startswith(b"-----"):
            cert_obj = x509.load_der_x509_certificate(cert_content)
            cert_pem = cert_obj.public_bytes(serialization.Encoding.PEM)
        else:
            cert_pem = cert_content if isinstance(cert_content, bytes) else cert_content.encode()

        logger.info("Signing certificate issued (id: %s)", self._cert_id)
        return cert_pem, self._private_key

    async def _cleanup_old_certs(self, session: AuthSession, team_id: str):
        """Revoke any previous Catapult certs to avoid hitting the 2-cert limit on free accounts."""
        try:
            data = await self._request(session, "listAllDevelopmentCerts", {"teamId": team_id})
            for cert in data.get("certificates", []):
                name = cert.get("machineName", "")
                if name == "catapult-local":
                    cid = cert.get("certificateId")
                    logger.info("Revoking old Catapult cert %s", cid)
                    await self._request(
                        session,
                        "revokeDevelopmentCert",
                        {"teamId": team_id, "certificateId": cid, "serialNumber": cert.get("serialNumber", "")},
                    )
        except Exception as e:
            logger.debug("Cert cleanup skipped: %s", e)

    async def register_device(self, session: AuthSession, team_id: str, udid: str, name: str) -> dict:
        logger.info("Registering device %s (%s)", name, udid)
        return await self._request(
            session,
            "addDevice",
            {"teamId": team_id, "deviceNumber": udid, "name": name or "Catapult Device"},
        )

    async def register_app_id(self, session: AuthSession, team_id: str, bundle_id: str) -> dict:
        logger.info("Registering app ID %s", bundle_id)
        data = await self._request(
            session,
            "addAppId",
            {
                "teamId": team_id,
                "identifier": bundle_id,
                "name": f"Catapult {bundle_id.rsplit('.', 1)[-1]}",
                "enabledFeatures": {},
                "entitlements": {},
            },
        )
        return data.get("appId", data)

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

        logger.info("Creating provisioning profile (app=%s, cert=%s)", app_id_id, self._cert_id)
        data = await self._request(
            session,
            "createProvisioningProfile",
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
