"""Apple Developer Services API -- certificates, profiles, device registration.

Implements the provisioning flow used by AltSign/AltStore/ReProvision:
  1. Fetch team
  2. Fetch existing certs -> revoke all -> submit new CSR
  3. Register device (idempotent, resultCode 35 = already exists)
  4. Register app ID (idempotent, or look up existing)
  5. Delete any stale provisioning profile for the app
  6. Download (create) provisioning profile via downloadTeamProvisioningProfile
     (Apple server-side creates/returns the profile with all registered certs+devices)

Key insight from AltSign: the endpoint is downloadTeamProvisioningProfile, NOT
createProvisioningProfile.  It only needs appIdId + teamId.  Apple's server
automatically includes all registered certificates and devices in the profile.
"""

import asyncio
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

from catapult import signing_identity
from catapult.anisette import get_anisette_http_headers
from catapult.apple_auth import AuthSession

logger = logging.getLogger(__name__)

DEV_SERVICES = "https://developerservices2.apple.com/services/QH65B2"

# Apple result codes
RC_SUCCESS = 0
RC_ALREADY_EXISTS = 35
RC_NOT_ALLOWED = 1200
RC_BUNDLE_ID_UNAVAILABLE = 9401


class DeveloperServicesError(RuntimeError):
    """Raised when Apple's developer services returns an error."""

    def __init__(self, message: str, result_code: int = 0):
        super().__init__(message)
        self.result_code = result_code


def team_is_free(team: dict) -> bool:
    """Whether a team is a free (Xcode personal) team.

    Team ``type`` cannot distinguish free from paid — a paid individual
    membership is also type "Individual". Apple marks free personal teams
    with ``xcodeFreeOnly``; paid teams carry active program memberships.
    """
    if team.get("xcodeFreeOnly"):
        return True
    memberships = team.get("memberships") or []
    return not any(m.get("status") == "active" for m in memberships)


class DeveloperServices:
    def __init__(self):
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True, verify=ctx)
        self._private_key: rsa.RSAPrivateKey | None = None
        self._cert_id: str | None = None
        self._cert_serial: str | None = None
        self._cert_lock = asyncio.Lock()

    # ── Auth headers ──

    def _auth_headers(self, session: AuthSession) -> dict:
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "Accept-Language": "en-us",
            "User-Agent": "Xcode",
            "X-Xcode-Version": "11.2 (11B41)",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
        }
        if session.adsid:
            headers["X-Apple-I-Identity-Id"] = session.adsid
        if session.gs_token:
            headers["X-Apple-GS-Token"] = session.gs_token
        try:
            headers.update(get_anisette_http_headers())
        except Exception as e:
            logger.warning("Could not fetch Anisette for dev services: %s", e)
        return headers

    # ── Low-level request ──

    async def _request(
        self,
        session: AuthSession,
        endpoint: str,
        fields: dict | None = None,
    ) -> dict:
        """Send a plist request to Apple Developer Services.

        Returns the parsed plist response dict.  Raises DeveloperServicesError
        on non-zero resultCode (except RC_ALREADY_EXISTS which is returned
        so callers can inspect the response).
        """
        payload: dict = {
            "clientId": "XABBG36SBA",
            "protocolVersion": "QH65B2",
            "requestId": str(uuid.uuid4()).upper(),
        }
        if fields:
            payload.update(fields)

        body = plistlib.dumps(payload)
        url = f"{DEV_SERVICES}/{endpoint}?clientId=XABBG36SBA"
        resp = await self._client.post(
            url, content=body, headers=self._auth_headers(session)
        )
        logger.debug(
            "%s: HTTP %d (%d bytes)", endpoint, resp.status_code, len(resp.content)
        )

        try:
            data = plistlib.loads(resp.content)
        except Exception:
            logger.error(
                "%s: non-plist response (%d bytes): %s",
                endpoint,
                len(resp.content),
                resp.text[:300],
            )
            raise DeveloperServicesError(
                f"{endpoint}: invalid response (HTTP {resp.status_code})"
            )

        rc = data.get("resultCode", 0)
        if rc == RC_SUCCESS:
            return data

        user_msg = (
            data.get("userString")
            or data.get("resultString")
            or f"resultCode={rc}"
        )

        # RC 35 = "already exists" -- not fatal, let caller decide
        if rc == RC_ALREADY_EXISTS:
            logger.info("%s: already exists (rc=35): %s", endpoint, user_msg)
            return data

        raise DeveloperServicesError(
            f"{endpoint}: {user_msg} (resultCode={rc})", result_code=rc
        )

    # ── 1. Team ──

    async def get_team(self, session: AuthSession) -> dict:
        """Fetch the preferred development team for this Apple ID.

        An Apple ID can belong to several teams — typically the free personal
        team plus one or more paid program teams. A paid team gets year-long
        profiles and 100 device slots, so prefer it over the personal team.
        """
        data = await self._request(session, "listTeams.action")
        teams = data.get("teams", [])
        if not teams:
            raise DeveloperServicesError("No development teams found for this Apple ID")
        active = [t for t in teams if t.get("status") == "active"] or teams
        team = next((t for t in active if not team_is_free(t)), active[0])
        logger.info(
            "Using team: %s (%s, type=%s, free=%s) of %d team(s)",
            team.get("name"),
            team.get("teamId"),
            team.get("type"),
            team_is_free(team),
            len(teams),
        )
        return team

    # ── 2. Certificates ──

    async def _list_certs(self, session: AuthSession, team_id: str) -> list[dict]:
        """List all development certificates for the team."""
        data = await self._request(
            session,
            "ios/listAllDevelopmentCerts.action",
            {"teamId": team_id},
        )
        certs = data.get("certificates", [])
        logger.info("Found %d existing development cert(s)", len(certs))
        return certs

    async def _revoke_cert(
        self, session: AuthSession, team_id: str, serial_number: str
    ):
        """Revoke a single development certificate by serial number.

        ReProvision/EEAppleServices uses serialNumber (not certificateId).
        """
        logger.info("Revoking cert with serial %s", serial_number)
        try:
            await self._request(
                session,
                "ios/revokeDevelopmentCert.action",
                {"teamId": team_id, "serialNumber": serial_number},
            )
        except DeveloperServicesError as e:
            logger.warning("Failed to revoke cert serial=%s: %s", serial_number, e)

    async def _revoke_cert_by_id(
        self, session: AuthSession, team_id: str, certificate_id: str
    ):
        """Revoke a single development certificate by certificateId.

        Apple's responses do not always include serialNumber for pending or
        recently-created certificates. Keep serial revocation as the preferred
        path, but fall back to certificateId so a stale pending cert does not
        permanently block new CSRs.
        """
        logger.info("Revoking cert with id %s", certificate_id)
        try:
            await self._request(
                session,
                "ios/revokeDevelopmentCert.action",
                {"teamId": team_id, "certificateId": certificate_id},
            )
        except DeveloperServicesError as e:
            logger.warning("Failed to revoke cert id=%s: %s", certificate_id, e)

    async def _revoke_all_certs(
        self, session: AuthSession, team_id: str, *, catapult_only: bool = False
    ):
        """Revoke development certs to make room for a new one.

        Free Apple IDs can only have a limited number of dev certs.
        AltSign revokes existing certs before creating a new one.

        On a shared paid team revoking everything would kill other members'
        certificates, so ``catapult_only`` restricts revocation to certs
        Catapult itself created (machineName "Catapult").
        """
        certs = await self._list_certs(session, team_id)
        if catapult_only:
            skipped = [c for c in certs if c.get("machineName") != "Catapult"]
            for cert in skipped:
                logger.info(
                    "Leaving cert alone (not Catapult's): %s (id=%s)",
                    cert.get("machineName", "?"),
                    cert.get("certificateId", "?"),
                )
            certs = [c for c in certs if c.get("machineName") == "Catapult"]
        for cert in certs:
            serial = cert.get("serialNumber", "")
            name = cert.get("machineName", "?")
            if serial:
                logger.info("Revoking cert: %s (serial=%s)", name, serial)
                await self._revoke_cert(session, team_id, serial)
            elif cert.get("certificateId"):
                logger.info(
                    "Revoking cert: %s (id=%s)",
                    name,
                    cert.get("certificateId"),
                )
                await self._revoke_cert_by_id(session, team_id, cert["certificateId"])
            else:
                logger.warning(
                    "Cert %s has no serialNumber, skipping revoke",
                    cert.get("certificateId", "?"),
                )

    async def get_or_create_cert(
        self, session: AuthSession, team_id: str, *, personal_team: bool = True
    ) -> tuple[bytes, rsa.RSAPrivateKey]:
        async with self._cert_lock:
            return await self._get_or_create_cert_locked(
                session, team_id, personal_team=personal_team
            )

    async def _get_or_create_cert_locked(
        self, session: AuthSession, team_id: str, *, personal_team: bool = True
    ) -> tuple[bytes, rsa.RSAPrivateKey]:
        """Ensure we have a valid signing certificate.

        Reuses the stored identity when Apple still lists it and it is not near
        expiry. Free-account certificates last a year — only the provisioning
        profile carries the 7-day clock — so minting a new one per refresh both
        wasted the credential and revoked whatever certificate Xcode, AltStore
        or a second Mac was using on the same Apple ID.

        Only when there is no usable identity do we fall back to the AltSign
        flow: revoke all, generate a fresh RSA key + CSR, submit, fetch content.

        Returns (cert_pem_bytes, private_key).
        """
        existing = signing_identity.load(team_id)
        if existing:
            apple_certs = await self._list_certs(session, team_id)
            if signing_identity.is_usable(existing, apple_certs):
                self._cert_id = existing.certificate_id
                self._cert_serial = existing.serial_number
                self._private_key = existing.private_key()
                logger.info(
                    "Reusing stored signing certificate (id=%s)", self._cert_id
                )
                return existing.cert_pem, self._private_key
            signing_identity.clear(team_id)

        # No usable identity — make room and mint a new one.
        logger.info("Revoking existing development certificates...")
        await self._revoke_all_certs(session, team_id, catapult_only=not personal_team)

        # Step 3: Generate fresh keypair + CSR
        self._private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Catapult")])
            )
            .sign(self._private_key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        # Step 4: Submit CSR
        # AltSign sends: csrContent, machineId (UUID), machineName
        machine_id = str(uuid.uuid4()).upper()
        logger.info("Submitting development CSR (machineId=%s)", machine_id)
        csr_fields = {
            "teamId": team_id,
            "csrContent": csr_pem,
            "machineId": machine_id,
            "machineName": "Catapult",
        }
        try:
            data = await self._request(
                session,
                "ios/submitDevelopmentCSR.action",
                csr_fields,
            )
        except DeveloperServicesError as e:
            if e.result_code != 7460:
                raise
            logger.info(
                "CSR was blocked by an existing or pending certificate; "
                "revoking and retrying once"
            )
            await self._revoke_all_certs(session, team_id, catapult_only=not personal_team)
            await asyncio.sleep(2)
            data = await self._request(
                session,
                "ios/submitDevelopmentCSR.action",
                csr_fields,
            )

        cert_req = data.get("certRequest", {})
        self._cert_id = cert_req.get("certificateId", "")
        self._cert_serial = cert_req.get("serialNumber", "")
        logger.info(
            "CSR accepted: certificateId=%s, serial=%s",
            self._cert_id,
            self._cert_serial,
        )

        # Step 5: Fetch the actual cert content
        # The CSR response contains a certRequest but often not the full cert
        # content. Fetch from the cert list.
        cert_content = await self._download_cert_content(session, team_id)
        if not cert_content:
            raise DeveloperServicesError(
                "Apple accepted CSR but certificate content is not available"
            )

        # Convert DER to PEM if needed
        if isinstance(cert_content, bytes) and not cert_content.startswith(b"-----"):
            cert_obj = x509.load_der_x509_certificate(cert_content)
            cert_pem = cert_obj.public_bytes(serialization.Encoding.PEM)
        else:
            cert_pem = (
                cert_content
                if isinstance(cert_content, bytes)
                else cert_content.encode()
            )

        signing_identity.save(
            team_id,
            signing_identity.SigningIdentity.from_key(
                cert_pem, self._private_key, self._cert_id, self._cert_serial
            ),
        )
        logger.info("Signing certificate ready (id=%s)", self._cert_id)
        return cert_pem, self._private_key

    async def revoke_all_certs(self, session: AuthSession, team_id: str) -> None:
        """Explicitly revoke every development certificate for the team.

        Destructive and user-initiated only: it invalidates apps signed by any
        machine on this Apple ID. Never called from the refresh path.
        """
        async with self._cert_lock:
            await self._revoke_all_certs(session, team_id)
            signing_identity.clear(team_id)
            self._cert_id = ""
            self._cert_serial = ""
            self._private_key = None

    async def _download_cert_content(
        self, session: AuthSession, team_id: str
    ) -> bytes | None:
        """Fetch cert content by listing all certs and finding our cert by ID."""
        certs = await self._list_certs(session, team_id)
        for cert in certs:
            if cert.get("certificateId") == self._cert_id:
                content = cert.get("certContent")
                if content:
                    logger.info(
                        "Downloaded cert %s (%d bytes)",
                        self._cert_id,
                        len(content),
                    )
                    return content
        # If only one cert exists (we just created it), use that
        if len(certs) == 1:
            content = certs[0].get("certContent")
            if content:
                logger.info(
                    "Using sole cert in list (%d bytes, id=%s)",
                    len(content),
                    certs[0].get("certificateId"),
                )
                self._cert_id = certs[0].get("certificateId", self._cert_id)
                self._cert_serial = certs[0].get("serialNumber", self._cert_serial)
                return content
        logger.warning(
            "Cert %s not found in list of %d certs", self._cert_id, len(certs)
        )
        return None

    # ── 3. Device registration ──

    async def register_device(
        self, session: AuthSession, team_id: str, udid: str, name: str
    ) -> dict:
        """Register a device.  Idempotent -- resultCode 35 means already registered."""
        device_name = name or "Catapult Device"
        logger.info("Registering device '%s' (%s)", device_name, udid)
        data = await self._request(
            session,
            "ios/addDevice.action",
            {
                "teamId": team_id,
                "deviceNumber": udid,
                "name": device_name,
            },
        )
        # rc=35 is fine (already registered)
        device = data.get("device", data)
        logger.info(
            "Device registered: %s (rc=%d)",
            device.get("name", device_name),
            data.get("resultCode", 0),
        )
        return device

    # ── 4. App ID registration ──

    @staticmethod
    def sideload_bundle_id(team_id: str, original_bundle_id: str) -> str:
        """Create a unique bundle ID for sideloading.

        Free accounts can't use bundle IDs that are already taken,
        so we prefix with a team-specific namespace.
        """
        safe = original_bundle_id.replace(".", "-")
        return f"com.catapult.{team_id}.{safe}"

    async def _list_app_ids(self, session: AuthSession, team_id: str) -> list[dict]:
        """List all registered app IDs for the team."""
        data = await self._request(
            session,
            "ios/listAppIds.action",
            {"teamId": team_id},
        )
        return data.get("appIds", [])

    async def _find_app_id(
        self, session: AuthSession, team_id: str, bundle_identifier: str
    ) -> dict | None:
        """Find an existing app ID by its bundle identifier."""
        app_ids = await self._list_app_ids(session, team_id)
        for app in app_ids:
            if app.get("identifier") == bundle_identifier:
                logger.info(
                    "Found existing app ID: %s (appIdId=%s)",
                    bundle_identifier,
                    app.get("appIdId"),
                )
                return app
        return None

    async def register_app_id(self, session: AuthSession, team_id: str, bundle_id: str) -> dict:
        """Register an exact app ID, or return the team's existing one.

        Preserve the IPA's bundle identifier by default. Rewriting it creates a
        second app on iOS/tvOS and loses the existing data container.
        """
        target_id = bundle_id
        short_name = bundle_id.rsplit(".", 1)[-1]
        # Sanitize name: AltSign strips diacritics and non-alphanumeric chars
        app_name = f"Catapult {short_name}"

        # Look up before registering. Apple caps free accounts at 10 App ID
        # registrations per 7 days, and the refresh loop runs hourly for the app
        # plus every embedded extension. Calling addAppId first each time burns
        # that quota and ends in an unrecoverable "You may only register 10 App
        # IDs every 7 days".
        existing = await self._find_app_id(session, team_id, target_id)
        if existing:
            return existing

        logger.info("Registering app ID: %s (name=%s)", target_id, app_name)
        try:
            data = await self._request(
                session,
                "ios/addAppId.action",
                {
                    "teamId": team_id,
                    "identifier": target_id,
                    "name": app_name,
                    "type": "explicit",
                    "enabledFeatures": {},
                    "entitlements": {},
                },
            )
            rc = data.get("resultCode", 0)

            if rc == RC_ALREADY_EXISTS:
                logger.info("App ID already exists (rc=35), looking it up")
                found = await self._find_app_id(session, team_id, target_id)
                if found:
                    return found
                # Shouldn't happen, but fall through

            # Success -- extract the appId from response
            app_id = data.get("appId")
            if app_id:
                logger.info(
                    "App ID registered: %s (appIdId=%s)",
                    target_id,
                    app_id.get("appIdId"),
                )
                return app_id

            # If no appId in response but no error, try looking it up
            logger.warning("addAppId succeeded but no appId in response, looking up")
            found = await self._find_app_id(session, team_id, target_id)
            if found:
                return found
            raise DeveloperServicesError(
                f"App ID {target_id} registered but could not be found"
            )

        except DeveloperServicesError as e:
            # resultCode 9401 = bundle ID unavailable (already taken by someone else)
            if e.result_code == RC_BUNDLE_ID_UNAVAILABLE or "not available" in str(
                e
            ).lower():
                logger.info(
                    "App ID bundle '%s' may already exist, looking it up",
                    target_id,
                )
                found = await self._find_app_id(session, team_id, target_id)
                if found:
                    return found
                wildcard = await self._get_or_create_wildcard_app_id(session, team_id)
                if wildcard:
                    logger.info(
                        "Using wildcard App ID %s to preserve bundle ID %s",
                        wildcard.get("identifier"),
                        target_id,
                    )
                    return wildcard
                raise DeveloperServicesError(
                    f"Cannot update '{target_id}' in place with this Apple ID. "
                    "Apple rejected the exact bundle ID and this account cannot create a "
                    "wildcard App ID. Use the same Apple ID/tool that installed the current "
                    "app, or install as a separate copy."
                ) from e
            raise

    async def _get_or_create_wildcard_app_id(
        self, session: AuthSession, team_id: str
    ) -> dict | None:
        """Return a wildcard App ID when Apple allows this account to create one."""
        for identifier in ("*", f"{team_id}.*"):
            found = await self._find_app_id(session, team_id, identifier)
            if found:
                return found

        try:
            data = await self._request(
                session,
                "ios/addAppId.action",
                {
                    "teamId": team_id,
                    "identifier": "*",
                    "name": "Catapult Wildcard",
                    "type": "wildcard",
                    "enabledFeatures": {},
                    "entitlements": {},
                },
            )
            app_id = data.get("appId")
            if app_id:
                logger.info("Wildcard App ID registered (appIdId=%s)", app_id.get("appIdId"))
                return app_id
            return await self._find_app_id(session, team_id, "*")
        except DeveloperServicesError as e:
            if e.result_code == RC_NOT_ALLOWED:
                logger.info("Wildcard App ID is not allowed for this Apple ID")
                return None
            logger.warning("Wildcard App ID creation failed: %s", e)
            return None

    # ── 5. Provisioning profiles ──

    async def _list_profiles(
        self, session: AuthSession, team_id: str
    ) -> list[dict]:
        """List all provisioning profiles for the team."""
        data = await self._request(
            session,
            "ios/listProvisioningProfiles.action",
            {
                "teamId": team_id,
                "includeInactiveProfiles": True,
            },
        )
        profiles = data.get("provisioningProfiles", [])
        logger.info("Found %d provisioning profile(s)", len(profiles))
        return profiles

    async def _delete_profile(
        self, session: AuthSession, team_id: str, profile_id: str
    ):
        """Delete a single provisioning profile by its provisioningProfileId."""
        logger.info("Deleting provisioning profile %s", profile_id)
        try:
            await self._request(
                session,
                "ios/deleteProvisioningProfile.action",
                {
                    "teamId": team_id,
                    "provisioningProfileId": profile_id,
                },
            )
            logger.info("Profile %s deleted", profile_id)
        except DeveloperServicesError as e:
            logger.warning("Failed to delete profile %s: %s", profile_id, e)

    async def _delete_profiles_for_app(
        self, session: AuthSession, team_id: str, app_id_id: str
    ):
        """Delete ALL provisioning profiles matching an appIdId.

        This ensures the next downloadTeamProvisioningProfile creates a fresh
        profile that includes our newly-created certificate and device.
        """
        profiles = await self._list_profiles(session, team_id)
        found_any = False
        for p in profiles:
            if p.get("appIdId") == app_id_id:
                found_any = True
                pid = p.get("provisioningProfileId", "")
                if pid:
                    await self._delete_profile(session, team_id, pid)
                else:
                    logger.warning(
                        "Profile for app %s has no provisioningProfileId", app_id_id
                    )
        if not found_any:
            logger.info("No existing profiles found for app %s", app_id_id)

    async def create_profile(
        self,
        session: AuthSession,
        team_id: str,
        app_id: dict,
        cert_bytes: bytes,
        device_udid: str,
        sub_platform: str | None = None,
    ) -> bytes:
        """Create/fetch a provisioning profile for the given app.

        This follows AltSign's approach:
          1. Delete any existing profile for this app (stale certs/devices)
          2. Call downloadTeamProvisioningProfile which makes Apple create
             a fresh profile server-side with all registered certs + devices

        The key insight: downloadTeamProvisioningProfile is the endpoint,
        NOT createProvisioningProfile.  It only needs appIdId.  Apple's
        server automatically includes all registered certificates and
        devices in the generated profile.
        """
        app_id_id = app_id.get("appIdId", "")
        if not app_id_id:
            raise DeveloperServicesError(
                "Cannot create profile: app_id dict has no appIdId field. "
                f"Keys present: {list(app_id.keys())}"
            )

        # Step 1: Delete stale profiles so Apple creates a fresh one
        logger.info("Cleaning up existing profiles for app %s...", app_id_id)
        await self._delete_profiles_for_app(session, team_id, app_id_id)

        # Step 2: Download (create) profile
        # AltSign sends ONLY appIdId + teamId.  No certificateIds, deviceIds,
        # or distributionType.  Apple handles it server-side.
        logger.info(
            "Requesting provisioning profile (appIdId=%s, team=%s)",
            app_id_id,
            team_id,
        )
        body = {"teamId": team_id, "appIdId": app_id_id}
        if sub_platform:
            body["subPlatform"] = sub_platform
        data = await self._request(
            session,
            "ios/downloadTeamProvisioningProfile.action",
            body,
        )

        profile = data.get("provisioningProfile", {})
        encoded = profile.get("encodedProfile", b"")

        if not encoded:
            # Log what we got for debugging
            profile_keys = list(profile.keys()) if profile else []
            data_keys = list(data.keys())
            logger.error(
                "downloadTeamProvisioningProfile returned no encodedProfile. "
                "profile keys=%s, response keys=%s, resultCode=%s",
                profile_keys,
                data_keys,
                data.get("resultCode", "?"),
            )
            raise DeveloperServicesError(
                "Apple returned an empty provisioning profile. "
                "This can happen if the app ID or certificate is in an "
                "inconsistent state.  Try signing in again."
            )

        profile_uuid = profile.get("UUID", profile.get("provisioningProfileId", "?"))
        logger.info(
            "Provisioning profile ready (UUID=%s, %d bytes)",
            profile_uuid,
            len(encoded),
        )
        return encoded

    # ── Full provisioning flow (convenience) ──

    async def provision(
        self,
        session: AuthSession,
        bundle_id: str,
        device_udid: str,
        device_name: str,
    ) -> tuple[bytes, rsa.RSAPrivateKey, bytes, str]:
        """Run the complete provisioning flow.

        Returns (cert_pem, private_key, profile_bytes, sideload_bundle_id).

        Order of operations (matches AltSign):
          1. Fetch team
          2. Revoke old certs + create new cert
          3. Register device
          4. Register app ID
          5. Delete old profile + download fresh profile
        """
        # 1. Team
        team = await self.get_team(session)
        team_id = team["teamId"]

        # 2. Certificate
        cert_pem, private_key = await self.get_or_create_cert(
            session, team_id, personal_team=team_is_free(team)
        )

        # 3. Device
        await self.register_device(session, team_id, device_udid, device_name)

        # 4. App ID
        app_id = await self.register_app_id(session, team_id, bundle_id)

        # 5. Profile
        profile = await self.create_profile(
            session, team_id, app_id, cert_pem, device_udid
        )

        return cert_pem, private_key, profile, bundle_id
