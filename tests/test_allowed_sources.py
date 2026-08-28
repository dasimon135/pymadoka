"""The allowed-sources guard: never pair on a path the caller did not sanction.

Under Home Assistant the BLEDevice handed to establish_connection is advisory:
habluetooth keeps only the address and re-scores every path at connect time. So
filtering the CANDIDATE list cannot stop a connect from landing on a proxy that
holds no bond — and pairing there puts a numeric-comparison prompt on the
thermostat screen that no unattended retry can ever answer.

These tests pin the guard that closes that hole by checking the path actually
used, after the link is up and before pair() is called.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError

from pymadoka import Controller
from pymadoka.connection import UNBONDED_PATH_ROUNDS, Connection
from pymadoka.errors import PairingRequiredError

AUTH_FAIL = BleakError(
    "Bluetooth GATT Error address=00:11:22:33:44:55 handle=515 "
    "error=5 description=Insufficient authentication"
)


def make_device(source):
    return SimpleNamespace(
        address="00:11:22:33:44:55", name="Daikin", details={"source": source}
    )


def make_client(real_source=None, pair_exc=None):
    """A connected client that names `real_source` as the path that carried it.

    real_source=None leaves the backend unable to name the path, which is the
    local-adapter / plain-BleakClient case.
    """
    client = AsyncMock()
    client.is_connected = True
    client.pair = AsyncMock(side_effect=pair_exc)
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client._connected_scanner = (
        SimpleNamespace(source=real_source) if real_source else None
    )
    return client


def make_connection(candidates, allowed=None):
    return Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False, hass=object(),
        candidates_callback=lambda: list(candidates),
        allowed_sources_callback=(None if allowed is None else (lambda: allowed)),
    )


def patch_settle_sleep():
    return patch("pymadoka.connection.asyncio.sleep", AsyncMock())


# --------------------------------------------------------------------------
# The guard itself
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_is_never_called_when_ha_lands_on_a_disallowed_path():
    """The whole point: no SMP request, so no prompt on the thermostat."""
    # We offer PROXY_A, HA connects via PROXY_B, which holds no bond.
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        await conn._connect_via_ha()
    client.pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallowed_path_is_disconnected_before_the_next_candidate():
    """The BRC1H accepts a single central; a leaked link blocks every retry."""
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        await conn._connect_via_ha()
    client.disconnect.assert_awaited()
    assert conn.connected_source is None


@pytest.mark.asyncio
async def test_allowed_path_pairs_normally():
    client = make_client(real_source="PROXY_A")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()
    assert conn.connected_source == "PROXY_A"


@pytest.mark.asyncio
async def test_guard_uses_the_real_path_not_the_offered_one():
    """Offering a disallowed device is fine when HA re-routes to an allowed one.

    The mirror image of the main case, and the reason the guard reads the real
    path instead of trusting our own candidate list.
    """
    client = make_client(real_source="PROXY_A")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_B")], allowed=["PROXY_A"])
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()
    assert conn.connected_source == "PROXY_A"


# --------------------------------------------------------------------------
# Fail open: the guard must never be the reason a device cannot connect
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_callback_leaves_every_path_allowed():
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])  # no allowed callback
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_allowed_set_is_unrestricted():
    """Empty means "no bond on record yet" — a fresh install must still pair."""
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=[])
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreadable_real_path_is_allowed():
    """A local adapter never names a scanner; refusing it would strand it."""
    client = make_client(real_source=None)
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_that_raises_does_not_block_connecting():
    client = make_client(real_source="PROXY_B")
    conn = Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False, hass=object(),
        candidates_callback=lambda: [make_device("PROXY_A")],
        allowed_sources_callback=lambda: 1 / 0,
    )
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        await conn._connect_via_ha()
    client.pair.assert_awaited_once()


# --------------------------------------------------------------------------
# The escape hatch: a round where EVERY path was disallowed
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_all_disallowed_round_does_not_accuse_anyone():
    """Scores fluctuate; the next poll may well land on a bonded proxy."""
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        await conn._connect_via_ha()  # must not raise
    assert conn.last_error is None


@pytest.mark.asyncio
async def test_repeated_all_disallowed_rounds_ask_for_a_re_pair():
    """Only a human can fix this, so eventually it has to be said out loud."""
    client = make_client(real_source="PROXY_B")
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=client)), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        for _ in range(UNBONDED_PATH_ROUNDS - 1):
            await conn._connect_via_ha()
        with pytest.raises(PairingRequiredError) as excinfo:
            await conn._connect_via_ha()
    assert excinfo.value.reason == "unbonded_path"
    # Names the proxy that keeps winning the score, which is the one the user
    # has to walk over and pair with.
    assert excinfo.value.evidence == {"PROXY_B": "unbonded"}
    # And it got there without ever putting a prompt on the screen.
    client.pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_successful_connect_clears_the_unbonded_streak():
    disallowed = make_client(real_source="PROXY_B")
    allowed = make_client(real_source="PROXY_A")
    with patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")], allowed=["PROXY_A"])
        with patch("bleak_retry_connector.establish_connection",
                   AsyncMock(return_value=disallowed)):
            for _ in range(UNBONDED_PATH_ROUNDS - 1):
                await conn._connect_via_ha()
        with patch("bleak_retry_connector.establish_connection",
                   AsyncMock(return_value=allowed)):
            await conn._connect_via_ha()
        # Streak forgotten: a fresh run of disallowed rounds must be needed.
        with patch("bleak_retry_connector.establish_connection",
                   AsyncMock(return_value=disallowed)):
            await conn._connect_via_ha()  # must not raise


@pytest.mark.asyncio
async def test_a_disallowed_path_is_not_counted_as_a_refusal():
    """A skipped path proves nothing about any bond — it was never asked.

    Collapsing the two would let the guard itself manufacture the "every path
    refused the bond" verdict that evicts bonds and summons the user.
    """
    disallowed = make_client(real_source="PROXY_B")
    refusing = make_client(real_source="PROXY_C", pair_exc=AUTH_FAIL)
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[disallowed, refusing])), patch_settle_sleep():
        conn = make_connection(
            [make_device("PROXY_B"), make_device("PROXY_C")],
            allowed=["PROXY_C"],
        )
        await conn._connect_via_ha()  # mixed round: must not raise
    assert conn.last_error is None
    assert conn.rejected_sources == frozenset({"PROXY_C"})


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_controller_forwards_the_allowed_sources_callback():
    marker = lambda: ["PROXY_A"]  # noqa: E731
    ctrl = Controller("00:11:22:33:44:55", allowed_sources_callback=marker)
    assert ctrl.connection.allowed_sources_callback is marker


def test_allowed_sources_callback_defaults_to_none():
    ctrl = Controller("00:11:22:33:44:55")
    assert ctrl.connection.allowed_sources_callback is None
