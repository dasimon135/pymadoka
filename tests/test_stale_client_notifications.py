"""One thermostat, one live client: an older one must not feed the transport.

Field report daikin_madoka#149 (2026-11-12, Raspberry Pi built-in adapter): on
one thermostat every notification reached the library five times. A multi-chunk
answer (SetPoint, four chunks) cannot survive that: a repeated chunk reads as
the start of a new message, the chunks collected so far are thrown away and the
waiting query is cancelled as "connection lost". One-chunk answers survive,
which is why only the setpoint was unreadable.

Every client a Connection ever opened was subscribed with the same bound
handler, so nothing told a notification of the current link from one relayed by
a client that had been replaced.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pymadoka.connection import Connection, ConnectionStatus


def make_device(source="PROXY_A"):
    return SimpleNamespace(
        address="00:11:22:33:44:55", name="Daikin", details={"source": source}
    )


def make_client(connected=True):
    client = AsyncMock()
    client.is_connected = connected
    return client


def make_connection():
    return Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=lambda: [make_device()],
    )


def handler_of(client):
    """The notification callback this client was subscribed with."""
    return client.start_notify.await_args.args[1]


async def connect(conn, client):
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), \
         patch("pymadoka.connection.asyncio.sleep", AsyncMock()):
        await conn._connect_via_ha()


# A complete one-chunk message: chunk id 0, then length 6 and five more bytes.
ONE_CHUNK = bytearray([0x00, 0x06, 0x00, 0x00, 0x30, 0x20, 0x01])


@pytest.mark.asyncio
async def test_a_replaced_client_no_longer_reaches_the_transport(caplog):
    conn = make_connection()
    old = make_client()
    await connect(conn, old)
    # The link is reported lost, then re-established with a new client; the
    # old client object stays subscribed, as it did on the reporter's adapter.
    old.is_connected = False
    conn.on_disconnect(old)
    new = make_client()
    await connect(conn, new)
    assert conn.client is new

    rebuilt = []
    conn.transport.rebuild_chunk = rebuilt.append
    with caplog.at_level(logging.WARNING, logger="pymadoka.connection"):
        handler_of(old)("char", ONE_CHUNK)
        handler_of(old)("char", ONE_CHUNK)
        handler_of(new)("char", ONE_CHUNK)

    assert rebuilt == [ONE_CHUNK]
    # Said once per connection, not once per dropped chunk.
    warnings = [r for r in caplog.records if "replaced connection" in r.message]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_a_client_still_connected_is_closed_before_reconnecting(caplog):
    conn = make_connection()
    old = make_client()
    await connect(conn, old)
    # The library lost track of the link (status no longer CONNECTED) while
    # the client itself still reports it up.
    conn.connection_status = ConnectionStatus.DISCONNECTED
    new = make_client()
    with caplog.at_level(logging.INFO, logger="pymadoka.connection"):
        await connect(conn, new)

    old.disconnect.assert_awaited_once()
    new.disconnect.assert_not_awaited()
    assert conn.client is new
    assert any("still connected" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_dead_previous_client_is_left_alone():
    conn = make_connection()
    old = make_client()
    await connect(conn, old)
    old.is_connected = False
    conn.on_disconnect(old)
    await connect(conn, make_client())
    old.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_closed_client_cannot_mark_the_new_link_disconnected():
    conn = make_connection()
    old = make_client()
    await connect(conn, old)
    conn.connection_status = ConnectionStatus.DISCONNECTED
    new = make_client()
    await connect(conn, new)
    # Its disconnect callback arrives late, after the new link is up.
    conn.on_disconnect(old)
    assert conn.connection_status is ConnectionStatus.CONNECTED
