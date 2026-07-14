"""Unit tests for the SetPoint status codec (parse/serialize)."""

from pymadoka.features.setpoint import SetPointStatus


def make_response(status: SetPointStatus) -> bytearray:
    """Wrap serialized params in a full response message.

    Layout: size(1) + cmd_id(3) + params. The first byte holds the total
    message length, as produced by the device after chunk reassembly.
    """
    params = status.serialize()
    message = bytearray([0x00, 0x00, 0x00, 0x40]) + params
    message[0] = len(message)
    return message


def test_serialize_uses_128_multiplier_encoding():
    status = SetPointStatus(25, 22)
    values = status.get_values()

    assert values[SetPointStatus.COOLING_IDX[0]] == (25 * 128).to_bytes(2, "big")
    assert values[SetPointStatus.HEATING_IDX[0]] == (22 * 128).to_bytes(2, "big")


def test_parse_serialize_round_trip():
    original = SetPointStatus(25, 22)

    parsed = SetPointStatus(0, 0)
    parsed.parse(make_response(original))

    assert parsed.cooling_set_point == 25
    assert parsed.heating_set_point == 22


def test_parse_reads_device_limits():
    """The device reports setpoint limits; parse must surface them."""
    original = SetPointStatus(24, 20)
    values = original.get_values()
    values[SetPointStatus.COOLING_LOWERLIMIT_IDX[0]] = (18 * 128).to_bytes(2, "big")
    values[SetPointStatus.HEATING_UPPERLIMIT_IDX[0]] = (30 * 128).to_bytes(2, "big")
    values[SetPointStatus.RANGE_ENABLED_IDX[0]] = (1 * 128).to_bytes(1, "big")

    # Re-serialize with the tweaked params.
    out = bytearray()
    for k, v in values.items():
        out.append(k)
        out.append(len(v))
        out.extend(v)
    message = bytearray([0x00, 0x00, 0x00, 0x40]) + out
    message[0] = len(message)

    parsed = SetPointStatus(0, 0)
    parsed.parse(message)

    assert parsed.cooling_lowerlimit == 18
    assert parsed.heating_upperlimit == 30
    assert parsed.range_enabled == 1
