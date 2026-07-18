import pytest
from bleak.exc import BleakError

from pymadoka.errors import (
    MadokaError,
    PairingRequiredError,
    DeviceUnreachableError,
    is_pairing_error,
)
from pymadoka.connection import ConnectionException


def test_hierarchy():
    assert issubclass(ConnectionException, MadokaError)
    assert issubclass(PairingRequiredError, MadokaError)
    assert issubclass(DeviceUnreachableError, MadokaError)


def test_pairing_required_carries_context():
    err = PairingRequiredError(
        "F0:B3:1E:87:AF:FE", tried_sources=["AA:BB:CC:DD:EE:01", None]
    )
    assert err.address == "F0:B3:1E:87:AF:FE"
    assert err.tried_sources == ["AA:BB:CC:DD:EE:01", None]
    assert "F0:B3:1E:87:AF:FE" in str(err)


def test_device_unreachable_carries_address():
    err = DeviceUnreachableError("F0:B3:1E:87:AF:FE")
    assert err.address == "F0:B3:1E:87:AF:FE"
    assert "F0:B3:1E:87:AF:FE" in str(err)


def test_pairing_required_renders_none_source_as_local_adapter():
    err = PairingRequiredError("F0:B3:1E:87:AF:FE", tried_sources=[None])
    assert "local adapter" in str(err)
    assert "None" not in str(err)


@pytest.mark.parametrize(
    "exc,expected",
    [
        # ESPHome proxy GATT rejection (exact shape seen in HA logs)
        (BleakError(
            "Bluetooth GATT Error address=F0:B3:1E:87:AF:FE handle=515 "
            "error=5 description=Insufficient authentication"), True),
        # esp32_ble_client pairing failure (error 97 seen in HA logs)
        (BleakError("Pairing failed due to error: 97"), True),
        (BleakError("Insufficient Encryption"), True),
        # ATT error 0x05 at the very end of the message (no trailing space)
        (BleakError(
            "Bluetooth GATT Error address=X handle=515 error=5"), True),
        # BlueZ local-adapter rejection (AuthenticationRejected/Canceled/...)
        (BleakError("org.bluez.Error.AuthenticationRejected"), True),
        # NOT pairing problems:
        # Marker-only contract: a bare TimeoutError is NOT classified here.
        # Only the pair() call site knows a timeout means an unanswered
        # prompt; it is responsible for treating its own TimeoutError as a
        # pairing failure (Task 4 call-site contract).
        (TimeoutError(), False),
        # "error=51" must not be mistaken for ATT error 5
        (BleakError("error=51 something"), False),
        (BleakError("Device disconnected"), False),
        (BleakError("No backend with an available connection slot"), False),
        (ConnectionError("boom"), False),
    ],
)
def test_is_pairing_error(exc, expected):
    assert is_pairing_error(exc) is expected
