"""Subscribe with AcquireNotify, so BlueZ cannot double the notifications.

Field report daikin_madoka#149, continued (2026-11-15, local BlueZ 5.66): v0.4.5
bound each subscription to its own client and still saw every notification
twice after a forced reconnect. The duplicates came through the CURRENT
client: with StartNotify every notify session on the characteristic makes
BlueZ emit its own PropertiesChanged signal, and bleak's manager hands each one
to the current client's callback by characteristic path alone. btmon counted
14 notifications on the radio, dbus-monitor 28 signals.

AcquireNotify gives each client its own file descriptor, so a stale session
can no longer reach the current one. The option is BlueZ-only; the ESPHome
proxy backend accepts and ignores it.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import pymadoka.connection as connection_module
from pymadoka.connection import NOTIFY_ARGS, Connection


def make_connection():
    return Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(),
        candidates_callback=lambda: [SimpleNamespace(
            address="00:11:22:33:44:55", name="Daikin",
            details={"source": "PROXY_A"},
        )],
    )


def test_the_option_asks_bluez_for_acquire_notify():
    assert NOTIFY_ARGS == {"bluez": {"use_start_notify": False}}


@pytest.mark.asyncio
async def test_the_ha_path_subscribes_with_acquire_notify():
    client = AsyncMock()
    client.is_connected = True
    conn = make_connection()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), \
         patch("pymadoka.connection.asyncio.sleep", AsyncMock()):
        await conn._connect_via_ha()

    assert client.start_notify.await_args.kwargs == NOTIFY_ARGS


def test_every_subscription_in_the_module_passes_the_option():
    """A guard for the paths the HA test above does not drive: a new
    start_notify call without the option would bring the duplicates back."""
    source = inspect.getsource(connection_module)
    calls = source.split(".start_notify(")[1:]
    assert len(calls) == 3
    # The arguments of each call end before the next blank-ish statement;
    # 200 characters cover the longest one.
    assert all("**NOTIFY_ARGS" in call[:200] for call in calls)
