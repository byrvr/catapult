"""The daily store check installs through a path that can never prompt.

`_install_app` starts the Apple TV tunnel with escalation allowed, which is
right for a user pressing Install and wrong for an unattended daily check:
nobody is there to answer an admin password dialog.
"""

from catapult import server, store


def _app():
    return store.StoreApp(source_id="s", app_key="k", name="A", version="1",
                          platform="tvos", download_url="https://x/a.ipa", sha256="")


async def _capture_install(monkeypatch):
    seen = {}

    async def fake_install_app(device_udid, ipa_path, progress, device_name_hint="", *, allow_escalation=True):
        seen["allow_escalation"] = allow_escalation
        return {"status": "ok", "message": "ok"}

    async def fake_download(url, dest, *, expected_sha256=""):
        return dest

    monkeypatch.setattr(server, "_install_app", fake_install_app)
    monkeypatch.setattr(server._store, "download_to", fake_download)
    monkeypatch.setattr(server._refresh, "tag_store_install", lambda *a, **k: None)
    return seen


async def _progress(step, pct, message):
    return None


async def test_refresh_loop_installer_never_escalates(monkeypatch):
    seen = await _capture_install(monkeypatch)
    installer = server._server_components()[6]

    await installer("TV1", _app(), _progress)

    assert seen["allow_escalation"] is False


async def test_store_tab_install_still_may_escalate(monkeypatch):
    """A user pressing Install is exactly when asking for the password is right."""
    seen = await _capture_install(monkeypatch)

    await server._install_store_app("TV1", _app(), _progress)

    assert seen["allow_escalation"] is True
