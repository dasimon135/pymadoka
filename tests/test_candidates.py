import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError

from pymadoka import Controller
from pymadoka.connection import Connection
from pymadoka.errors import PairingRequiredError, DeviceUnreachableError


def test_candidates_callback_reaches_connection():
    marker = lambda: []  # noqa: E731
    ctrl = Controller("00:11:22:33:44:55", candidates_callback=marker)
    assert ctrl.connection.candidates_callback is marker
    assert ctrl.connection.connected_source is None
    assert ctrl.connection.last_error is None


def test_candidates_callback_defaults_to_none():
    ctrl = Controller("00:11:22:33:44:55")
    assert ctrl.connection.candidates_callback is None


AUTH_FAIL = BleakError(
    "Bluetooth GATT Error address=00:11:22:33:44:55 handle=515 "
    "error=5 description=Insufficient authentication"
)


def make_device(source):
    return SimpleNamespace(
        address="00:11:22:33:44:55", name="Daikin", details={"source": source}
    )


def make_client(pair_exc=None):
    client = AsyncMock()
    client.is_connected = True
    client.pair = AsyncMock(side_effect=pair_exc)
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def make_connection(candidates):
    return Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=lambda: list(candidates),
    )


def patch_settle_sleep():
    """Mock out the 1.5s post-pairing settle delay to keep the suite fast."""
    return patch("pymadoka.connection.asyncio.sleep", AsyncMock())


async def drain_pending_tasks():
    """Yield to the loop so scheduled disconnect tasks can run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_candidate_wins():
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=good)) as est, patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_A"
    assert est.await_count == 1  # never touched PROXY_B


@pytest.mark.asyncio
async def test_auth_failure_falls_through_to_next_candidate():
    bad = make_client(pair_exc=AUTH_FAIL)
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[bad, good])), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_B"
    await drain_pending_tasks()
    bad.disconnect.assert_awaited()  # failed path is not left half-open


@pytest.mark.asyncio
async def test_pair_timeout_counts_as_auth_failure():
    # Task 2 narrowed is_pairing_error to marker-only: a TimeoutError raised BY
    # pair() must be treated as a pairing failure AT THE CALL SITE.
    slow = make_client(pair_exc=TimeoutError())
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[slow, good])), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_B"
    await drain_pending_tasks()


@pytest.mark.asyncio
async def test_all_candidates_auth_fail_raises_pairing_required():
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[make_client(pair_exc=AUTH_FAIL),
                                      make_client(pair_exc=AUTH_FAIL)])):
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        with pytest.raises(PairingRequiredError) as ei:
            await conn._connect_via_ha()
    assert ei.value.tried_sources == ["PROXY_A", "PROXY_B"]
    assert isinstance(conn.last_error, PairingRequiredError)
    await drain_pending_tasks()


@pytest.mark.asyncio
async def test_empty_candidates_raises_unreachable():
    conn = make_connection([])
    with pytest.raises(DeviceUnreachableError):
        await conn._connect_via_ha()
    assert isinstance(conn.last_error, DeviceUnreachableError)


@pytest.mark.asyncio
async def test_broken_candidates_callback_degrades_to_single_path():
    # An integration-side bug in the callback must NOT kill the connect loop:
    # it degrades to the legacy single-device path.
    conn = Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=lambda: 1 / 0,
    )
    with patch.object(conn, "_connect_via_ha_single",
                      AsyncMock()) as single:
        await conn._connect_via_ha()
    single.assert_awaited_once()
