from pymadoka.errors import (
    MadokaError,
    PairingRequiredError,
    DeviceUnreachableError,
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
