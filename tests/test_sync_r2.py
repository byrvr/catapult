"""R2Store: the SigV4 region is configurable, and blobs stream through disk.

`region` was hardcoded to "auto", which is right for Cloudflare R2 and wrong
for every other S3-compatible bucket. And R2Store inherited the base-class
put_file/get_file, which read the whole encrypted IPA into memory — the exact
3 GB peak the streaming blob format was introduced to remove.
"""

import hashlib

import httpx

from catapult import sync


def _config(region="auto"):
    return sync.SyncConfig(
        provider="r2",
        folder=None,
        r2_endpoint="https://acct.r2.cloudflarestorage.com",
        r2_bucket="catapult",
        r2_access_key_id="AKIAEXAMPLE",
        r2_secret_access_key="secret",
        region=region,
    )


def test_region_is_part_of_the_signing_scope():
    store = sync.R2Store(_config(region="us-east-1"))

    headers = store._sign_headers("GET", store._url("teams/T/vault.json"))

    assert "/us-east-1/s3/aws4_request" in headers["Authorization"]


def test_region_defaults_to_auto_for_r2():
    store = sync.R2Store(_config())

    assert "/auto/s3/aws4_request" in store._sign_headers("GET", store._url("k"))["Authorization"]


def test_config_round_trips_region(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.setattr(sync, "_keychain_set", lambda account, value: True)
    monkeypatch.setattr(sync, "_keychain_get", lambda account: "x")

    _config(region="eu-central-003").save()

    assert sync.SyncConfig.load().region == "eu-central-003"


async def test_put_file_streams_the_file_with_its_digest(tmp_path):
    payload = b"x" * (3 * 1024 * 1024 + 17)
    src = tmp_path / "blob.enc"
    src.write_bytes(payload)
    seen = {}

    async def handler(request: httpx.Request):
        seen["method"] = request.method
        seen["body"] = await request.aread()
        seen["sha"] = request.headers.get("x-amz-content-sha256")
        seen["length"] = request.headers.get("content-length")
        return httpx.Response(200)

    store = sync.R2Store(_config())
    store.transport = httpx.MockTransport(handler)

    await store.put_file("teams/T/ipas/abc.ipa.enc", src)

    assert seen["method"] == "PUT"
    assert seen["body"] == payload
    assert seen["sha"] == hashlib.sha256(payload).hexdigest()
    assert seen["length"] == str(len(payload))


async def test_get_file_streams_to_disk(tmp_path):
    payload = b"y" * (2 * 1024 * 1024 + 5)

    async def handler(request: httpx.Request):
        return httpx.Response(200, content=payload)

    store = sync.R2Store(_config())
    store.transport = httpx.MockTransport(handler)
    dest = tmp_path / "out" / "blob.enc"

    assert await store.get_file("teams/T/ipas/abc.ipa.enc", dest)
    assert dest.read_bytes() == payload


async def test_get_file_reports_a_missing_object(tmp_path):
    async def handler(request: httpx.Request):
        return httpx.Response(404)

    store = sync.R2Store(_config())
    store.transport = httpx.MockTransport(handler)
    dest = tmp_path / "out" / "blob.enc"

    assert not await store.get_file("teams/T/ipas/missing.ipa.enc", dest)
    assert not dest.exists()
