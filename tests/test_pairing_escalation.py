"""A pair() timeout must not be mistaken for a missing bond.

Field incident 2026-07-20: a Home Assistant restart reconnects every
thermostat at once through the same proxies. Under that contention the SMP
encryption of an ALREADY VALID bond regularly exceeds the 8s pair() budget on
every candidate path. The library read "every path failed to pair" as
PairingRequiredError, which put a pairing prompt on the thermostat screen and
— repeated every poll — jammed the BRC1H's SMP stack until the user toggled
its Bluetooth off/on.

Proof the timeouts were transient: in the same restart, "Madoka parents"
timed out on proxy D0:CF:13:0E:C9:2A at 08:50:23 and connected successfully
through that very proxy at 08:51:46, with nobody touching the thermostat.

So a timeout is AMBIGUOUS and must be retried; only an explicit
authentication rejection is proof of a missing bond.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError

from pymadoka.connection import (
    PAIRING_TIMEOUT_ROUNDS,
    Connection,
    ConnectionStatus,
)
from pymadoka.errors import PairingRequiredError

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
    return patch("pymadoka.connection.asyncio.sleep", AsyncMock())


def patch_connect(clients):
    return patch(
        "bleak_retry_connector.establish_connection", AsyncMock(side_effect=clients)
    )


@pytest.mark.asyncio
async def test_all_paths_timing_out_retries_instead_of_raising():
    """One round of all-timeouts is not proof: retry, don't accuse."""
    clients = [make_client(pair_exc=TimeoutError()) for _ in range(2)]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()  # must not raise

    assert conn.last_error is None
    assert conn.connection_status is not ConnectionStatus.ABORTED
    assert conn.pairing_timeout_rounds == 1


@pytest.mark.asyncio
async def test_repeated_all_timeout_rounds_escalate_to_pairing_required():
    """A genuinely unbonded device times out forever — surface it eventually.

    The prompt really is sitting unanswered on the screen, so after enough
    consecutive all-timeout rounds the user needs the actionable error.
    """
    clients = [make_client(pair_exc=TimeoutError()) for _ in range(PAIRING_TIMEOUT_ROUNDS)]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        for _ in range(PAIRING_TIMEOUT_ROUNDS - 1):
            await conn._connect_via_ha()

        with pytest.raises(PairingRequiredError):
            await conn._connect_via_ha()

    assert isinstance(conn.last_error, PairingRequiredError)


@pytest.mark.asyncio
async def test_hard_rejection_escalates_immediately_despite_timeouts():
    """An explicit auth rejection is unambiguous: no ambiguity rounds needed."""
    clients = [make_client(pair_exc=TimeoutError()), make_client(pair_exc=AUTH_FAIL)]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        with pytest.raises(PairingRequiredError):
            await conn._connect_via_ha()


@pytest.mark.asyncio
async def test_successful_connect_resets_the_ambiguity_counter():
    """The restart-contention case: round 1 times out, round 2 connects fine."""
    clients = [make_client(pair_exc=TimeoutError()), make_client()]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        await conn._connect_via_ha()
        assert conn.pairing_timeout_rounds == 1

        await conn._connect_via_ha()

    assert conn.connection_status is ConnectionStatus.CONNECTED
    assert conn.pairing_timeout_rounds == 0


@pytest.mark.asyncio
async def test_transient_non_auth_failure_does_not_count_as_ambiguity():
    """A plain disconnect is already handled as transient; keep it separate.

    Mixing it into the timeout counter would let ordinary link flakiness
    escalate into a pairing accusation.
    """
    clients = [BleakError("Device disconnected"), BleakError("Device disconnected")]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()

    assert conn.pairing_timeout_rounds == 0
    assert conn.last_error is None
