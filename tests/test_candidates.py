import asyncio
import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError

from pymadoka import Controller
from pymadoka.connection import Connection, ConnectionStatus
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
    """Mock out settle/backoff sleeps to keep the suite fast."""
    return patch("pymadoka.connection.asyncio.sleep", AsyncMock())


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
    bad.disconnect.assert_awaited()  # failed path is not left half-open


@pytest.mark.asyncio
async def test_pair_timeout_falls_through_to_next_candidate():
    # A TimeoutError raised BY pair() is recognised at the call site (marker
    # -only is_pairing_error cannot see it) and moves on to the next path.
    # Whether an all-timeout round means "unbonded" is decided separately —
    # see tests/test_pairing_escalation.py.
    slow = make_client(pair_exc=TimeoutError())
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[slow, good])), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_B"


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


@pytest.mark.asyncio
async def test_stale_disconnect_callback_does_not_clobber_live_connection():
    # Every candidate's client carries on_disconnect as disconnected_callback.
    # A failed candidate's late disconnect callback must NOT stamp
    # DISCONNECTED over a later candidate's live connection nor spawn a
    # competing reconnect (the BRC1H accepts a single central).
    bad = make_client(pair_exc=AUTH_FAIL)
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[bad, good])), patch_settle_sleep():
        conn = Connection(
            "00:11:22:33:44:55", adapter=None, reconnect=True,
            hass=object(),
            candidates_callback=lambda: [make_device("PROXY_A"),
                                         make_device("PROXY_B")],
        )
        await conn._connect_via_ha()
    assert conn.connection_status is ConnectionStatus.CONNECTED

    with patch.object(Connection, "start", AsyncMock()) as start_mock:
        conn.on_disconnect(bad)  # stale callback from the failed candidate
        await asyncio.sleep(0)
    assert conn.connection_status is ConnectionStatus.CONNECTED
    assert conn._paired is True
    start_mock.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_failures_keep_retrying_instead_of_pairing_required():
    # Auth failure on one path + transient failure on another is NOT proof
    # that pairing is required: leave the outer start() loop retrying.
    bad = make_client(pair_exc=AUTH_FAIL)
    transient = BleakError("Device disconnected")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[bad, transient])), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()  # must not raise
    assert conn.connection_status is not ConnectionStatus.ABORTED
    assert conn.last_error is None
    assert conn._retry_delay == 10.0  # backoff doubled for the next round


@pytest.mark.asyncio
async def test_start_propagates_pairing_required():
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client(pair_exc=AUTH_FAIL))), \
         patch("pymadoka.connection.asyncio.sleep", AsyncMock()):
        conn = make_connection([make_device("PROXY_A")])
        with pytest.raises(PairingRequiredError):
            await conn.start()


@pytest.mark.asyncio
async def test_start_propagates_device_unreachable():
    conn = make_connection([])
    with pytest.raises(DeviceUnreachableError):
        await conn.start()


@pytest.mark.asyncio
async def test_background_start_swallows_typed_errors(caplog):
    caplog.set_level(logging.WARNING)
    conn = make_connection([])
    await conn._background_start()          # must NOT raise
    assert isinstance(conn.last_error, DeviceUnreachableError)
    assert "gave up" in caplog.text


@pytest.mark.asyncio
async def test_disconnect_of_live_client_schedules_background_reconnect():
    # on_disconnect must route through _background_start (not bare start())
    # so a typed error in the reconnect can never become an unhandled
    # task exception.
    conn = make_connection([])
    live = make_client()
    conn.client = live
    conn.reconnect = True
    with patch.object(Connection, "_background_start", AsyncMock()) as bg:
        conn.on_disconnect(live)
        await asyncio.sleep(0)   # let the created task run
    bg.assert_awaited_once()


def _install_fake_ha_bluetooth(monkeypatch, ble_device):
    # The suite must run WITHOUT homeassistant installed (library contract:
    # the import in _connect_via_ha_single is function-local for exactly that
    # reason), so stub the module hierarchy instead of patch("homeassistant...")
    # which would import the real package. monkeypatch restores sys.modules
    # afterwards, so a real HA install on the dev machine is not clobbered.
    bt = types.ModuleType("homeassistant.components.bluetooth")
    bt.async_ble_device_from_address = lambda hass, address, connectable=True: ble_device
    components = types.ModuleType("homeassistant.components")
    ha = types.ModuleType("homeassistant")
    monkeypatch.setitem(sys.modules, "homeassistant", ha)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bt)


@pytest.mark.asyncio
async def test_single_path_success_clears_last_error(monkeypatch):
    # last_error invariant: "None after a successful connect" must hold on
    # the legacy single-device path too. Concrete path: PairingRequiredError
    # recorded -> user pairs -> retry degrades to the single path (broken
    # candidates_callback) -> connects fine -> the stale typed error must
    # not survive for the HA coordinator to read.
    conn = Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=None,
    )
    conn.last_error = DeviceUnreachableError("00:11:22:33:44:55")
    _install_fake_ha_bluetooth(monkeypatch, make_device(None))
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client())), \
         patch_settle_sleep():
        await conn._connect_via_ha_single()
    assert conn.connection_status is ConnectionStatus.CONNECTED
    assert conn.last_error is None


@pytest.mark.asyncio
async def test_candidate_connect_uses_single_attempt_per_path():
    # Field incident 2026-07-18: with max_attempts=2, habluetooth's client
    # wrapper rescores ALL paths on every connect attempt, so a transient
    # failure of attempt 1 (which also bumps that scanner's failure count)
    # made attempt 2 silently fail over to the strongest-RSSI — unbonded —
    # proxy, which then held the BRC1H's single central slot through SMP
    # auth timeouts. One establish_connection call must make exactly one
    # path decision; retries/failover belong to the candidate loop.
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client())) as est, patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        await conn._connect_via_ha()
    assert est.await_args.kwargs.get("max_attempts") == 1


@pytest.mark.asyncio
async def test_cleanup_resets_connected_source():
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client())), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_A"
    await conn.cleanup()
    assert conn.connected_source is None


@pytest.mark.asyncio
async def test_live_disconnect_resets_connected_source():
    conn = make_connection([])
    live = make_client()
    conn.client = live
    conn.connected_source = "PROXY_A"
    conn.on_disconnect(live)
    assert conn.connected_source is None


@pytest.mark.asyncio
async def test_stale_disconnect_keeps_connected_source():
    # A failed candidate's late disconnect callback must not clear the
    # source of the path that IS serving us.
    conn = make_connection([])
    conn.client = make_client()
    conn.connected_source = "PROXY_A"
    conn.on_disconnect(make_client())  # stale client, not self.client
    assert conn.connected_source == "PROXY_A"


@pytest.mark.asyncio
async def test_cleanup_quiesces_background_tasks():
    # cleanup() must cancel/await in-flight background tasks so a reconnect
    # racing past the _closing check cannot complete a connect AFTER cleanup
    # disconnected the client (single-central BRC1H: that would block the
    # next entry setup).
    conn = make_connection([])
    slow = asyncio.create_task(asyncio.sleep(30))  # real sleep, must be cancelled
    conn._bg_tasks.add(slow)
    slow.add_done_callback(conn._bg_tasks.discard)
    await conn.cleanup()
    assert slow.done()
    assert all(t.done() for t in conn._bg_tasks)
