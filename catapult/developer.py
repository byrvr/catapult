import plistlib

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from catapult.apple_auth import AuthSession

DEV_SERVICES = "https://developerservices2.apple.com/services/QH65B2"


class DeveloperServices:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._private_key: rsa.RSAPrivateKey | None = None

    def _auth_headers(self, session: AuthSession) -> dict:
        return {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "15.0 (15A240d)",
            "Cookie": "; ".join(f"{k}={v}" for k, v in session.cookies.items()),
        }

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
        return plistlib.loads(resp.content)

    async def get_team(self, session: AuthSession) -> dict:
        data = await self._request(session, "listTeams")
        teams = data.get("teams", [])
        if not teams:
            raise RuntimeError("No development teams found for this Apple ID")
        return teams[0]

    async def get_or_create_cert(self, session: AuthSession, team_id: str) -> tuple[bytes, rsa.RSAPrivateKey]:
        list_data = await self._request(session, "listAllDevelopmentCerts", {"teamId": team_id})
        certs = list_data.get("certificates", [])

        for cert in certs:
            if cert.get("certContent"):
                # We don't have the private key for existing certs, so create a new one
                pass

        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Catapult")]))
            .sign(self._private_key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM)

        submit_data = await self._request(
            session,
            "submitDevelopmentCSR",
            {
                "teamId": team_id,
                "csrContent": csr_pem.decode(),
                "machineId": "catapult-local",
            },
        )

        cert_bytes = submit_data.get("certContent", b"")
        if not cert_bytes:
            raise RuntimeError("Failed to create signing certificate")

        return cert_bytes, self._private_key

    async def register_device(self, session: AuthSession, team_id: str, udid: str, name: str) -> dict:
        data = await self._request(
            session,
            "addDevice",
            {"teamId": team_id, "deviceNumber": udid, "name": name},
        )
        return data

    async def register_app_id(self, session: AuthSession, team_id: str, bundle_id: str) -> dict:
        data = await self._request(
            session,
            "addAppId",
            {
                "teamId": team_id,
                "identifier": bundle_id,
                "name": f"Catapult {bundle_id.split('.')[-1]}",
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
        cert_id = app_id.get("certId") or ""

        data = await self._request(
            session,
            "createProvisioningProfile",
            {
                "teamId": team_id,
                "appIdId": app_id.get("appIdId", ""),
                "certificateIds": [cert_id] if cert_id else [],
                "deviceIds": [device_udid],
                "distributionType": "limited",
                "template": "DEVELOPMENT",
            },
        )

        profile = data.get("provisioningProfile", {})
        encoded = profile.get("encodedProfile", b"")
        if not encoded:
            raise RuntimeError("Failed to create provisioning profile")
        return encoded
