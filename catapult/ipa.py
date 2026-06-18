import plistlib
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import UploadFile

UPLOAD_DIR = Path.home() / ".catapult" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_IPA_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
ZIP_MAGIC = b"PK\x03\x04"


class IpaProcessor:
    async def save_upload(self, file: UploadFile) -> Path:
        # Use random filename to prevent path traversal
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}.ipa"
        size = 0
        with dest.open("wb") as f:
            first_chunk = True
            while chunk := await file.read(1024 * 1024):
                if first_chunk:
                    if not chunk[:4].startswith(ZIP_MAGIC):
                        dest.unlink(missing_ok=True)
                        raise ValueError("File is not a valid IPA (not a ZIP archive)")
                    first_chunk = False
                size += len(chunk)
                if size > MAX_IPA_SIZE:
                    dest.unlink(missing_ok=True)
                    raise ValueError(f"IPA exceeds maximum size ({MAX_IPA_SIZE // (1024**3)} GB)")
                f.write(chunk)

        # Validate IPA structure
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                self._find_app_dir(zf)
        except (zipfile.BadZipFile, ValueError) as e:
            dest.unlink(missing_ok=True)
            raise ValueError(f"Invalid IPA: {e}") from e
        return dest

    async def save_raw_upload(self, stream) -> Path:
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}.ipa"
        size = 0
        first_chunk = True

        with dest.open("wb") as f:
            async for chunk in stream:
                if not chunk:
                    continue
                if first_chunk:
                    if not chunk[:4].startswith(ZIP_MAGIC):
                        dest.unlink(missing_ok=True)
                        raise ValueError("File is not a valid IPA (not a ZIP archive)")
                    first_chunk = False
                size += len(chunk)
                if size > MAX_IPA_SIZE:
                    dest.unlink(missing_ok=True)
                    raise ValueError(f"IPA exceeds maximum size ({MAX_IPA_SIZE // (1024**3)} GB)")
                f.write(chunk)

        if first_chunk:
            dest.unlink(missing_ok=True)
            raise ValueError("IPA upload was empty")

        try:
            with zipfile.ZipFile(dest, "r") as zf:
                self._find_app_dir(zf)
        except (zipfile.BadZipFile, ValueError) as e:
            dest.unlink(missing_ok=True)
            raise ValueError(f"Invalid IPA: {e}") from e
        return dest

    async def inspect(self, ipa_path: str | Path) -> dict:
        ipa_path = Path(ipa_path)
        with zipfile.ZipFile(ipa_path, "r") as zf:
            app_dir = self._find_app_dir(zf)
            plist_path = f"{app_dir}/Info.plist"
            with zf.open(plist_path) as f:
                info = plistlib.load(f)

        return {
            "bundle_id": info.get("CFBundleIdentifier", ""),
            "bundle_name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
            "version": info.get("CFBundleShortVersionString", ""),
            "build": info.get("CFBundleVersion", ""),
            "min_os": info.get("MinimumOSVersion", ""),
            "executable": info.get("CFBundleExecutable", ""),
        }

    async def inspect_extensions(self, ipa_path: str | Path) -> list[dict]:
        ipa_path = Path(ipa_path)
        extensions: list[dict] = []
        with zipfile.ZipFile(ipa_path, "r") as zf:
            app_dir = self._find_app_dir(zf)
            prefix = f"{app_dir}/PlugIns/"
            for name in zf.namelist():
                if not name.startswith(prefix) or not name.endswith(".appex/Info.plist"):
                    continue
                with zf.open(name) as f:
                    info = plistlib.load(f)
                extensions.append({
                    "bundle_id": info.get("CFBundleIdentifier", ""),
                    "bundle_name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
                    "path": name.rsplit("/", 1)[0],
                })
        return extensions

    async def extract(self, ipa_path: Path, work_dir: Path) -> Path:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            zf.extractall(work_dir)
            app_dir_name = self._find_app_dir(zf)

        return work_dir / app_dir_name

    async def repack(self, app_dir: Path, output_path: Path):
        # app_dir = work_dir/Payload/App.app
        # paths must be Payload/App.app/... in the zip
        root_dir = app_dir.parent.parent
        payload_dir = app_dir.parent
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in payload_dir.rglob("*"):
                if file.is_file():
                    arcname = file.relative_to(root_dir)
                    zf.write(file, arcname)

    def _find_app_dir(self, zf: zipfile.ZipFile) -> str:
        for name in zf.namelist():
            parts = name.split("/")
            if len(parts) >= 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
                return f"Payload/{parts[1]}"
        raise ValueError("No .app bundle found in IPA")
