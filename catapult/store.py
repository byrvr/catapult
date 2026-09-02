"""App store: sources, catalog, and update checking.

Two source kinds normalise into one catalog:

  github    poll GET /repos/{owner}/{repo}/releases and read .ipa assets
  altstore  fetch an apps.json in the AltStore/SideStore source format

Catapult ships no default catalog. The user adds sources, which keeps this a
tool rather than a distribution channel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import PurePosixPath
from urllib.parse import unquote
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Catapult"
SOURCES_PATH = APP_SUPPORT_DIR / "sources.json"

GITHUB_API = "https://api.github.com"
# Unauthenticated GitHub allows 60 requests/hour per IP. A daily check per
# source is nowhere near that.
REQUEST_TIMEOUT = 30

# Filename tokens. tvOS is checked first: a name can carry both, and the more
# specific platform should win.
_TVOS_TOKENS = ("tvos", "appletv", "apple-tv")
_IOS_TOKENS = ("ios", "iphone", "ipad", "universal")

_PRERELEASE_TOKENS = ("alpha", "beta", "rc", "dev", "nightly", "preview", "snapshot")

# A version-looking token, so the variant scan knows where the name ends.
_VERSION_TOKEN = re.compile(r"^v?\d+(\.\d+)*")


# ── classification ──────────────────────────────────────────────────────────

def _tokens(filename: str) -> list[str]:
    stem = Path(filename).stem
    return [t for t in re.split(r"[-_. ]+", stem) if t]


def platform_for_asset(filename: str) -> str:
    """Best-effort platform from a release asset filename.

    A hint only — the IPA's own UIDeviceFamily is authoritative once downloaded.
    """
    lowered = filename.lower()
    if any(token in lowered for token in _TVOS_TOKENS):
        return "tvos"
    if any(token in lowered for token in _IOS_TOKENS):
        return "ios"
    return "unknown"


def variant_for_asset(filename: str) -> str:
    """Build variant, e.g. "lite" in VortX-tvOS-lite-v0.3.14-beta.12-ci.ipa.

    Anything between the platform token and the version token. A variant is a
    different app with a different bundle ID, so it gets its own catalog entry
    rather than being hidden behind the main one.
    """
    tokens = _tokens(filename)
    platform_index = None
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in _TVOS_TOKENS or lowered in _IOS_TOKENS:
            platform_index = index
            break
    if platform_index is None:
        return ""

    parts: list[str] = []
    for token in tokens[platform_index + 1:]:
        if _VERSION_TOKEN.match(token):
            break
        parts.append(token)
    return "-".join(parts).lower()


def _name_tokens(filename: str) -> list[str]:
    """Split a filename on separators, keeping dots so versions stay intact."""
    import re as _re
    stem = Path(filename).stem
    return [tok for tok in _re.split(r"[-_ ]+", stem) if tok]


def app_name_for_asset(filename: str) -> str:
    """The app's name, taken from the leading tokens of its filename.

    Needed because a repo can publish several DIFFERENT apps, each in its own
    release. github.com/mrdrvt99/YouProEXTRA ships six, so keying the catalog
    on platform alone collapsed them into one entry.
    """
    parts: list[str] = []
    for token in _name_tokens(filename):
        lowered = token.lower()
        # The name ends at the first version or platform token — everything
        # after a platform token is variant or version, not part of the name.
        if _VERSION_TOKEN.match(token) or lowered in _TVOS_TOKENS or lowered in _IOS_TOKENS:
            break
        parts.append(token)
    return " ".join(parts) or Path(filename).stem


def version_for_asset(filename: str, tag: str) -> str:
    """Version for an asset, preferring the release tag when it carries one.

    Some repos use a fixed tag per app (ytl-ipa3) and put the version only in
    the filename. Comparing tags there would never detect a new build.
    """
    if any(_VERSION_TOKEN.match(tok) for tok in _name_tokens(tag)):
        return tag
    versions = [tok for tok in _name_tokens(filename) if _VERSION_TOKEN.match(tok)]
    return "-".join(versions) if versions else Path(filename).stem


def select_ipa_assets(filenames: list[str]) -> list[str]:
    return [name for name in filenames if name.lower().endswith(".ipa")]


def find_checksums_asset(filenames: list[str]) -> str | None:
    for name in filenames:
        if name.upper().startswith("SHA256SUMS"):
            return name
    return None


def platform_for_device_family(device_family: list[int]) -> str:
    """Resolve platform from an IPA's UIDeviceFamily. 1=iPhone 2=iPad 3=tvOS."""
    families = set(device_family or [])
    if 3 in families:
        return "tvos"
    if families & {1, 2}:
        return "ios"
    return "unknown"


def platform_fits_device(platform: str, device_class: str) -> bool:
    """Whether a catalog entry should be offered for a device.

    "unknown" is offered rather than hidden: the filename carried no hint, and
    the IPA will be checked properly after download.
    """
    if platform == "unknown":
        return True
    if device_class == "tvos":
        return platform == "tvos"
    if device_class in ("ios", "ipados"):
        return platform == "ios"
    return False


# ── versions ────────────────────────────────────────────────────────────────

def is_prerelease_tag(tag: str) -> bool:
    """Whether a tag looks like a pre-release.

    GitHub's own `prerelease` flag is not trusted: VortX publishes
    v0.3.14-beta.12 with prerelease=false.
    """
    lowered = tag.lower()
    return any(token in lowered for token in _PRERELEASE_TOKENS)


def version_key(tag: str) -> tuple:
    """A natural-order sort key for a version tag.

    Lexical comparison gets beta.9 > beta.12 backwards, so numeric runs are
    compared as integers. A trailing alphabetic run marks a pre-release of the
    version formed by the numeric prefix, so 0.3.14 outranks 0.3.14-beta.12.
    """
    cleaned = tag.strip().lstrip("vV")
    runs = re.findall(r"\d+|[A-Za-z]+", cleaned)

    numeric: list[int] = []
    suffix: list[tuple[int, object]] = []
    seen_alpha = False
    for run in runs:
        if run.isdigit():
            if seen_alpha:
                suffix.append((1, int(run)))
            else:
                numeric.append(int(run))
        else:
            seen_alpha = True
            suffix.append((0, run.lower()))

    # No suffix means a final release, which must outrank any pre-release of the
    # same numeric version.
    return (tuple(numeric), 1 if not suffix else 0, tuple(suffix))


def app_version_prefix(version: str) -> str:
    """The app's own version out of a tag or filename version.

    "21.24.3-5.2.2" is YouTube 21.24.3 with tweak build 5.2.2; "v0.3.14-beta.12"
    is 0.3.14. The numeric prefix is what an installed IPA's Info.plist reports.
    """
    match = re.match(r"^v?(\d+(?:\.\d+)*)", (version or "").strip())
    return match.group(1) if match else ""


def _version_tuple(prefix: str) -> tuple[int, ...]:
    """Numeric version with trailing zeros dropped, so 2.0 equals v2.0.0."""
    parts = [int(piece) for piece in prefix.split(".")] if prefix else []
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


_HASH_STEM = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$", re.I)


def _asset_name(filename: str) -> str:
    """The app name a filename carries, or "" for hash-named vault copies."""
    stem = PurePosixPath(unquote(filename or "")).stem
    if not stem or _HASH_STEM.match(stem):
        return ""
    name = app_name_for_asset(filename)
    return "" if _HASH_STEM.match(name) else name.casefold()


# Two builds of the same tweak differ by a few hundred KB; different tweaks
# of the same app version differ by more than a percent.
INSTALL_SIZE_TOLERANCE = 0.01


def match_strength(app: StoreApp, record: dict) -> str | None:
    """How surely an install record is this catalog entry.

    "exact": the record was installed from this entry (store link) or is the
    published asset byte for byte (digest). "likely": the record was installed
    by hand and matches by the name in its original filename, by bundle id, or
    — for GitHub assets that carry no bundle id — by the app version baked into
    the tag together with the asset size. Every YouTube tweak shares
    com.google.ios.youtube, so version and size are what tell them apart, and
    a filename such as YouTubePlus_21.20.1_5.1.0.ipa keeps naming its tweak
    after the source has moved on to a newer version.
    """
    key = record.get("store_app_key") or ""
    if app.app_key and key == app.app_key:
        return "exact"
    if app.sha256 and record.get("ipa_sha256") == app.sha256:
        return "exact"
    if key and app.source_id and key.startswith(app.source_id + "#"):
        return None  # a different entry of this very source

    entry_name = _asset_name(app.download_url.rsplit("/", 1)[-1])
    record_name = _asset_name(record.get("original_filename") or "")
    if entry_name and record_name:
        return "likely" if entry_name == record_name else None

    version = _version_tuple(app_version_prefix(app.version))
    record_version = _version_tuple(app_version_prefix(record.get("app_version") or ""))
    same_version = bool(version and record_version and version == record_version)
    record_size = int(record.get("ipa_size") or 0)
    size_known = bool(app.size and record_size)
    close_size = size_known and abs(record_size - app.size) / app.size <= INSTALL_SIZE_TOLERANCE

    if app.bundle_id and record.get("source_bundle_id") == app.bundle_id:
        if version and record_version and not same_version:
            return None
        if size_known and not close_size:
            return None
        return "likely"
    return "likely" if (same_version and close_size) else None


def matches_install_record(app: StoreApp, record: dict) -> bool:
    """Whether an install record is, very likely, this catalog entry."""
    return match_strength(app, record) is not None


def is_newer(candidate: str, installed: str | None) -> bool:
    if not installed:
        return True
    return version_key(candidate) > version_key(installed)


# ── catalog ─────────────────────────────────────────────────────────────────

@dataclass
class StoreApp:
    source_id: str
    app_key: str
    name: str
    version: str
    platform: str
    download_url: str
    variant: str = ""
    developer: str = ""
    bundle_id: str = ""
    icon_url: str = ""
    changelog: str = ""
    size: int = 0
    sha256: str = ""
    version_date: str = ""
    prerelease: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Source:
    id: str
    kind: str
    url: str
    # Defaults to True because most projects shipping IPAs from CI tag every
    # build as a pre-release. VortX, for instance, has no non-beta tag at all,
    # so defaulting this off means adding the source and seeing an empty list.
    # Entries carry prerelease=True so the UI can badge them.
    include_prerelease: bool = True
    last_checked_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_source(raw_url: str) -> Source:
    """Turn user input into a Source, guessing the kind."""
    value = raw_url.strip()
    if not value:
        raise ValueError("Enter a GitHub repository or a source URL")

    # Accept the repository itself, a bare owner/repo, and the releases or tags
    # pages people naturally copy out of the address bar.
    match = re.match(
        r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?"
        r"(?:/(?:releases|tags)(?:/.*)?)?/?$",
        value,
    )
    if match and "/" in value:
        owner, repo = match.group(1), match.group(2)
        return Source(
            id=f"github:{owner}/{repo}",
            kind="github",
            url=f"https://github.com/{owner}/{repo}",
        )

    if value.startswith("http"):
        return Source(id=f"altstore:{value}", kind="altstore", url=value)

    raise ValueError(f"Could not understand source {raw_url!r}")


def load_sources() -> list[Source]:
    try:
        data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("Sources file at %s is unreadable", SOURCES_PATH)
        return []
    return [Source(**item) for item in data.get("sources", []) if item.get("id")]


def save_sources(sources: list[Source]) -> None:
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_PATH.write_text(
        json.dumps({"sources": [s.to_dict() for s in sources]}, indent=2),
        encoding="utf-8",
    )


# ── adapters ────────────────────────────────────────────────────────────────

def catalog_from_github_releases(source: Source, releases: list[dict]) -> list[StoreApp]:
    """Newest matching build per (platform, variant)."""
    repo = source.id.removeprefix("github:")
    base_name = repo.split("/")[-1]
    best: dict[tuple[str, str], StoreApp] = {}

    for release in releases:
        tag = release.get("tag_name") or release.get("name") or ""
        if not tag:
            continue
        prerelease = is_prerelease_tag(tag) or bool(release.get("prerelease"))
        if prerelease and not source.include_prerelease:
            continue

        assets = {a.get("name", ""): a for a in release.get("assets", []) if a.get("name")}
        for filename in select_ipa_assets(list(assets)):
            asset = assets[filename]
            platform = platform_for_asset(filename)
            variant = variant_for_asset(filename)
            app_name = app_name_for_asset(filename)
            version = version_for_asset(filename, tag)
            key = (app_name, platform, variant)

            existing = best.get(key)
            if existing and not is_newer(version, existing.version):
                continue

            label = app_name or base_name
            if platform != "unknown":
                label += f" ({'Apple TV' if platform == 'tvos' else 'iOS'}"
                label += f" {variant})" if variant else ")"
            elif variant:
                label += f" ({variant})"

            best[key] = StoreApp(
                source_id=source.id,
                app_key=f"{source.id}#{app_name}:{platform}:{variant}",
                name=label,
                version=version,
                platform=platform,
                variant=variant,
                developer=repo.split("/")[0],
                download_url=asset.get("browser_download_url", ""),
                size=int(asset.get("size") or 0),
                changelog=(release.get("body") or "")[:4000],
                version_date=release.get("published_at") or "",
                prerelease=prerelease,
            )

    return sorted(best.values(), key=lambda a: a.name)


def catalog_from_altstore(source: Source, document: dict) -> list[StoreApp]:
    """Parse an AltStore/SideStore apps.json."""
    developer = document.get("name") or document.get("identifier") or ""
    apps: list[StoreApp] = []

    for app in document.get("apps", []) or []:
        bundle_id = app.get("bundleIdentifier") or ""
        versions = app.get("versions") or []
        if versions:
            newest = versions[0]
            version = newest.get("version") or ""
            download_url = newest.get("downloadURL") or ""
            size = int(newest.get("size") or 0)
            version_date = newest.get("date") or ""
            changelog = newest.get("localizedDescription") or ""
            sha256 = str(newest.get("sha256") or "").lower()
        else:
            version = app.get("version") or ""
            download_url = app.get("downloadURL") or ""
            size = int(app.get("size") or 0)
            version_date = app.get("versionDate") or ""
            changelog = app.get("versionDescription") or ""
            sha256 = str(app.get("sha256") or "").lower()

        if not download_url:
            continue

        # AltStore sources do not carry a platform; almost all are iOS. The
        # IPA's UIDeviceFamily settles it after download.
        apps.append(StoreApp(
            source_id=source.id,
            app_key=f"{source.id}#{bundle_id or app.get('name', '')}",
            name=app.get("name") or bundle_id,
            version=version,
            platform="unknown",
            developer=app.get("developerName") or developer,
            bundle_id=bundle_id,
            download_url=download_url,
            icon_url=app.get("iconURL") or "",
            changelog=changelog[:4000],
            size=size,
            sha256=sha256,
            version_date=version_date,
            prerelease=is_prerelease_tag(version),
        ))

    return sorted(apps, key=lambda a: a.name)


async def fetch_catalog(source: Source) -> list[StoreApp]:
    """Fetch and normalise one source. Network errors propagate to the caller."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        if source.kind == "github":
            repo = source.id.removeprefix("github:")
            response = await client.get(
                f"{GITHUB_API}/repos/{repo}/releases",
                params={"per_page": 10},
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            releases = response.json()
            apps = catalog_from_github_releases(source, releases)
            digests: dict[str, str] = {}
            for url in checksum_urls_for(releases, apps):
                try:
                    sums = await client.get(url)
                    sums.raise_for_status()
                    digests.update(parse_checksums(sums.text))
                except Exception as e:
                    logger.info("Could not read checksums at %s: %s", url, e)
            apply_checksums(apps, digests)
            return apps

        response = await client.get(source.url)
        response.raise_for_status()
        return catalog_from_altstore(source, response.json())


# ── downloading ─────────────────────────────────────────────────────────────

_BSD_SUM = re.compile(r"^SHA256\s*\((.+)\)\s*=\s*([0-9a-fA-F]{64})$")


def parse_checksums(text: str) -> dict[str, str]:
    """Parse a SHA256SUMS file into {basename: digest}.

    Accepts GNU lines (``<hash>  path``, optional ``*`` binary marker) and BSD
    lines (``SHA256 (path) = <hash>``). Keys are basenames because CI-generated
    files list ``./dist/App.ipa`` while the release asset is just ``App.ipa``.
    """
    digests: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bsd = _BSD_SUM.match(line)
        if bsd:
            name, digest = bsd.group(1), bsd.group(2)
        else:
            parts = line.split()
            if len(parts) < 2 or len(parts[0]) != 64:
                continue
            digest, name = parts[0], parts[-1].lstrip("*")
        digests[PurePosixPath(name.strip()).name] = digest.lower()
    return digests


def checksum_urls_for(releases: list[dict], apps: list[StoreApp]) -> list[str]:
    """SHA256SUMS download URLs for the releases that produced ``apps``.

    One request per release actually shown, not per release fetched:
    unauthenticated GitHub allows 60 requests an hour.
    """
    wanted = {app.download_url for app in apps if app.download_url}
    urls: list[str] = []
    for release in releases:
        assets = [a for a in release.get("assets", []) if a.get("name")]
        if not any(a.get("browser_download_url") in wanted for a in assets):
            continue
        name = find_checksums_asset([a["name"] for a in assets])
        if not name:
            continue
        url = next((a.get("browser_download_url", "") for a in assets if a["name"] == name), "")
        if url and url not in urls:
            urls.append(url)
    return urls


def apply_checksums(apps: list[StoreApp], digests: dict[str, str]) -> None:
    """Fill ``sha256`` on apps whose asset filename appears in a SHA256SUMS map."""
    for app in apps:
        filename = unquote(app.download_url.rsplit("/", 1)[-1])
        digest = digests.get(filename)
        if digest:
            app.sha256 = digest.lower()


async def download_to(url: str, dest: Path, *, expected_sha256: str = "") -> Path:
    """Stream a download to disk, verifying the digest when one is known."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    # A unique temp name: a user-initiated install and the daily check can
    # fetch the same URL at once, and two writers on one .part file interleave.
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        # No total timeout (IPAs are large and links can be slow), but a stalled
        # connection or a silent server must not hang the install forever.
        timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with tmp.open("wb") as f:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        digest.update(chunk)
                        f.write(chunk)

        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            raise ValueError(
                f"Downloaded file does not match its published checksum "
                f"(expected {expected_sha256[:12]}…, got {actual[:12]}…)"
            )
        tmp.replace(dest)
        return dest
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
