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
