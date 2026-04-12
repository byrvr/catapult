"""IPA signing via temporary keychain and macOS codesign."""

import asyncio
import logging
import plistlib
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from catapult.ipa import IpaProcessor

logger = logging.getLogger(__name__)


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
    ) -> Path:
        ipa_path = Path(ipa_path)
        work_dir = Path(tempfile.mkdtemp(prefix="catapult_sign_"))

        try:
            app_dir = await self._ipa.extract(ipa_path, work_dir)
            logger.info("Extracted %s to %s", ipa_path.name, app_dir)

            # Update bundle ID for sideloading
            if new_bundle_id:
                self._update_bundle_id(app_dir, new_bundle_id)

            # Embed provisioning profile
            (app_dir / "embedded.mobileprovision").write_bytes(profile_bytes)

            # Extract entitlements from the profile
            entitlements_path = work_dir / "entitlements.plist"
            self._extract_entitlements(profile_bytes, entitlements_path)

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
            identity = await self._find_identity(keychain)

            try:
                await self._codesign_app(app_dir, keychain, identity, entitlements_path)
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

    def _extract_entitlements(self, profile_bytes: bytes, dest: Path):
        """Pull entitlements dict from a provisioning profile and write as plist."""
        # Profile is a CMS signed blob; the plist payload is between
        # the first <?xml and </plist> tags.
        raw = profile_bytes.decode("latin-1")
        start = raw.find("<?xml")
        end = raw.find("</plist>") + len("</plist>")
        if start < 0 or end <= len("</plist>"):
            raise RuntimeError("Could not parse provisioning profile")

        plist_data = plistlib.loads(raw[start:end].encode("latin-1"))
        entitlements = plist_data.get("Entitlements", {})
        dest.write_bytes(plistlib.dumps(entitlements))
        logger.info("Extracted entitlements: %s", list(entitlements.keys()))

    async def _create_p12(self, cert_path: Path, key_path: Path, p12_path: Path):
        await self._run(
            "openssl", "pkcs12", "-export", "-legacy",
            "-out", str(p12_path),
            "-inkey", str(key_path),
            "-in", str(cert_path),
            "-passout", "pass:catapult",
            label="create-p12",
        )

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

    async def _find_identity(self, keychain: str) -> str:
        """Find the signing identity hash in the keychain."""
        proc = await asyncio.create_subprocess_exec(
            "security", "find-identity", "-v", "-p", "codesigning", keychain,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        # Output lines look like:  1) AABB1122... "iPhone Developer: ..."
        for line in stdout.decode().splitlines():
            line = line.strip()
            if ")" in line and '"' in line:
                parts = line.split(")")[1].strip().split('"')[0].strip()
                if parts:
                    logger.info("Found signing identity: %s", parts[:12] + "...")
                    return parts
        # Fallback to ad-hoc
        logger.warning("No identity found, falling back to ad-hoc signing")
        return "-"

    async def _cleanup_keychain(self, keychain: str):
        # Restore original keychain search list
        if hasattr(self, '_original_keychains') and self._original_keychains:
            await self._run(
                "security", "list-keychains", "-d", "user", "-s", *self._original_keychains,
                label="restore-keychains", check=False,
            )
        await self._run("security", "delete-keychain", keychain, label="delete-keychain", check=False)

    async def _codesign_app(self, app_dir: Path, keychain: str, identity: str, entitlements: Path):
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
                    await self._codesign_path(item, keychain, identity, entitlements)
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
            cmd.append("--preserve-metadata=identifier,entitlements,flags")
        cmd.append(str(path))

        await self._run(*cmd, label=f"codesign:{path.name}")

    def _update_bundle_id(self, app_dir: Path, new_id: str):
        """Rewrite CFBundleIdentifier in Info.plist."""
        plist_path = app_dir / "Info.plist"
        with plist_path.open("rb") as f:
            info = plistlib.load(f)
        old_id = info.get("CFBundleIdentifier", "")
        info["CFBundleIdentifier"] = new_id
        with plist_path.open("wb") as f:
            plistlib.dump(info, f)
        logger.info("Bundle ID: %s → %s", old_id, new_id)

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
