"""HA WebSocket client against the fake HA (which reproduces the verified HA behavior)."""

import asyncio

import pytest

from tests.shopping.fake_ha import FakeHA
from thymble.connectors.home_assistant.client import (
    AuthError,
    ConnectionClosed,
    HAClient,
    HAError,
    websocket_url,
)

pytestmark = pytest.mark.asyncio

E = "todo.main"


async def client(fake, token="test-token"):
    c = HAClient(await fake.connect(), token)
    await c.start()
    return c


def test_websocket_url():
    assert websocket_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"
    assert websocket_url("https://ha.example.com/") == "wss://ha.example.com/api/websocket"
    assert websocket_url("https://x/proxy/") == "wss://x/proxy/api/websocket"
    with pytest.raises(ValueError):
        websocket_url("ftp://x")


async def test_auth_rejected():
    with pytest.raises(AuthError):
        await client(FakeHA((E,)), token="wrong")


async def test_events_fire_twice_identically_and_service_returns_no_items():
    fake = FakeHA((E,), double_fire_delay=0.03)
    c = await client(fake)
    assert c.ha_version == "2026.9.4"
    events = []
    await c.subscribe_todo(E, events.append)
    await asyncio.sleep(0.01)
    assert events == [{"items": []}]  # initial snapshot
    await c.call_service("add_item", E, {"item": "thymble test item"})
    await asyncio.sleep(0.1)
    assert len(events) == 3 and events[1] == events[2]
    item = events[1]["items"][0]
    assert set(item) == {"uid", "summary", "status", "due", "description", "completed"}
    await c.call_service("update_item", E, {"item": item["uid"], "status": "completed"})
    await asyncio.sleep(0.1)
    assert events[-1]["items"][0]["status"] == "completed"  # struck through, not removed
    await c.call_service("remove_item", E, {"item": item["uid"]})
    await asyncio.sleep(0.1)
    assert events[-1] == events[-2] == {"items": []}
    await c.close()


async def test_unknown_uid_is_an_error():
    c = await client(FakeHA((E,)))
    with pytest.raises(HAError):
        await c.call_service("remove_item", E, {"item": "nope"})
    await c.close()


async def test_window_drops_oldest_and_restores_when_list_shrinks():
    fake = FakeHA((E,), window=100, double_fire_delay=0.01)
    fake.seed(E, ["Cheese"], "completed")
    fake.seed(E, [f"item {i}" for i in range(99)])
    c = await client(fake)
    snap = await c.list_items(E)
    assert len(snap) == 100 and snap[0]["summary"] == "Cheese"
    await c.call_service("add_item", E, {"item": "thymble test item"})
    snap = await c.list_items(E)
    assert len(snap) == 100 and "Cheese" not in [i["summary"] for i in snap]
    await c.call_service("remove_item", E, {"item": "thymble test item"})
    assert "Cheese" in [i["summary"] for i in await c.list_items(E)]
    await c.close()


async def test_drop_connections_closes_client():
    fake = FakeHA((E,))
    c = await client(fake)
    fake.drop_connections()
    await asyncio.wait_for(c.closed.wait(), 1)
    with pytest.raises(ConnectionClosed):
        await c.list_items(E)
