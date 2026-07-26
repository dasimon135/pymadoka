"""PairingRequiredError must say WHY it was raised.

The same exception class is raised from two epistemically different places:

* a path that EXPLICITLY REFUSED the authenticated bond — proof, a human has
  to re-pair;
* every path merely TIMING OUT for PAIRING_TIMEOUT_ROUNDS consecutive rounds —
  an inference, which congestion alone produces on a perfectly valid bond.

Before 0.3.10 both carried the same attributes and the same message ("refused
the authenticated bond"), so a consumer could only tell them apart through an
undocumented side channel (whether the round counter happened to be reset).
The first claim is actionable, the second one is false on a bonded path — and
acting on it costs a spurious pairing prompt on the thermostat screen, which
repeated jams the BRC1H's SMP stack.
"""

import sys
import types
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
TRANSIENT = BleakError("Device disconnected")


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


# --------------------------------------------------------------------------
# The exception itself
# --------------------------------------------------------------------------


def test_reason_defaults_to_rejected():
    """Constructor compatibility: existing call sites keep the old meaning."""
    err = PairingRequiredError("F0:B3:1E:87:AF:FE", tried_sources=[None])
    assert err.reason == "rejected"
    assert err.timeout_rounds == 0
    assert err.evidence == {}
    assert "refused the authenticated bond" in str(err)


def test_timeout_streak_message_does_not_claim_a_refusal():
    err = PairingRequiredError(
        "F0:B3:1E:87:AF:FE",
        tried_sources=["AA:BB:CC:DD:EE:01"],
        reason="timeout_streak",
        timeout_rounds=3,
    )
    assert err.reason == "timeout_streak"
    assert err.timeout_rounds == 3
    text = str(err)
    assert "refused" not in text
    assert "did not complete" in text
    assert "F0:B3:1E:87:AF:FE" in text
    assert "AA:BB:CC:DD:EE:01" in text


def test_evidence_is_carried_per_source():
    err = PairingRequiredError(
        "F0:B3:1E:87:AF:FE",
        tried_sources=["AA:BB:CC:DD:EE:01", None],
        evidence={"AA:BB:CC:DD:EE:01": "rejected", None: "timeout"},
    )
    assert err.evidence == {"AA:BB:CC:DD:EE:01": "rejected", None: "timeout"}


# --------------------------------------------------------------------------
# The two raise sites
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_raise_reports_reason_rejected():
    clients = [make_client(pair_exc=AUTH_FAIL), make_client(pair_exc=TimeoutError())]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        with pytest.raises(PairingRequiredError) as excinfo:
            await conn._connect_via_ha()

    err = excinfo.value
    assert err.reason == "rejected"
    assert err.timeout_rounds == 0
    assert err.evidence == {"PROXY_A": "rejected", "PROXY_B": "timeout"}


@pytest.mark.asyncio
async def test_timeout_streak_raise_reports_reason_timeout_streak():
    clients = [make_client(pair_exc=TimeoutError()) for _ in range(PAIRING_TIMEOUT_ROUNDS)]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        for _ in range(PAIRING_TIMEOUT_ROUNDS - 1):
            await conn._connect_via_ha()
        with pytest.raises(PairingRequiredError) as excinfo:
            await conn._connect_via_ha()

    err = excinfo.value
    assert err.reason == "timeout_streak"
    assert err.timeout_rounds == PAIRING_TIMEOUT_ROUNDS
    assert err.evidence == {"PROXY_A": "timeout"}
    assert "refused" not in str(err)


# --------------------------------------------------------------------------
# Defect: the streak was not reset at its own raise site
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streak_raise_resets_the_counter():
    """Otherwise 4 >= 3 re-raises instantly on the next classified timeout.

    The rejection site already resets; the asymmetry meant a user-initiated
    retry after the accusation was convicted again by a SINGLE timed-out
    round instead of getting a fresh budget of PAIRING_TIMEOUT_ROUNDS.
    """
    clients = [make_client(pair_exc=TimeoutError()) for _ in range(PAIRING_TIMEOUT_ROUNDS + 1)]
    with patch_connect(clients), patch_settle_sleep():
        conn = make_connection([make_device("PROXY_A")])
        for _ in range(PAIRING_TIMEOUT_ROUNDS - 1):
            await conn._connect_via_ha()
        with pytest.raises(PairingRequiredError):
            await conn._connect_via_ha()
        assert conn.pairing_timeout_rounds == 0

        # One more all-timeout round must NOT raise again.
        await conn._connect_via_ha()

    assert conn.pairing_timeout_rounds == 1


# --------------------------------------------------------------------------
# Defect: the fallback path's success did not forgive the streak
# --------------------------------------------------------------------------


def _install_fake_ha_bluetooth(monkeypatch, ble_device):
    # The suite must run WITHOUT homeassistant installed (the import in
    # _connect_via_ha_single is function-local for exactly that reason), so
    # stub the module hierarchy rather than patch("homeassistant...").
    bt = types.ModuleType("homeassistant.components.bluetooth")
    bt.async_ble_device_from_address = lambda hass, address, connectable=True: ble_device
    monkeypatch.setitem(sys.modules, "homeassistant", types.ModuleType("homeassistant"))
    monkeypatch.setitem(
        sys.modules, "homeassistant.components",
        types.ModuleType("homeassistant.components"),
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bt)


@pytest.mark.asyncio
async def test_single_device_path_success_resets_the_streak(monkeypatch):
    """Only the candidates path forgave; a device recovering through the
    fallback kept a stale streak armed and was convicted by the next round."""
    conn = Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False, hass=object(),
    )
    conn.resume_pairing_timeout_rounds(2)
    _install_fake_ha_bluetooth(monkeypatch, make_device("PROXY_A"))
    with patch(
        "bleak_retry_connector.establish_connection",
        AsyncMock(return_value=make_client()),
    ), patch_settle_sleep():
        await conn._connect_via_ha_single()

    assert conn.connection_status is ConnectionStatus.CONNECTED
    assert conn.pairing_timeout_rounds == 0


# --------------------------------------------------------------------------
# Defect: a mixed round threw proven rejections away
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_proven_rejection_survives_a_flapping_third_path():
    """2 rejections + 1 transient failure must not erase the rejections.

    With the evidence discarded every round, a proxy that fails transiently
    (and differently) each time postpones the legitimate conclusion forever.
    """
    devices = [make_device("PROXY_A"), make_device("PROXY_B"), make_device("PROXY_C")]

    # Round 1: A and B refuse, C fails transiently -> no verdict yet.
    round1 = [make_client(pair_exc=AUTH_FAIL), make_client(pair_exc=AUTH_FAIL), TRANSIENT]
    with patch_connect(round1), patch_settle_sleep():
        conn = make_connection(devices)
        await conn._connect_via_ha()
    assert conn.last_error is None

    # Round 2: A and B fail transiently now, C refuses. The retained proof for
    # A and B means the round as a whole is an authentication failure.
    round2 = [TRANSIENT, TRANSIENT, make_client(pair_exc=AUTH_FAIL)]
    with patch_connect(round2), patch_settle_sleep(), pytest.raises(
        PairingRequiredError
    ) as excinfo:
        await conn._connect_via_ha()

    err = excinfo.value
    assert err.reason == "rejected"
    assert err.evidence == {
        "PROXY_A": "rejected", "PROXY_B": "rejected", "PROXY_C": "rejected",
    }


@pytest.mark.asyncio
async def test_retained_rejection_is_cleared_by_a_success_on_that_path():
    """A path that authenticates is exonerated: proof must not outlive it."""
    devices = [make_device("PROXY_A")]
    with patch_connect([make_client(pair_exc=AUTH_FAIL)]), patch_settle_sleep():
        conn = make_connection(devices)
        with pytest.raises(PairingRequiredError):
            await conn._connect_via_ha()

    with patch_connect([make_client()]), patch_settle_sleep():
        await conn._connect_via_ha()
    assert conn.connection_status is ConnectionStatus.CONNECTED

    # A later purely transient round must not be convicted by stale evidence.
    with patch_connect([TRANSIENT]), patch_settle_sleep():
        conn.connection_status = ConnectionStatus.DISCONNECTED
        await conn._connect_via_ha()
    assert conn.last_error is None


@pytest.mark.asyncio
async def test_retained_evidence_never_convicts_a_never_rejected_path():
    """Conservative by construction: only PROVEN rejections are retained.

    A round of pure transient failures stays transient no matter how many
    rounds precede it.
    """
    devices = [make_device("PROXY_A"), make_device("PROXY_B")]
    conn = make_connection(devices)
    for _ in range(4):
        with patch_connect([TRANSIENT, TRANSIENT]), patch_settle_sleep():
            await conn._connect_via_ha()
    assert conn.last_error is None
    assert conn.pairing_timeout_rounds == 0


# --------------------------------------------------------------------------
# Public accessors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_streak_accessors():
    conn = make_connection([make_device("PROXY_A")])
    assert conn.pairing_timeout_rounds == 0
    conn.resume_pairing_timeout_rounds(2)
    assert conn.pairing_timeout_rounds == 2
    conn.resume_pairing_timeout_rounds(-5)  # clamped, never negative
    assert conn.pairing_timeout_rounds == 0
    conn.resume_pairing_timeout_rounds(2)
    conn.reset_pairing_timeout_rounds()
    assert conn.pairing_timeout_rounds == 0
