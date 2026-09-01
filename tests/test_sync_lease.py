"""Refresh lease: two Macs sharing a vault take turns refreshing.

teams/<team_id>/lease.json = {locked_by, locked_until, operation}. Acquire
before a refresh cycle, honor a short TTL, skip the cycle when another machine
holds it. Certificate reuse is the other half of stopping the two-Mac fight.
"""

import json

import pytest

from catapult import sync

TEAM = "ABCDE12345"


@pytest.fixture
def store(tmp_path):
    return sync.FolderStore(tmp_path / "vault")


async def test_first_machine_acquires_the_lease(store):
    assert await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)

    doc = json.loads((await store.get(sync._lease_key(TEAM))).decode())
    assert doc["locked_by"] == "mac-a"
    assert doc["operation"] == "refresh"
    assert doc["locked_until"] == 1000.0 + sync.REFRESH_LEASE_TTL_SECONDS


async def test_second_machine_is_refused_while_the_lease_is_live(store):
    await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)

    assert not await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-b", now=1060.0)


async def test_an_expired_lease_can_be_taken_over(store):
    await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)
    later = 1000.0 + sync.REFRESH_LEASE_TTL_SECONDS + 1

    assert await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-b", now=later)


async def test_the_holder_can_renew_its_own_lease(store):
    await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)

    assert await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1060.0)


async def test_release_removes_only_the_holders_lease(store):
    await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)

    await sync.release_refresh_lease(store, TEAM, machine_id="mac-b")
    assert await store.exists(sync._lease_key(TEAM))

    await sync.release_refresh_lease(store, TEAM, machine_id="mac-a")
    assert not await store.exists(sync._lease_key(TEAM))


async def test_a_corrupt_lease_does_not_block_forever(store):
    await store.put(sync._lease_key(TEAM), b"not json")

    assert await sync.acquire_refresh_lease(store, TEAM, machine_id="mac-a", now=1000.0)


def test_lease_ttl_is_short():
    """Long enough for one refresh cycle, short enough that a Mac that died
    mid-cycle does not block the other one for hours."""
    assert 5 * 60 <= sync.REFRESH_LEASE_TTL_SECONDS <= 60 * 60


def test_machine_id_is_stable_across_calls(tmp_path, monkeypatch):
    """Hostnames change with the network; the id must survive that."""
    monkeypatch.setattr(sync, "MACHINE_ID_PATH", tmp_path / "machine-id")

    first = sync.machine_id()

    assert first == sync.machine_id()
    assert len(first) >= 16
