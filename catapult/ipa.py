import plistlib
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import UploadFile

UPLOAD_DIR = Path(tempfile.gettempdir()) / "catapult_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


class IpaProcessor:
    async def save_upload(self, file: UploadFile) -> Path:
        dest = UPLOAD_DIR / file.filename
        with dest.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
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
