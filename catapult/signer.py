"""IPA signing via temporary keychain and macOS codesign."""

import asyncio
import base64
import logging
import plistlib
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from catapult import provisioning
from catapult.ipa import IpaProcessor

logger = logging.getLogger(__name__)

APPLE_WWDR_G3_CERT_DER_B64 = """
MIIEUTCCAzmgAwIBAgIQfK9pCiW3Of57m0R6wXjF7jANBgkqhkiG9w0BAQsFADBiMQswCQYDVQQG
EwJVUzETMBEGA1UEChMKQXBwbGUgSW5jLjEmMCQGA1UECxMdQXBwbGUgQ2VydGlmaWNhdGlvbiBB
dXRob3JpdHkxFjAUBgNVBAMTDUFwcGxlIFJvb3QgQ0EwHhcNMjAwMjE5MTgxMzQ3WhcNMzAwMjIw
MDAwMDAwWjB1MUQwQgYDVQQDDDtBcHBsZSBXb3JsZHdpZGUgRGV2ZWxvcGVyIFJlbGF0aW9ucyBD
ZXJ0aWZpY2F0aW9uIEF1dGhvcml0eTELMAkGA1UECwwCRzMxEzARBgNVBAoMCkFwcGxlIEluYy4x
CzAJBgNVBAYTAlVTMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2PWJ/KhZC4fHTJEu
LVaQ03gdpDDppUjvC0O/LYT7JF1FG+XrWTYSXFRknmxiLbTGl8rMPPbWBpH85QKmHGq0edVny6zp
PwcR4YS8Rx1mjjmi6LRJ7TrS4RBgeo6TjMrA2gzAg9Dj+ZHWp4zIwXPirkbRYp2SqJBgN31ols2N
4Pyb+ni743uvLRfdW/6AWSN1F7gSwe0b5TTO/iK1nkmw5VW/j4SiPKi6xYaVFuQAyZ8D0MyzOhZ7
1gVcnetHrg21LYwOaU1A0EtMOwSejSGxrC5DVDDOwYqGlJhL32oNP/77HK6XF8J4CjDgXx9UO0m3
JQAaN4LSVpelUkl8YDib7wIDAQABo4HvMIHsMBIGA1UdEwEB/wQIMAYBAf8CAQAwHwYDVR0jBBgw
FoAUK9BpR5R2Cf70a40uQKb3R01/CF4wRAYIKwYBBQUHAQEEODA2MDQGCCsGAQUFBzABhihodHRw
Oi8vb2NzcC5hcHBsZS5jb20vb2NzcDAzLWFwcGxlcm9vdGNhMC4GA1UdHwQnMCUwI6AhoB+GHWh0
dHA6Ly9jcmwuYXBwbGUuY29tL3Jvb3QuY3JsMB0GA1UdDgQWBBQJ/sAVkPmvZAqSErkmKGMMl+yn
sjAOBgNVHQ8BAf8EBAMCAQYwEAYKKoZIhvdjZAYCAQQCBQAwDQYJKoZIhvcNAQELBQADggEBAK1l
E+j24IF3RAJHQr5fpTkg6mKp/cWQyXMT1Z6b0KoPjY3L7QHPbChAW8dVJEH4/M/BtSPp3Ozxb8qA
HXfCxGFJJWevD8o5Ja3T43rMMygNDi6hV0Bz+uZcrgZRKe3jhQxPYdwyFot30ETKXXIDMUacrptA
Gvr04NM++i+MZp+XxFRZ79JI9AeZSWBZGcfdlNHAwWx/eCHvDOs7bJmCS1JgOLU5gm3sUjFTvg+R
TElJdI+mUcuER04ddSduvfnSXPN/wmwLCTbiZOTCNwMUGdXqapSqqdv+9poIZ4vvK7iqF0mDr8/L
vOnP6pVxsLRFoszlh6oKw0E6eVzaUDSdlTs=
"""


class Signer:
    def __init__(self):
        self._ipa = IpaProcessor()

    async def sign(
        self,
        ipa_path: str | Path,
        cert_bytes: bytes,
        private_key: rsa.RSAPrivateKey,
        profile_bytes: bytes,
        new_bundle_id: str | None = None,
        extension_profiles: dict[str, bytes] | None = None,
    ) -> Path:
        ipa_path = Path(ipa_path)
        work_dir = Path(tempfile.mkdtemp(prefix="catapult_sign_"))

        try:
            app_dir = await self._ipa.extract(ipa_path, work_dir)
            logger.info("Extracted %s to %s", ipa_path.name, app_dir)

            self.strip_watch_apps(app_dir)

            # Update bundle ID for sideloading
            if new_bundle_id:
                self._update_bundle_id(app_dir, new_bundle_id)

            # Embed provisioning profile
            (app_dir / "embedded.mobileprovision").write_bytes(profile_bytes)

            # Extract entitlements from the profile. Nested app extensions need
            # their own application-identifier matching their rewritten bundle ID.
            entitlements_dir = work_dir / "entitlements"
            entitlements_dir.mkdir()
            entitlements_path = entitlements_dir / "main.plist"
            self._extract_entitlements(profile_bytes, entitlements_path, self._bundle_id(app_dir))

            # Build p12 and set up isolated keychain
            cert_path = work_dir / "cert.pem"
            key_path = work_dir / "key.pem"
            cert_path.write_bytes(cert_bytes)
            key_path.write_bytes(
                private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

            p12_path = work_dir / "cert.p12"
            await self._create_p12(cert_path, key_path, p12_path)

            keychain = await self._setup_keychain(work_dir, p12_path)
            await self._import_apple_certificate_chain(work_dir, keychain)
            identity = await self._find_identity(keychain)

            try:
                await self._codesign_app(
                    app_dir,
                    keychain,
                    identity,
                    entitlements_path,
                    profile_bytes,
                    entitlements_dir,
                    extension_profiles or {},
                )
                await self._verify_codesign(app_dir)
            finally:
                await self._cleanup_keychain(keychain)

            signed_ipa = work_dir / f"{ipa_path.stem}_signed.ipa"
            await self._ipa.repack(app_dir, signed_ipa)

            # Move signed IPA out of work_dir before cleanup
            from catapult.ipa import UPLOAD_DIR
            final_path = UPLOAD_DIR / signed_ipa.name
            shutil.move(str(signed_ipa), str(final_path))
            shutil.rmtree(work_dir, ignore_errors=True)

            logger.info("Signed IPA written to %s", final_path)
            return final_path
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def _extract_entitlements(self, profile_bytes: bytes, dest: Path, bundle_id: str):
        """Pull entitlements dict from a provisioning profile and write as plist."""
        try:
            plist_data = provisioning.parse_profile_plist(profile_bytes)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        entitlements = plist_data.get("Entitlements", {})
        team_id = entitlements.get("com.apple.developer.team-identifier", "")
        app_identifier = entitlements.get("application-identifier", "")
        if team_id:
            entitlements["application-identifier"] = f"{team_id}.{bundle_id}"
        keychain_groups = entitlements.get("keychain-access-groups")
        if isinstance(keychain_groups, list):
            entitlements["keychain-access-groups"] = [
                f"{team_id}.{bundle_id}"
                if isinstance(group, str) and team_id and (group.endswith(".*") or group == app_identifier)
                else group
                for group in keychain_groups
            ]
        dest.write_bytes(plistlib.dumps(entitlements))
        logger.info("Extracted entitlements: %s", list(entitlements.keys()))

    def _bundle_id(self, app_dir: Path) -> str:
        plist_path = app_dir / "Info.plist"
        with plist_path.open("rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleIdentifier", "")

    async def _create_p12(self, cert_path: Path, key_path: Path, p12_path: Path):
        # -legacy is required on OpenSSL 3.x (RC2 moved to legacy provider)
        # but unknown to LibreSSL, which is what ships at /usr/bin/openssl on macOS.
        version = await self._run("openssl", "version", label="openssl-version")
        args = ["openssl", "pkcs12", "-export"]
        if version.startswith("OpenSSL 3"):
            args.append("-legacy")
        args += [
            "-out", str(p12_path),
            "-inkey", str(key_path),
            "-in", str(cert_path),
            "-passout", "pass:catapult",
        ]
        await self._run(*args, label="create-p12")

    async def _setup_keychain(self, work_dir: Path, p12_path: Path) -> str:
        keychain = str(work_dir / "catapult.keychain-db")
        pwd = "catapult-tmp"

        await self._run("security", "create-keychain", "-p", pwd, keychain, label="create-keychain")
        await self._run("security", "set-keychain-settings", keychain, label="set-keychain-settings")
        await self._run("security", "unlock-keychain", "-p", pwd, keychain, label="unlock-keychain")
        await self._run(
            "security", "import", str(p12_path),
            "-k", keychain, "-P", "catapult", "-T", "/usr/bin/codesign",
            label="import-cert",
        )
        await self._run(
            "security", "set-key-partition-list",
            "-S", "apple-tool:,apple:", "-s", "-k", pwd, keychain,
            label="set-partition-list",
        )

        # Add temp keychain to search list so codesign can find identities
        existing = await self._run("security", "list-keychains", "-d", "user", label="list-keychains")
        # Parse existing keychains (each line is a quoted path)
        existing_paths = [line.strip().strip('"') for line in existing.splitlines() if line.strip()]
        all_keychains = [keychain] + existing_paths
        await self._run("security", "list-keychains", "-d", "user", "-s", *all_keychains, label="add-to-search")
        self._original_keychains = existing_paths

        logger.info("Temporary keychain ready at %s", keychain)
        return keychain

    async def _import_apple_certificate_chain(self, work_dir: Path, keychain: str):
        """Import Apple's developer signing chain into the temporary keychain.

        Developer Services can issue "iPhone Developer" certs under either the
        older WWDR intermediate or WWDR G3. codesign only receives our temporary
        keychain via --keychain, so make that keychain self-contained instead
        of relying on whatever chain search list launchd happened to inherit.
        """
        cert_path = work_dir / "AppleWWDRCAG3.cer"
        cert_bytes = base64.b64decode("".join(APPLE_WWDR_G3_CERT_DER_B64.split()))
        cert_path.write_bytes(cert_bytes)
        await self._run(
            "security", "add-certificates", "-k", keychain, str(cert_path),
            label="import-bundled-wwdr-g3", check=False,
        )

        await self._import_system_certificates(
            work_dir,
            keychain,
            "Apple Worldwide Developer Relations Certification Authority",
            "wwdr",
        )
        await self._import_system_certificates(
            work_dir,
            keychain,
            "Apple Root CA",
            "apple-root",
        )
        logger.info("Imported Apple signing certificate chain into temporary keychain")

    async def _import_system_certificates(
        self,
        work_dir: Path,
        keychain: str,
        common_name: str,
        filename: str,
    ):
        search_keychains = [
            "/Library/Keychains/System.keychain",
            "/System/Library/Keychains/SystemRootCertificates.keychain",
            str(Path.home() / "Library/Keychains/login.keychain-db"),
        ]
        existing_keychains = [p for p in search_keychains if Path(p).exists()]
        if not existing_keychains:
            return

        pem = await self._run(
            "security",
            "find-certificate",
            "-a",
            "-p",
            "-c",
            common_name,
            *existing_keychains,
            label=f"find-cert:{filename}",
            check=False,
        )
        if "BEGIN CERTIFICATE" not in pem:
            logger.info("No system certificates found for %s", common_name)
            return

        cert_path = work_dir / f"{filename}.pem"
        cert_path.write_text(pem)
        await self._run(
            "security",
            "add-certificates",
            "-k",
            keychain,
            str(cert_path),
            label=f"import-cert:{filename}",
            check=False,
        )

    async def _find_identity(self, keychain: str) -> str:
        """Find the signing identity hash in the keychain."""
        proc = await asyncio.create_subprocess_exec(
            "security", "find-identity", "-v", "-p", "codesigning", keychain,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="replace")
        errors = stderr.decode(errors="replace").strip()
        if errors:
            logger.warning("find-identity stderr: %s", errors)
        # Output lines look like:  1) AABB1122... "iPhone Developer: ..."
        for line in output.splitlines():
            line = line.strip()
            if ")" in line and '"' in line:
                parts = line.split(")")[1].strip().split('"')[0].strip()
                if parts:
                    logger.info("Found signing identity: %s", parts[:12] + "...")
                    return parts

        certs = await self._run(
            "security", "find-certificate", "-a", "-Z", keychain,
            label="list-certificates",
            check=False,
        )
        logger.error("No valid signing identities found. find-identity output:\n%s", output.strip())
        logger.debug("Temporary keychain certificates:\n%s", certs.strip())
        raise RuntimeError(
            "No valid Apple development signing identity was created. "
            "Catapult refused to ad-hoc sign because tvOS will reject that IPA."
        )

    async def _cleanup_keychain(self, keychain: str):
        # Restore original keychain search list
        if hasattr(self, '_original_keychains') and self._original_keychains:
            await self._run(
                "security", "list-keychains", "-d", "user", "-s", *self._original_keychains,
                label="restore-keychains", check=False,
            )
        await self._run("security", "delete-keychain", keychain, label="delete-keychain", check=False)

    async def _codesign_app(
        self,
        app_dir: Path,
        keychain: str,
        identity: str,
        entitlements: Path,
        profile_bytes: bytes,
        entitlements_dir: Path,
        extension_profiles: dict[str, bytes],
    ):
        """Sign all signable content inside the app bundle."""
        # Frameworks
        frameworks_dir = app_dir / "Frameworks"
        if frameworks_dir.exists():
            for item in sorted(frameworks_dir.iterdir()):
                await self._codesign_path(item, keychain, identity, entitlements=None)

        # PlugIns / app extensions
        plugins_dir = app_dir / "PlugIns"
        if plugins_dir.exists():
            for item in sorted(plugins_dir.iterdir()):
                if item.suffix == ".appex":
                    bundle_id = self._bundle_id(item)
                    item_profile = extension_profiles.get(bundle_id, profile_bytes)
                    (item / "embedded.mobileprovision").write_bytes(item_profile)
                    appex_entitlements = entitlements_dir / f"{item.stem}.plist"
                    self._extract_entitlements(item_profile, appex_entitlements, bundle_id)
                    await self._codesign_path(item, keychain, identity, appex_entitlements)
                else:
                    await self._codesign_path(item, keychain, identity, entitlements=None)

        # Watch apps
        watch_dir = app_dir / "Watch"
        if watch_dir.exists():
            for item in sorted(watch_dir.rglob("*.app")):
                await self._codesign_path(item, keychain, identity, entitlements=None)

        # Main binary
        await self._codesign_path(app_dir, keychain, identity, entitlements)
        logger.info("All components signed")

    async def _verify_codesign(self, app_dir: Path):
        """Verify that the app has a real Team ID signature before install."""
        await self._run(
            "codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_dir),
            label="verify-codesign",
        )
        proc = await asyncio.create_subprocess_exec(
            "codesign", "-dv", "--verbose=4", str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        details = "\n".join(
            part.decode(errors="replace").strip()
            for part in (stdout, stderr)
            if part
        )
        if proc.returncode != 0:
            raise RuntimeError(f"[inspect-codesign] failed (rc={proc.returncode}): {details}")
        if "Signature=adhoc" in details or "TeamIdentifier=not set" in details:
            raise RuntimeError("Signed app still has an ad-hoc signature; refusing to install it")
        logger.info("codesign verification passed")

    async def _codesign_path(
        self,
        path: Path,
        keychain: str,
        identity: str,
        entitlements: Path | None,
    ):
        cmd = [
            "codesign", "--force", "--sign", identity,
            "--keychain", keychain,
            "--generate-entitlement-der",
        ]
        if entitlements:
            cmd += ["--entitlements", str(entitlements)]
        else:
            # Pin the signing identifier to the bundle's own CFBundleIdentifier
            # rather than preserving whatever the original signature carried.
            # Repackaged IPAs often disagree: ellekit ships as
            # CydiaSubstrate.framework with CFBundleIdentifier "ellekit" but a
            # signing identifier of "CydiaSubstrate", and installd rejects the
            # install with MismatchedBundleIDSigningIdentifier.
            bundle_id = self._nested_bundle_identifier(path)
            if bundle_id:
                cmd += ["-i", bundle_id, "--preserve-metadata=entitlements,flags"]
            else:
                # No Info.plist (a bare dylib): codesign derives the identifier
                # from the filename, which is what we want.
                cmd.append("--preserve-metadata=identifier,entitlements,flags")
        cmd.append(str(path))

        await self._run(*cmd, label=f"codesign:{path.name}")

    @staticmethod
    def _nested_bundle_identifier(path: Path) -> str | None:
        """CFBundleIdentifier of a nested bundle, or None if it has no plist."""
        if not path.is_dir():
            return None
        for candidate in (path / "Info.plist", path / "Resources" / "Info.plist"):
            if not candidate.exists():
                continue
            try:
                with candidate.open("rb") as f:
                    identifier = plistlib.load(f).get("CFBundleIdentifier", "")
            except Exception:
                logger.debug("Could not read %s", candidate, exc_info=True)
                return None
            if identifier:
                return str(identifier)
        return None

    # An App Store build ships its watchOS app either as a real bundle under
    # Watch/ or, more often, as an on-demand placeholder under this name.
    WATCH_DIRS = ("com.apple.WatchPlaceholder", "Watch")

    def strip_watch_apps(self, app_dir: Path):
        """Drop any watchOS app from the payload before signing.

        A placeholder has no executable, so it could never run sideloaded, and
        a real watch app would need watchOS App IDs and profiles that Catapult
        does not create. Either way its WKCompanionAppBundleIdentifier still
        names the original app, and once the bundle ID is namespaced installd
        rejects the entire install with InvalidCompanionAppBundleIdentifier.
        """
        for name in self.WATCH_DIRS:
            watch_dir = app_dir / name
            if not watch_dir.is_dir():
                continue
            shutil.rmtree(watch_dir, ignore_errors=True)
            logger.info("Removed the watch app at %s: Catapult cannot provision watchOS", name)

    def _update_bundle_id(self, app_dir: Path, new_id: str):
        """Rewrite CFBundleIdentifier in the app and nested app extensions."""
        plist_path = app_dir / "Info.plist"
        with plist_path.open("rb") as f:
            info = plistlib.load(f)
        old_id = info.get("CFBundleIdentifier", "")
        info["CFBundleIdentifier"] = new_id
        with plist_path.open("wb") as f:
            plistlib.dump(info, f)
        logger.info("Bundle ID: %s → %s", old_id, new_id)

        plugins_dir = app_dir / "PlugIns"
        if not old_id or not plugins_dir.exists():
            return

        for appex_dir in sorted(plugins_dir.glob("*.appex")):
            appex_plist = appex_dir / "Info.plist"
            if not appex_plist.exists():
                continue
            with appex_plist.open("rb") as f:
                appex_info = plistlib.load(f)
            old_extension_id = appex_info.get("CFBundleIdentifier", "")
            if not old_extension_id:
                continue
            if old_extension_id == old_id:
                new_extension_id = new_id
            elif old_extension_id.startswith(old_id + "."):
                new_extension_id = new_id + old_extension_id[len(old_id):]
            else:
                new_extension_id = f"{new_id}.{old_extension_id.rsplit('.', 1)[-1]}"
            appex_info["CFBundleIdentifier"] = new_extension_id
            with appex_plist.open("wb") as f:
                plistlib.dump(appex_info, f)
            logger.info("Extension Bundle ID: %s → %s", old_extension_id, new_extension_id)

    async def _run(self, *cmd: str, label: str = "", check: bool = True):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(f"[{label}] failed (rc={proc.returncode}): {stderr.decode().strip()}")
        return stdout.decode()
