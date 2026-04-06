import asyncio
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from catapult.ipa import IpaProcessor


class Signer:
    def __init__(self):
        self._ipa = IpaProcessor()

    async def sign(
        self,
        ipa_path: str | Path,
        cert_bytes: bytes,
        private_key: rsa.RSAPrivateKey,
        profile_bytes: bytes,
    ) -> Path:
        ipa_path = Path(ipa_path)
        work_dir = Path(tempfile.mkdtemp(prefix="catapult_sign_"))

        try:
            app_dir = await self._ipa.extract(ipa_path, work_dir)

            # Write provisioning profile
            (app_dir / "embedded.mobileprovision").write_bytes(profile_bytes)

            # Write cert and key to temp files for codesign
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

            try:
                await self._codesign(app_dir, keychain)
            finally:
                await self._cleanup_keychain(keychain)

            signed_ipa = work_dir / f"{ipa_path.stem}_signed.ipa"
            await self._ipa.repack(app_dir, signed_ipa)
            return signed_ipa
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    async def _create_p12(self, cert_path: Path, key_path: Path, p12_path: Path):
        proc = await asyncio.create_subprocess_exec(
            "openssl", "pkcs12", "-export",
            "-out", str(p12_path),
            "-inkey", str(key_path),
            "-in", str(cert_path),
            "-passout", "pass:",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create p12: {stderr.decode()}")

    async def _setup_keychain(self, work_dir: Path, p12_path: Path) -> str:
        keychain = str(work_dir / "catapult.keychain-db")
        password = "catapult-tmp"

        cmds = [
            ["security", "create-keychain", "-p", password, keychain],
            ["security", "set-keychain-settings", keychain],
            ["security", "unlock-keychain", "-p", password, keychain],
            ["security", "import", str(p12_path), "-k", keychain, "-P", "", "-T", "/usr/bin/codesign"],
            ["security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", password, keychain],
        ]
        for cmd in cmds:
            proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()

        return keychain

    async def _cleanup_keychain(self, keychain: str):
        proc = await asyncio.create_subprocess_exec(
            "security", "delete-keychain", keychain,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def _codesign(self, app_dir: Path, keychain: str):
        # Sign frameworks and dylibs first
        frameworks = app_dir / "Frameworks"
        if frameworks.exists():
            for item in frameworks.iterdir():
                await self._codesign_path(item, keychain)

        # Sign plugins
        plugins = app_dir / "PlugIns"
        if plugins.exists():
            for item in plugins.iterdir():
                await self._codesign_path(item, keychain)

        # Sign the main app bundle
        await self._codesign_path(app_dir, keychain)

    async def _codesign_path(self, path: Path, keychain: str):
        proc = await asyncio.create_subprocess_exec(
            "codesign",
            "--force", "--sign", "-",
            "--keychain", keychain,
            "--preserve-metadata=identifier,entitlements,flags",
            "--generate-entitlement-der",
            str(path),
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"codesign failed for {path.name}: {stderr.decode()}")
