"""Connector supervisor: one engine per active account, restarts on changes, Redis lease."""

import asyncio

import pytest

from tests.conftest import make_settings
from tests.shopping.fake_ha import FakeHA
from thymble.connectors.crypto import encrypt_credentials
from thymble.connectors.home_assistant.supervisor import ConnectorSupervisor
from thymble.models import ConnectorAccount

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

MAIN = "todo.shopping_list"
SETTINGS = make_settings(connector_encryption_key="supervisor-test-key")
FAST = dict(reconnect_delay=0.05, poll_interval=3600, sweep_interval=3600)


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, px=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def pexpire(self, key, ms):
        return key in self.data

    async def delete(self, key):
        self.data.pop(key, None)


def _account(sessions, household, setup, **kw):
    with sessions() as s:
        account = ConnectorAccount(
            household_id=household,
            kind="home_assistant",
            base_url="http://fake-ha.invalid",
            config={"main_entity": MAIN, "list_id": str(setup.list.id)},
            credentials_encrypted=encrypt_credentials(SETTINGS, {"token": "test-token"}),
            **kw,
        )
        s.add(account)
        s.commit()
        return account.id


def _supervisor(sessions, fake, **kw):
    return ConnectorSupervisor(
        SETTINGS,
        sessions,
        connect_factory=lambda account: fake.connect,
        engine_overrides=FAST,
        **kw,
    )


async def test_starts_restarts_and_stops_engines(sessions, household, setup):
    fake = FakeHA((MAIN,), double_fire_delay=0.01)
    account_id = _account(sessions, household, setup)
    sup = _supervisor(sessions, fake)
    try:
        await sup.reconcile()
        engine = sup.engines[account_id]
        await asyncio.wait_for(engine.connected.wait(), 2)
        await sup.reconcile()
        assert sup.engines[account_id] is engine  # unchanged -> left running

        with sessions() as s:
            s.get(ConnectorAccount, account_id).config = {
                "main_entity": MAIN,
                "list_id": str(setup.list.id),
                "dry_run": True,
            }
            s.commit()
        await sup.reconcile()
        restarted = sup.engines[account_id]
        assert restarted is not engine and restarted.dry_run

        with sessions() as s:
            s.get(ConnectorAccount, account_id).status = "disabled"
            s.commit()
        await sup.reconcile()
        assert sup.engines == {}
    finally:
        await sup.stop()


async def test_unreadable_credentials_are_reported(sessions, household, setup):
    fake = FakeHA((MAIN,))
    with sessions() as s:
        account = ConnectorAccount(
            household_id=household,
            kind="home_assistant",
            base_url="http://fake-ha.invalid",
            config={"main_entity": MAIN},
            credentials_encrypted=b"not a fernet token",
        )
        s.add(account)
        s.commit()
        account_id = account.id
    sup = _supervisor(sessions, fake)
    await sup.reconcile()
    assert sup.engines == {} and fake.connect_count == 0
    with sessions() as s:
        assert "can't be decrypted" in s.get(ConnectorAccount, account_id).last_error


async def test_lease_keeps_a_connector_on_one_worker(sessions, household, setup):
    fake = FakeHA((MAIN,), double_fire_delay=0.01)
    account_id = _account(sessions, household, setup)
    redis = FakeRedis()
    first, second = (
        _supervisor(sessions, fake, redis=redis),
        _supervisor(sessions, fake, redis=redis),
    )
    try:
        await first.reconcile()
        await second.reconcile()
        assert account_id in first.engines and second.engines == {}
        await first.stop()
        await second.reconcile()
        assert account_id in second.engines
    finally:
        await first.stop()
        await second.stop()
