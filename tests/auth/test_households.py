"""Households: membership, roles, invites, join and leave."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from tests.auth.conftest import SignedIn, sign_in
from thymble.auth.tokens import now
from thymble.models import HouseholdInvite

pytestmark = pytest.mark.db


def invite(api, owner: SignedIn) -> dict:
    response = api.post(f"/v1/households/{owner.household_id}/invites", headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


def join(api, user: SignedIn, code: str):
    return api.post("/v1/households/join", json={"code": code}, headers=user.headers)


def members(api, user: SignedIn, household_id: str):
    return api.get(f"/v1/households/{household_id}/members", headers=user.headers)


def test_list_and_create(api, alice):
    listed = api.get("/v1/households", headers=alice.headers).json()
    assert [(h["id"], h["role"]) for h in listed] == [(alice.household_id, "owner")]
    created = api.post("/v1/households", json={"name": "Cabin"}, headers=alice.headers)
    assert created.status_code == 201
    assert len(api.get("/v1/households", headers=alice.headers).json()) == 2


def test_invite_and_join(api, alice, bob):
    code = invite(api, alice)
    assert code["url"].endswith(f"/join?code={code['code']}")
    # Codes are forgiving about case and separators.
    joined = join(api, bob, code["code"].lower().replace("-", " "))
    assert joined.status_code == 200
    assert joined.json() == {
        "id": alice.household_id,
        "name": "Alice's household",
        "role": "member",
    }
    roles = {m["user_id"]: m["role"] for m in members(api, alice, alice.household_id).json()}
    assert roles == {alice.user_id: "owner", bob.user_id: "member"}
    # Bob now sees both households.
    assert len(api.get("/v1/households", headers=bob.headers).json()) == 2


def test_invite_is_single_use(api, alice, bob, fake_apple):
    code = invite(api, alice)["code"]
    assert join(api, bob, code).status_code == 200
    carol = sign_in(api, fake_apple, "apple-carol")
    assert join(api, carol, code).status_code == 404


def test_expired_and_revoked_invites(api, alice, bob, db):
    expired = invite(api, alice)
    db.execute(
        update(HouseholdInvite)
        .where(HouseholdInvite.id == expired["id"])
        .values(expires_at=now() - timedelta(minutes=1))
    )
    db.commit()
    assert join(api, bob, expired["code"]).status_code == 404

    revoked = invite(api, alice)
    url = f"/v1/households/{alice.household_id}/invites/{revoked['id']}"
    assert api.delete(url, headers=alice.headers).status_code == 204
    assert join(api, bob, revoked["code"]).status_code == 404


def test_already_member_does_not_burn_code(api, alice, bob):
    code = invite(api, alice)["code"]
    assert join(api, alice, code).status_code == 409
    assert join(api, bob, code).status_code == 200


def test_non_members_see_nothing(api, alice, bob):
    hid = alice.household_id
    assert members(api, bob, hid).status_code == 404
    assert api.post(f"/v1/households/{hid}/invites", headers=bob.headers).status_code == 404
    assert (
        api.patch(f"/v1/households/{hid}", json={"name": "x"}, headers=bob.headers).status_code
        == 404
    )
    assert api.post(f"/v1/households/{hid}/leave", headers=bob.headers).status_code == 404


def test_members_cannot_manage(api, alice, bob):
    join(api, bob, invite(api, alice)["code"])
    hid = alice.household_id
    assert members(api, bob, hid).status_code == 200
    assert api.post(f"/v1/households/{hid}/invites", headers=bob.headers).status_code == 403
    assert (
        api.patch(f"/v1/households/{hid}", json={"name": "x"}, headers=bob.headers).status_code
        == 403
    )
    url = f"/v1/households/{hid}/members/{alice.user_id}"
    assert api.delete(url, headers=bob.headers).status_code == 403


def test_roles_and_leaving(api, alice, bob):
    hid = alice.household_id
    join(api, bob, invite(api, alice)["code"])
    # The only owner can't leave while others remain...
    assert api.post(f"/v1/households/{hid}/leave", headers=alice.headers).status_code == 409
    # ...or demote themselves.
    own = f"/v1/households/{hid}/members/{alice.user_id}"
    assert api.patch(own, json={"role": "member"}, headers=alice.headers).status_code == 409
    # Promote Bob, then Alice can leave.
    promote = api.patch(
        f"/v1/households/{hid}/members/{bob.user_id}", json={"role": "owner"}, headers=alice.headers
    )
    assert promote.status_code == 200 and promote.json()["role"] == "owner"
    assert api.post(f"/v1/households/{hid}/leave", headers=alice.headers).status_code == 204
    assert members(api, alice, hid).status_code == 404
    assert [m["user_id"] for m in members(api, bob, hid).json()] == [bob.user_id]


def test_owner_removes_member_and_member_can_rejoin(api, alice, bob):
    hid = alice.household_id
    join(api, bob, invite(api, alice)["code"])
    url = f"/v1/households/{hid}/members/{bob.user_id}"
    assert api.delete(url, headers=alice.headers).status_code == 204
    assert members(api, bob, hid).status_code == 404
    assert join(api, bob, invite(api, alice)["code"]).status_code == 200
    assert members(api, bob, hid).status_code == 200


def test_last_member_leaving_deletes_household(api, alice):
    hid = alice.household_id
    assert api.post(f"/v1/households/{hid}/leave", headers=alice.headers).status_code == 204
    assert api.get("/v1/households", headers=alice.headers).json() == []
