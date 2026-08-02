"""Team selection.

An Apple ID with a paid membership almost always also carries a free
"Individual" team. Picking the free one silently caps the user at 10 App IDs,
3 installed apps, and 7-day profiles when they are entitled to a year.
"""

import pytest

from catapult.developer import DeveloperServices, team_is_free


class FakeSession:
    pass


async def _get_team(teams):
    services = DeveloperServices()

    async def fake_request(session, action, fields=None):
        return {"teams": teams}

    services._request = fake_request
    return await services.get_team(FakeSession())


async def test_prefers_a_paid_team_over_the_free_one():
    team = await _get_team([
        {"teamId": "FREE1", "type": "Individual", "status": "active"},
        {"teamId": "PAID1", "type": "Company/Organization", "status": "active"},
    ])

    assert team["teamId"] == "PAID1"


async def test_prefers_paid_regardless_of_order():
    team = await _get_team([
        {"teamId": "PAID1", "type": "Company/Organization", "status": "active"},
        {"teamId": "FREE1", "type": "Individual", "status": "active"},
    ])

    assert team["teamId"] == "PAID1"


async def test_falls_back_to_the_free_team_when_that_is_all_there_is():
    team = await _get_team([{"teamId": "FREE1", "type": "Individual", "status": "active"}])

    assert team["teamId"] == "FREE1"


async def test_prefers_an_active_team_among_equals():
    team = await _get_team([
        {"teamId": "OLD", "type": "Individual", "status": "expired"},
        {"teamId": "NOW", "type": "Individual", "status": "active"},
    ])

    assert team["teamId"] == "NOW"


async def test_raises_when_there_are_no_teams():
    with pytest.raises(Exception):
        await _get_team([])


async def test_prefers_a_paid_individual_team_over_the_free_personal_one():
    """Both teams are type "Individual", so type alone cannot tell them apart.
    Apple flags the free personal team with xcodeFreeOnly and gives the paid
    one an active program membership."""
    team = await _get_team([
        {"teamId": "FREE1", "type": "Individual", "status": "active", "xcodeFreeOnly": True},
        {
            "teamId": "PAID1",
            "type": "Individual",
            "status": "active",
            "memberships": [{"status": "active", "name": "Apple Developer Program"}],
        },
    ])

    assert team["teamId"] == "PAID1"


async def test_prefers_an_active_free_team_over_an_expired_paid_one():
    """An expired paid team cannot sign anything, so the active free team wins."""
    team = await _get_team([
        {"teamId": "PAID1", "type": "Company/Organization", "status": "expired"},
        {"teamId": "FREE1", "type": "Individual", "status": "active"},
    ])

    assert team["teamId"] == "FREE1"


def test_team_is_free_trusts_xcode_free_only_first():
    assert team_is_free({
        "type": "Individual",
        "xcodeFreeOnly": True,
        "memberships": [{"status": "active"}],
    })


def test_team_is_free_uses_membership_status_when_apple_sent_it():
    assert not team_is_free({"type": "Individual", "memberships": [{"status": "active"}]})
    assert team_is_free({"type": "Individual", "memberships": [{"status": "expired"}]})


def test_team_is_free_falls_back_to_type_without_membership_data():
    assert team_is_free({"type": "Individual"})
    assert team_is_free({})
    assert not team_is_free({"type": "Company/Organization"})
    assert not team_is_free({"type": "In-House"})
