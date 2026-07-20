"""The pairing window must be widenable for human confirmation.

A BRC1H pairs by numeric comparison: HA shows a 6-digit code and the user has
to compare it with the thermostat screen and accept there. The 8s budget that
suits an automatic re-encryption of an existing bond is far too short for that
— the notification typically reaches the user's phone after the attempt has
already timed out.

Field incident 2026-07-20: a newly added proxy had the best RSSI to a
thermostat, so it was tried first, had no bond, and started a real pairing on
every reconnect. Nobody could confirm within 8s, and the repeated half-finished
SMP exchanges jammed the thermostat until its Bluetooth was toggled by hand.

So the timeout stays short by default (automatic reconnects must not hold the
BRC1H's single central slot) and the caller widens it when it knows a human is
standing at the device.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pymadoka import Controller
from pymadoka.connection import DEFAULT_PAIR_TIMEOUT, Connection


def make_device(source):
    return SimpleNamespace(
        address="00:11:22:33:44:55", name="Daikin", details={"source": source}
    )


def make_client():
    client = AsyncMock()
    client.is_connected = True
    client.pair = AsyncMock()
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def make_connection(candidates, **kwargs):
    return Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=lambda: list(candidates), **kwargs,
    )


def capture_pair_timeout():
    """Patch wait_for so the timeout the pair() call was given is observable."""
    seen = {}

    async def fake_wait_for(awaitable, timeout):
        seen["timeout"] = timeout
        return await awaitable

    return seen, patch("pymadoka.connection.asyncio.wait_for", fake_wait_for)


async def connect_once(conn):
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client())), \
         patch("pymadoka.connection.asyncio.sleep", AsyncMock()):
        await conn._connect_via_ha()


@pytest.mark.asyncio
async def test_pairing_window_is_short_by_default():
    seen, patched = capture_pair_timeout()
    conn = make_connection([make_device("PROXY_A")])
    with patched:
        await connect_once(conn)
    assert seen["timeout"] == DEFAULT_PAIR_TIMEOUT


@pytest.mark.asyncio
async def test_pairing_window_can_be_widened_at_runtime():
    """The integration widens it when the user asks to pair, then restores it."""
    seen, patched = capture_pair_timeout()
    conn = make_connection([make_device("PROXY_A")])
    conn.pair_timeout = 60.0
    with patched:
        await connect_once(conn)
    assert seen["timeout"] == 60.0


@pytest.mark.asyncio
async def test_pairing_window_can_be_set_at_construction():
    seen, patched = capture_pair_timeout()
    conn = make_connection([make_device("PROXY_A")], pair_timeout=45.0)
    with patched:
        await connect_once(conn)
    assert seen["timeout"] == 45.0


def test_controller_forwards_the_pairing_window():
    ctrl = Controller("00:11:22:33:44:55", pair_timeout=30.0)
    assert ctrl.connection.pair_timeout == 30.0


def test_controller_defaults_the_pairing_window():
    assert Controller("00:11:22:33:44:55").connection.pair_timeout == (
        DEFAULT_PAIR_TIMEOUT
    )
