"""Persistent install records and opportunistic background refresh scheduler."""

import asyncio
import json
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from catapult.errors import normalize_error
from catapult import power, provisioning, vault

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".catapult"
STATE_FILE = STATE_DIR / "state.json"
_KEYCHAIN_SERVICE = "com.catapult.session"

REFRESH_INTERVAL_DAYS = 7
REFRESH_VALID_SECONDS = REFRESH_INTERVAL_DAYS * 86400
REFRESH_WINDOW_HOURS = 72
REFRESH_WINDOW_SECONDS = REFRESH_WINDOW_HOURS * 3600
# Re-sign opportunistically once an app has 72h or less before expiry.
REFRESH_AFTER_SECONDS = max(0, REFRESH_VALID_SECONDS - REFRESH_WINDOW_SECONDS)

# A failing refresh backs off instead of retiring the record. The old behaviour
# (retire after 3 consecutive failures) meant one weekend away from the device
# silently ended auto-refresh for good.
RETRY_BASE_SECONDS = 15 * 60
RETRY_MAX_SECONDS = 12 * 3600

# Wait in slices rather than one long sleep. asyncio's clock is time.monotonic(),
# which on macOS is mach_absolute_time() and does NOT advance while the system is
# asleep, so a single long sleep silently loses all suspended time.
SLEEP_SLICE_SECONDS = 60


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _expiry_ts(last_installed: float | int | None) -> float | None:
    if not last_installed:
        return None
    return float(last_installed) + REFRESH_VALID_SECONDS


def _refresh_after_ts(last_installed: float | int | None) -> float | None:
    if not last_installed:
        return None
    return float(last_installed) + REFRESH_AFTER_SECONDS


def retry_delay_seconds(fail_count: int) -> float:
    """Exponential backoff for a record whose refresh keeps failing."""
    if fail_count <= 0:
        return 0.0
    return float(min(RETRY_BASE_SECONDS * (2 ** (fail_count - 1)), RETRY_MAX_SECONDS))


async def sleep_until(deadline_ts: float, *, now=None, sleep=None) -> None:
    """Sleep until a wall-clock deadline, re-checking the real clock as we go.

    A machine that suspends past the deadline must run on its next wake rather
    than waiting out a duration measured on a clock that was frozen.
    """
    now = now or _now_ts
    sleep = sleep or asyncio.sleep
    while True:
        remaining = deadline_ts - now()
        if remaining <= 0:
            return
        await sleep(min(remaining, SLEEP_SLICE_SECONDS))


def seconds_until_expiry(rec: dict, now: float | None = None) -> float | None:
    """Seconds until the profile expires.

    Prefers the ExpirationDate recorded from the provisioning profile; falls
    back to install time + 7 days for records written before that was stored.
    """
    expires_at = rec.get("expires_at") or _expiry_ts(rec.get("last_installed"))
    if expires_at is None:
        return None
    return float(expires_at) - (now if now is not None else _now_ts())


def seconds_until_refresh(rec: dict, now: float | None = None) -> float | None:
    refresh_after = rec.get("refresh_after") or _refresh_after_ts(rec.get("last_installed"))
    if refresh_after is None:
        return None
    return float(refresh_after) - (now if now is not None else _now_ts())


def _stamp_refresh_schedule(
    rec: dict, installed_at: float, expires_at: float | None = None
) -> None:
    rec["last_installed"] = installed_at
    resolved_expiry = expires_at if expires_at else _expiry_ts(installed_at)
    rec["expires_at"] = resolved_expiry
    rec["refresh_after"] = (
        resolved_expiry - REFRESH_WINDOW_SECONDS if resolved_expiry else None
    )
    rec["refresh_window_hours"] = REFRESH_WINDOW_HOURS


def load_state() -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"session": None, "installs": []}


def save_state(state: dict):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_install(
    device_udid: str,
    ipa_path: str,
    device_name: str = "",
    bundle_id: str = "",
    source_bundle_id: str = "",
    app_name: str = "",
    ipa_sha256: str = "",
    ipa_size: int | None = None,
    original_filename: str = "",
    expires_at: float | None = None,
):
    state = load_state()
    installs = state.get("installs", [])
    installed_at = _now_ts()
    if ipa_path:
        try:
            vaulted = vault.store_ipa(ipa_path, original_filename=original_filename)
            ipa_path = vaulted["path"]
            ipa_sha256 = ipa_sha256 or vaulted["sha256"]
            ipa_size = ipa_size or vaulted["size"]
            original_filename = original_filename or vaulted["original_filename"]
        except Exception:
            logger.debug("Could not import IPA into durable vault: %s", ipa_path, exc_info=True)

    # Update existing or add new. A user can choose the same IPA from a new path
    # later; keep one refresh record per app/device so the durable path replaces
    # older temporary upload paths.
    for rec in installs:
        same_device = rec.get("device_udid") == device_udid
        same_file = same_device and rec.get("ipa_path") == ipa_path
        same_hash = same_device and ipa_sha256 and rec.get("ipa_sha256") == ipa_sha256
        same_signed_bundle = same_device and bundle_id and rec.get("bundle_id") == bundle_id
        same_source_bundle = same_device and source_bundle_id and rec.get("source_bundle_id") == source_bundle_id
        if same_file or same_hash or same_signed_bundle or same_source_bundle:
            rec["ipa_path"] = ipa_path
            _stamp_refresh_schedule(rec, installed_at, expires_at)
            rec["device_name"] = device_name or rec.get("device_name", "")
            if bundle_id:
                rec["bundle_id"] = bundle_id
            if source_bundle_id:
                rec["source_bundle_id"] = source_bundle_id
            if app_name:
                rec["app_name"] = app_name
            if ipa_sha256:
                rec["ipa_sha256"] = ipa_sha256
            if ipa_size is not None:
                rec["ipa_size"] = ipa_size
            if original_filename:
                rec["original_filename"] = original_filename
            rec["fail_count"] = 0
            rec.pop("next_attempt_at", None)
            save_state(state)
            return
    installs.append({
        "device_udid": device_udid,
        "ipa_path": ipa_path,
        "device_name": device_name,
        "bundle_id": bundle_id,
        "source_bundle_id": source_bundle_id,
        "app_name": app_name,
        "ipa_sha256": ipa_sha256,
        "ipa_size": ipa_size,
        "original_filename": original_filename,
    })
    _stamp_refresh_schedule(installs[-1], installed_at, expires_at)
    state["installs"] = installs
    save_state(state)
    logger.info("Recorded install: %s → %s", ipa_path, device_udid)


def find_recorded_bundle_id(device_udid: str, candidate_bundle_ids: list[str]) -> str | None:
    """Return a prior installed bundle ID for this device/app, if known."""
    candidates = {value for value in candidate_bundle_ids if value}
    if not candidates:
        return None
    state = load_state()
    for rec in state.get("installs", []):
        if rec.get("device_udid") != device_udid:
            continue
        bundle_id = rec.get("bundle_id") or ""
        if bundle_id in candidates:
            return bundle_id
    return None


def choose_target_bundle_id(
    *,
    original_bundle_id: str,
    legacy_bundle_id: str,
    installed_bundle_id: str | None,
    recorded_bundle_id: str | None,
) -> str:
    """Pick the bundle ID to sign and install under.

    Prefer updating an installed copy in place. But a copy that carries the
    app's real bundle ID, which Catapult never recorded installing under that
    ID, is the App Store build: installd refuses to replace it with a
    development-signed build (IXErrorDomain 46, "a coordinated app install
    already exists ... (creator App Store)"). Fall through to the recorded or
    namespaced ID and install alongside it — on every install, not only the
    first one, because the device lookup finds the App Store copy first even
    when Catapult's own copy is also installed.
    """
    if installed_bundle_id == original_bundle_id and recorded_bundle_id != original_bundle_id:
        installed_bundle_id = None
    return installed_bundle_id or recorded_bundle_id or legacy_bundle_id


def _keychain_set(account: str, data: str) -> bool:
    """Store a value in macOS Keychain.

    The command is fed to ``security -i`` over stdin with the value hex-encoded
    (``-X``), so the secret never sits in the process table the way a
    ``-w <secret>`` argument does. ``-U`` updates an existing item in place;
    ``security -i`` exits non-zero when the inner command fails.
    """
    script = (
        f"add-generic-password -s {shlex.quote(_KEYCHAIN_SERVICE)} "
        f"-a {shlex.quote(account)} -X {data.encode('utf-8').hex()} -U\n"
    )
    result = subprocess.run(
        ["security", "-i"], input=script, capture_output=True, text=True,
    )
    return result.returncode == 0


def _keychain_get(account: str) -> str | None:
    """Retrieve a value from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _keychain_delete(account: str):
    """Remove a value from macOS Keychain."""
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
    )


def save_session(session) -> None:
    """Persist auth session — tokens go to Keychain, metadata to state file."""
    tokens = json.dumps({
        "adsid": session.adsid,
        "dsprsid": session.dsprsid,
        "idms_token": session.idms_token,
        "gs_token": session.gs_token,
        "sk": session.sk.hex() if session.sk else "",
        "c": session.c.hex() if session.c else "",
    })
    if _keychain_set(session.apple_id, tokens):
        logger.info("Session tokens stored in Keychain")
    else:
        logger.warning("Failed to store tokens in Keychain, falling back to file")

    state = load_state()
    state["session"] = {
        "apple_id": session.apple_id,
        "authenticated": session.authenticated,
    }
    save_state(state)


def restore_session(auth_client) -> bool:
    """Restore saved session — read tokens from Keychain."""
    state = load_state()
    saved = state.get("session")
    if not saved or not saved.get("authenticated"):
        return False

    apple_id = saved.get("apple_id", "")
    raw = _keychain_get(apple_id)
    if not raw:
        logger.warning("No Keychain entry for %s", apple_id)
        return False

    try:
        tokens = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupt Keychain data for %s", apple_id)
        return False

    from catapult.apple_auth import AuthSession
    s = AuthSession(
        apple_id=apple_id,
        adsid=tokens.get("adsid", ""),
        dsprsid=tokens.get("dsprsid", ""),
        idms_token=tokens.get("idms_token", ""),
        gs_token=tokens.get("gs_token", ""),
        sk=bytes.fromhex(tokens.get("sk", "")) if tokens.get("sk") else b"",
        c=bytes.fromhex(tokens.get("c", "")) if tokens.get("c") else b"",
        authenticated=True,
    )
    auth_client.session = s
    logger.info("Restored session for %s (from Keychain)", s.apple_id)
    return True


def _rewrite_nested_bundle_id(parent_old_id: str, parent_new_id: str, nested_old_id: str) -> str:
    if nested_old_id == parent_old_id:
        return parent_new_id
    if nested_old_id.startswith(parent_old_id + "."):
        return parent_new_id + nested_old_id[len(parent_old_id):]
    return f"{parent_new_id}.{nested_old_id.rsplit('.', 1)[-1]}"


def due_installs(state: dict, now: float | None = None) -> list[dict]:
    """Return installs inside the refresh window and past any backoff window."""
    now = now if now is not None else _now_ts()

    def _is_due(rec: dict) -> bool:
        remaining = seconds_until_expiry(rec, now)
        if remaining is not None and remaining > REFRESH_WINDOW_SECONDS:
            return False
        next_attempt = rec.get("next_attempt_at")
        return not next_attempt or float(next_attempt) <= now

    return [r for r in state.get("installs", []) if _is_due(r)]


async def run_refresh_loop(get_server_components):
    """Background loop — refreshes installs once they enter the 72h expiry window."""
    CHECK_INTERVAL = 3600  # check every hour
    await asyncio.sleep(30)  # let server settle on startup

    while True:
        # Compute the next deadline against the wall clock BEFORE doing the work,
        # so a slow refresh cycle does not push the following check out.
        next_check_at = _now_ts() + CHECK_INTERVAL
        try:
            state = load_state()
            pending = due_installs(state)
            if pending:
                logger.info("Auto-refresh: %d install(s) in the %dh refresh window", len(pending), REFRESH_WINDOW_HOURS)
                # Hold off idle sleep for the cycle. A refresh suspended
                # mid-signing leaves a half-written IPA and a failed install.
                with power.prevent_idle_sleep(
                    f"Catapult is refreshing {len(pending)} app(s)"
                ):
                    await _run_refresh_cycle(pending, get_server_components)
            if store_check_is_due(state):
                # Daily, per the store design: check every source and install
                # opted-in updates wherever the device is reachable.
                with power.prevent_idle_sleep("Catapult is checking store sources for updates"):
                    summary = await run_store_update_check(get_server_components)
                if summary.get("status") == "ok":
                    logger.info(
                        "Store check: %d source(s), %d updated, %d unreachable, %d failed",
                        summary["sources"], len(summary["updated"]),
                        len(summary["unreachable"]), len(summary["failed"]),
                    )
        except Exception:
            logger.exception("Auto-refresh error")

        await sleep_until(next_check_at)


async def _refresh_lease_context(dev_services, session):
    """(store, team_id, machine_id) for the cycle lease, or None when sync is off."""
    from catapult import sync as _sync  # sync imports this module; keep it local

    store = _sync._store_from_config(_sync.SyncConfig.load())
    if store is None:
        return None
    team = await dev_services.get_team(session)
    return store, team["teamId"], _sync.machine_id()


async def _run_refresh_cycle(pending: list[dict], get_server_components) -> None:
    """Refresh every due record once. Errors are handled per record.

    When sync is configured, the cycle runs under the vault's refresh lease so
    two Macs sharing a vault do not sign and install the same apps at once.
    """
    components = tuple(get_server_components())
    device_manager, auth_client, dev_services, signer, ipa_processor = components[:5]
    activity_manager = components[5] if len(components) > 5 else None
    session = auth_client.session

    if not session or not session.authenticated:
        logger.warning("Auto-refresh skipped — not authenticated")
        if activity_manager:
            job = activity_manager.start(
                "refresh",
                "Auto-refresh skipped",
                target=f"{len(pending)} install(s)",
                message="Not authenticated.",
            )
            normalized = normalize_error("Not authenticated. Please sign in first.")
            activity_manager.fail(
                job,
                normalized.message,
                category=normalized.category,
                detail=normalized.detail,
            )
        return

    lease = None
    try:
        lease = await _refresh_lease_context(dev_services, session)
        if lease is not None:
            from catapult import sync as _sync

            store, team_id, machine = lease
            if not await _sync.acquire_refresh_lease(store, team_id, machine_id=machine):
                logger.info("Auto-refresh skipped — another Mac holds the refresh lease")
                if activity_manager:
                    job = activity_manager.start(
                        "refresh",
                        "Auto-refresh skipped",
                        target=f"{len(pending)} install(s)",
                        message="Another Mac is refreshing this vault right now.",
                    )
                    activity_manager.complete(
                        job, message="Another Mac is refreshing this vault right now; skipped."
                    )
                return
    except Exception:
        logger.debug("Refresh lease unavailable; continuing without it", exc_info=True)
        lease = None

    try:
        for rec in pending:
            job = None
            if activity_manager:
                job = activity_manager.start(
                    "refresh",
                    f"Refresh {rec.get('app_name') or Path(rec.get('ipa_path', '')).name or 'app'}",
                    target=rec.get("device_name") or rec.get("device_udid", ""),
                    message="Refreshing saved install...",
                )
            result = await _refresh_install(rec, device_manager, auth_client,
                                            dev_services, signer, ipa_processor)
            if activity_manager and job:
                if result.get("status") == "ok":
                    activity_manager.complete(
                        job,
                        message=result.get("message") or "Auto-refresh complete.",
                    )
                else:
                    normalized = normalize_error(result.get("message") or "Auto-refresh failed.")
                    activity_manager.fail(
                        job,
                        normalized.message,
                        category=normalized.category,
                        detail=normalized.detail,
                    )
    finally:
        if lease is not None:
            from catapult import sync as _sync

            store, team_id, machine = lease
            try:
                await _sync.release_refresh_lease(store, team_id, machine_id=machine)
            except Exception:
                logger.debug("Could not release the refresh lease", exc_info=True)


async def _refresh_install(rec, device_manager, auth_client, dev_services, signer, ipa_processor):
    """Re-sign and re-install a single recorded app."""
    device_udid = rec["device_udid"]
    ipa_path = rec["ipa_path"]
    logger.info("Refreshing %s on %s", ipa_path, device_udid)
    try:
        ipa_file = vault.resolve_ipa_path(rec)
        if ipa_file is None:
            missing = ipa_path or rec.get("ipa_sha256", "")
            raise RuntimeError(f"IPA file is missing: {missing}. Choose the IPA again before refreshing.")

        session = auth_client.session
        team = await dev_services.get_team(session)
        team_id = team["teamId"]
        from catapult.developer import team_is_free
        cert, private_key = await dev_services.get_or_create_cert(
            session, team_id, personal_team=team_is_free(team)
        )

        device_info = await device_manager.get_device_info(device_udid)
        if "remotepairing" in device_info.get("service", ""):
            tunnel = await device_manager.start_tunnel(
                device_udid=device_udid,
                device_host=device_info.get("host", ""),
                # Never pop an admin password dialog from the background loop.
                allow_escalation=False,
            )
            if tunnel.get("status") != "ok":
                raise RuntimeError(tunnel.get("message") or "Apple TV tunnel is not ready.")
        if device_info.get("service") == "usbmux":
            # Same preference as the install path: register the UDID lockdown
            # reported, not the usbmux list serial.
            real_udid = (
                (device_info.get("properties") or {}).get("UniqueDeviceID")
                or device_info["udid"]
            )
            sub_platform = None
        else:
            real_udid, sub_platform = await device_manager.get_real_udid(
                device_udid=device_udid,
                device_host=device_info.get("host", ""),
            )
        await dev_services.register_device(
            session,
            team_id,
            real_udid,
            device_info.get("name") or rec.get("device_name", ""),
        )

        ipa_info = await ipa_processor.inspect(str(ipa_file))
        bundle_id = rec.get("bundle_id") or ipa_info["bundle_id"]
        app_id = await dev_services.register_app_id(session, team_id, bundle_id)
        profile = await dev_services.create_profile(
            session, team_id, app_id, cert, real_udid, sub_platform=sub_platform
        )
        rewrite_bundle_id = bundle_id if bundle_id != ipa_info["bundle_id"] else None

        extension_profiles: dict[str, bytes] = {}
        if rewrite_bundle_id:
            for extension in await ipa_processor.inspect_extensions(str(ipa_file)):
                extension_bundle_id = extension.get("bundle_id", "")
                if not extension_bundle_id:
                    continue
                target_extension_bundle_id = _rewrite_nested_bundle_id(
                    ipa_info["bundle_id"],
                    bundle_id,
                    extension_bundle_id,
                )
                extension_app_id = await dev_services.register_app_id(
                    session,
                    team_id,
                    target_extension_bundle_id,
                )
                extension_profiles[target_extension_bundle_id] = await dev_services.create_profile(
                    session,
                    team_id,
                    extension_app_id,
                    cert,
                    real_udid,
                    sub_platform=sub_platform,
                )

        signed_path = await signer.sign(
            str(ipa_file),
            cert,
            private_key,
            profile,
            rewrite_bundle_id,
            extension_profiles=extension_profiles,
        )
        await device_manager.install(device_udid, signed_path)

        # Success — reset failure count
        rec["fail_count"] = 0
        record_install(
            device_udid,
            str(ipa_file),
            device_info.get("name") or rec.get("device_name", ""),
            bundle_id=bundle_id,
            source_bundle_id=ipa_info.get("bundle_id", ""),
            app_name=ipa_info.get("bundle_name", ""),
            ipa_sha256=rec.get("ipa_sha256", ""),
            ipa_size=rec.get("ipa_size"),
            original_filename=rec.get("original_filename", ""),
            expires_at=provisioning.profile_expiration_ts(profile),
        )
        logger.info("Auto-refresh complete: %s", ipa_path)
        return {"status": "ok", "message": "Auto-refresh complete."}
    except Exception as e:
        rec["fail_count"] = rec.get("fail_count", 0) + 1
        delay = retry_delay_seconds(rec["fail_count"])
        rec["next_attempt_at"] = _now_ts() + delay
        # Persist failure count and backoff window
        state = load_state()
        for r in state.get("installs", []):
            if r.get("device_udid") == device_udid and r.get("ipa_path") == ipa_path:
                r["fail_count"] = rec["fail_count"]
                r["next_attempt_at"] = rec["next_attempt_at"]
                break
        save_state(state)
        logger.exception(
            "Auto-refresh failed for %s (attempt %d, retrying in %.0f min)",
            ipa_path, rec["fail_count"], delay / 60,
        )
        return {"status": "error", "message": str(e), "fail_count": rec["fail_count"]}


STORE_CHECK_INTERVAL_SECONDS = 86400


def store_updates_due(state: dict, catalog: dict[str, str], now: float | None = None) -> list[dict]:
    """Install records whose source publishes a newer version.

    catalog maps store_app_key -> latest version. Only records that opted into
    auto-update and are not pinned are considered.
    """
    from catapult import store as _store

    due: list[dict] = []
    for rec in state.get("installs", []):
        key = rec.get("store_app_key")
        if not key or not rec.get("store_auto_update"):
            continue
        if rec.get("store_pinned"):
            continue
        latest = catalog.get(key)
        if latest and _store.is_newer(latest, rec.get("store_version")):
            due.append(rec)
    return due


def store_check_is_due(state: dict, now: float | None = None) -> bool:
    now = now if now is not None else _now_ts()
    last = float(state.get("store_checked_at") or 0)
    return (now - last) >= STORE_CHECK_INTERVAL_SECONDS


def mark_store_checked(now: float | None = None) -> None:
    state = load_state()
    state["store_checked_at"] = now if now is not None else _now_ts()
    save_state(state)


def set_store_auto_update(device_udid: str, app_key: str, enabled: bool) -> bool:
    """Opt one installed store app in or out of the daily update. Off by default."""
    state = load_state()
    changed = False
    for rec in state.get("installs", []):
        if rec.get("device_udid") == device_udid and rec.get("store_app_key") == app_key:
            rec["store_auto_update"] = bool(enabled)
            changed = True
    if changed:
        save_state(state)
    return changed


async def run_store_update_check(
    get_server_components,
    *,
    sources=None,
    fetch_catalog=None,
    now: float | None = None,
) -> dict:
    """The daily store check, folded into the refresh loop.

    Fetch every source, find installs that opted into auto-update and whose
    source publishes a newer build, and install them — but only where the
    device is reachable right now. Nothing installs to a device that is not
    there; those records are simply looked at again at the next daily check.
    """
    from catapult import store as _store

    now = now if now is not None else _now_ts()
    state = load_state()
    if not store_check_is_due(state, now):
        return {"status": "skipped"}
    mark_store_checked(now)

    components = tuple(get_server_components())
    device_manager = components[0]
    installer = components[6] if len(components) > 6 else None
    fetch = fetch_catalog or _store.fetch_catalog
    sources = _store.load_sources() if sources is None else list(sources)

    catalog: dict[str, "_store.StoreApp"] = {}
    source_errors: list[str] = []
    for source in sources:
        try:
            for app in await fetch(source):
                current = catalog.get(app.app_key)
                if current is None or _store.is_newer(app.version, current.version):
                    catalog[app.app_key] = app
        except Exception as e:
            logger.info("Store check: source %s failed: %s", source.id, e)
            source_errors.append(source.id)

    versions = {key: app.version for key, app in catalog.items()}
    updated: list[str] = []
    unreachable: list[str] = []
    failed: list[str] = []
    for rec in store_updates_due(state, versions, now):
        udid = rec.get("device_udid", "")
        app = catalog[rec["store_app_key"]]
        try:
            device = await device_manager.get_device_info(udid)
            if not device.get("installable", True):
                raise RuntimeError("the device is not ready for installation")
        except Exception as e:
            logger.info("Store update of %s on %s deferred: %s", app.name, udid, e)
            unreachable.append(udid)
            continue
        if installer is None:
            logger.warning("Store update of %s: no installer available", app.name)
            failed.append(udid)
            continue

        async def progress(step, pct, message, _app=app, _udid=udid):
            logger.info("Store update %s on %s: %s", _app.name, _udid, message)

        try:
            await installer(udid, app, progress)
            updated.append(udid)
        except Exception:
            logger.exception("Store update of %s on %s failed", app.name, udid)
            failed.append(udid)

    return {
        "status": "ok",
        "sources": len(sources),
        "updated": updated,
        "unreachable": unreachable,
        "failed": failed,
        "source_errors": source_errors,
    }


def tag_store_install(device_udid: str, ipa_path: str, app_key: str, version: str) -> None:
    """Mark an install as coming from a store source, so updates can track it.

    Matched on the vault digest rather than the download path, because
    record_install() rewrites ipa_path to the content-addressed vault copy.
    """
    digest = ""
    try:
        digest = vault.sha256_file(ipa_path)
    except OSError:
        logger.debug("Could not hash %s for store tagging", ipa_path, exc_info=True)

    state = load_state()
    for rec in state.get("installs", []):
        if rec.get("device_udid") != device_udid:
            continue
        if digest and rec.get("ipa_sha256") == digest:
            rec["store_app_key"] = app_key
            rec["store_version"] = version
            save_state(state)
            return
    logger.debug("No install record matched the store app %s", app_key)
