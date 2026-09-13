"""_gsa_request retries GSA's pinned-node 5xx/HTML replies on a fresh connection."""
import plistlib
import httpx
import pytest
from catapult import apple_auth


def _plist(d): return plistlib.dumps({"Header": {}, "Response": d})


class FakeClient:
    def __init__(self, replies, closed):
        self._replies = replies
        self._closed = closed

    async def post(self, url, content=None, headers=None):
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        status, ctype, body = item
        return httpx.Response(status, headers={"content-type": ctype}, content=body)

    async def aclose(self):
        self._closed.append(True)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_): return None
    monkeypatch.setattr(apple_auth.asyncio, "sleep", instant)
    monkeypatch.setattr(apple_auth, "gsa_request_headers", lambda: {})


async def test_recovers_after_a_503_html_page(monkeypatch):
    replies = [
        (503, "text/html", b"<html>503 Service Temporarily Unavailable</html>"),
        (200, "text/x-xml-plist", _plist({"spim": "OK"})),
    ]
    closed = []
    c = apple_auth.AppleAuthClient()
    clients = iter([FakeClient(replies, closed), FakeClient(replies, closed)])
    monkeypatch.setattr(apple_auth.AppleAuthClient, "_new_client", staticmethod(lambda: next(clients)))
    # rebuild so the first FakeClient is in place
    c._client = apple_auth.AppleAuthClient._new_client()
    out = await c._gsa_request({"o": "init"})
    assert out["Response"]["spim"] == "OK"
    assert closed  # the dead connection was dropped


async def test_gives_up_with_a_clear_error(monkeypatch):
    replies = [(503, "text/html", b"<html>down</html>")] * 5
    closed = []
    monkeypatch.setattr(apple_auth.AppleAuthClient, "_new_client", staticmethod(lambda: FakeClient(replies, closed)))
    c = apple_auth.AppleAuthClient()
    with pytest.raises(apple_auth.GSAError):
        await c._gsa_request({"o": "init"})


async def test_retries_on_transport_error(monkeypatch):
    replies = [httpx.ConnectError("boom"), (200, "text/x-xml-plist", _plist({"ok": "1"}))]
    closed = []
    monkeypatch.setattr(apple_auth.AppleAuthClient, "_new_client", staticmethod(lambda: FakeClient(replies, closed)))
    c = apple_auth.AppleAuthClient()
    out = await c._gsa_request({"o": "init"})
    assert out["Response"]["ok"] == "1"
