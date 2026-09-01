"""Sync and power endpoints.

TestClient is constructed without entering its context manager so the app's
startup hook never runs — we do not want the refresh loop, session restore, or
a device scan firing during tests.
"""

import pytest
from fastapi.testclient import TestClient

from catapult import server, sync


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    # The developer's own machine has a real ~/.catapult/config.env, which
    # SyncConfig still reads as a one-release migration fallback. Point it at
    # nothing so these tests start genuinely unconfigured.
    monkeypatch.setattr(sync, "CONFIG_ENV_PATH", tmp_path / "absent.env")
    for name in (
        "CATAPULT_SYNC_PROVIDER", "CATAPULT_SYNC_FOLDER", "CATAPULT_SYNC_KEY",
        "CATAPULT_R2_ENDPOINT", "CATAPULT_R2_BUCKET",
        "CATAPULT_R2_ACCESS_KEY_ID", "CATAPULT_R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server.auth_client, "session", None, raising=False)
    return TestClient(server.app)


def test_legacy_dotfile_still_configures_sync(tmp_path, monkeypatch):
    """One release of overlap: an existing config.env user is not stranded."""
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    legacy = tmp_path / "config.env"
    legacy.write_text(
        "CATAPULT_SYNC_PROVIDER=folder\n"
        f"CATAPULT_SYNC_FOLDER={tmp_path / 'legacy-vault'}\n"
    )
    monkeypatch.setattr(sync, "CONFIG_ENV_PATH", legacy)
    monkeypatch.delenv("CATAPULT_SYNC_PROVIDER", raising=False)

    config = sync.SyncConfig.load()

    assert config.provider == "folder"
    assert config.folder == tmp_path / "legacy-vault"


def test_status_reports_disabled_before_setup(client):
    body = client.get("/api/sync/status").json()

    assert body["vault_state"] == "disabled"
    assert body["provider"] == "disabled"


def test_configure_rejects_an_unknown_provider(client):
    response = client.post("/api/sync/configure", json={"provider": "dropbox"})

    assert response.status_code == 400


def test_configure_stores_a_folder_provider(client, tmp_path):
    response = client.post(
        "/api/sync/configure",
        json={"provider": "folder", "folder": str(tmp_path / "vault")},
    )

    assert response.status_code == 200
    assert sync.SyncConfig.load().provider == "folder"


def test_configure_refuses_icloud_when_icloud_drive_is_off(client, monkeypatch):
    """Better an honest error than a vault written to a path that never syncs."""
    monkeypatch.setattr(sync, "icloud_drive_available", lambda: False)

    response = client.post("/api/sync/configure", json={"provider": "folder"})

    assert response.status_code == 400
    assert response.json()["status"] == "needs_icloud"


def test_unlock_requires_authentication(client):
    response = client.post("/api/sync/unlock", json={"recovery_key": "CAT1-AAAAA"})

    assert response.status_code == 401


def test_create_vault_requires_authentication(client):
    assert client.post("/api/sync/create-vault").status_code == 401


def test_wake_command_is_returned_not_executed(client):
    body = client.get("/api/power/wake-command", params={"hour": 4, "minute": 15}).json()

    assert body["command"] == "sudo pmset repeat wake MTWRFSU 04:15:00"
    assert "battery" in body["note"]


def test_wake_command_validates_the_time(client):
    assert client.get("/api/power/wake-command", params={"hour": 99}).status_code == 400


class _Session:
    authenticated = True
    apple_id = "me@example.com"


def test_recovery_key_requires_authentication(client):
    assert client.get("/api/sync/recovery-key").status_code == 401


def test_sync_run_reports_informational_states_as_200(client, monkeypatch):
    """A 500 made the Swift client throw and drop the body, so the settings pane
    never learned it should show the unlock or create-vault controls."""
    monkeypatch.setattr(server.auth_client, "session", _Session(), raising=False)

    async def locked():
        return {"status": "locked", "vault_state": "locked", "provider": "folder", "configured": True}

    monkeypatch.setattr(server, "_sync_authenticated_state", locked)

    response = client.post("/api/sync/run")

    assert response.status_code == 200
    assert response.json()["vault_state"] == "locked"


def test_sync_run_still_reports_failures_as_500(client, monkeypatch):
    monkeypatch.setattr(server.auth_client, "session", _Session(), raising=False)

    async def failed():
        return {"status": "error", "message": "boom"}

    monkeypatch.setattr(server, "_sync_authenticated_state", failed)

    assert client.post("/api/sync/run").status_code == 500


def test_create_vault_refuses_to_overwrite_without_confirmation(client, monkeypatch):
    monkeypatch.setattr(server.auth_client, "session", _Session(), raising=False)

    async def team():
        return "ABCDE12345"

    monkeypatch.setattr(server, "_current_team_id", team)
    seen = {}

    async def fake_create(apple_id, team_id, *, replace=False):
        seen["replace"] = replace
        return {"status": "exists", "message": "A vault already exists here."}

    monkeypatch.setattr(server._sync, "create_vault", fake_create)

    response = client.post("/api/sync/create-vault")

    assert response.status_code == 409
    assert seen["replace"] is False


def test_create_vault_passes_the_confirmed_replace_flag(client, monkeypatch):
    monkeypatch.setattr(server.auth_client, "session", _Session(), raising=False)

    async def team():
        return "ABCDE12345"

    monkeypatch.setattr(server, "_current_team_id", team)
    seen = {}

    async def fake_create(apple_id, team_id, *, replace=False):
        seen["replace"] = replace
        return {"status": "ok", "recovery_key": "CAT1-TEST", "message": ""}

    monkeypatch.setattr(server._sync, "create_vault", fake_create)

    response = client.post("/api/sync/create-vault", json={"replace": "true"})

    assert response.status_code == 200
    assert seen["replace"] is True


def test_configure_stores_the_region_for_s3_compatible_buckets(client, monkeypatch):
    """The region field existed on SyncConfig but the endpoint rebuilt the config
    without it, so nothing but R2's "auto" was ever reachable from the app."""
    monkeypatch.setattr(sync, "_keychain_set", lambda account, value: True)
    monkeypatch.setattr(sync, "_keychain_get", lambda account: "k")

    response = client.post("/api/sync/configure", json={
        "provider": "r2", "r2_endpoint": "https://s3.eu-central-003.example.com",
        "r2_bucket": "catapult", "r2_access_key_id": "a", "r2_secret_access_key": "s",
        "region": "eu-central-003",
    })

    assert response.status_code == 200
    assert sync.SyncConfig.load().region == "eu-central-003"
