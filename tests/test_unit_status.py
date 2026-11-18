"""Unit tests for the indoor unit status (function 0x0100, arguments 0x41,
0x50 and 0x90): defrost / hot start, error, warning, alarm and error code.

Requested in daikin_madoka#159 to start underfloor heating while a ducted unit
defrosts. Bit layout from isakLevinson/madoka (MIT), decoder.py.
"""

from pymadoka.controller import (
    DEVICE_TYPE_VENTILATION,
    Controller,
)
from pymadoka.features.unit_status import UnitStatus, UnitStatusStatus


def frame(*args: tuple[int, bytes]) -> bytes:
    """A rebuilt 0x0100 answer: size, cmd id (3 bytes), then id/len/value."""
    body = bytearray([0x00, 0x01, 0x00])
    for arg_id, value in args:
        body += bytes([arg_id, len(value)]) + value
    return bytes([len(body) + 1]) + bytes(body)


def parsed(*args: tuple[int, bytes]) -> UnitStatusStatus:
    status = UnitStatusStatus()
    status.parse(bytearray(frame(*args)))
    return status


def test_the_query_asks_function_0x0100_for_the_three_arguments():
    assert UnitStatus(connection=None).query_cmd_id() == 0x0100
    assert bytes(UnitStatusStatus().serialize()) == bytes(
        [0x41, 0x01, 0x00, 0x50, 0x01, 0x00, 0x90, 0x01, 0x00]
    )


def test_all_clear():
    status = parsed((0x41, b"\x00"), (0x50, b"\x00"), (0x90, b"\x00\x00\x00\x00"))

    assert status.defrost is False
    assert (status.error, status.warning, status.alarm) == (False, False, False)
    assert status.error_code is None


def test_defrost_is_bit_2_of_argument_0x41():
    assert parsed((0x41, b"\x04")).defrost is True
    # Quick start (bit 0), test operation (bit 1), duty rotation (bit 3).
    assert parsed((0x41, b"\x0b")).defrost is False


def test_error_warning_and_alarm_are_bits_0_1_2_of_argument_0x50():
    status = parsed((0x50, b"\x01"))
    assert (status.error, status.warning, status.alarm) == (True, False, False)
    status = parsed((0x50, b"\x02"))
    assert (status.error, status.warning, status.alarm) == (False, True, False)
    status = parsed((0x50, b"\x04"))
    assert (status.error, status.warning, status.alarm) == (False, False, True)


def test_the_error_code_reads_as_the_thermostat_shows_it():
    status = parsed((0x50, b"\x01"), (0x90, bytes([0x01, ord("U"), ord("4"), 1])))
    assert status.error_code == "U4-01"
    status = parsed((0x50, b"\x02"), (0x90, bytes([0x00, ord("A"), ord("6"), 0])))
    assert status.error_code == "A6-00"


def test_a_recorded_code_with_no_flag_set_is_not_a_current_error():
    # What four healthy BRC1H answered: all 0x50 flags clear, yet 0x90 still
    # holds the last recorded code.
    status = parsed((0x50, b"\x00"), (0x90, bytes([0x00, ord("U"), ord("0"), 0])))

    assert (status.error, status.warning, status.alarm) == (False, False, False)
    assert status.error_code is None
    assert status.last_error_code == "U0-00"


def test_no_flags_reported_means_no_current_code():
    status = parsed((0x90, bytes([0x00, ord("A"), ord("5"), 0])))

    assert status.error_code is None
    assert status.last_error_code == "A5-00"


def test_an_empty_or_short_error_code_is_no_error():
    assert parsed((0x50, b"\x01"), (0x90, b"")).error_code is None
    assert parsed((0x50, b"\x01"), (0x90, b"\x00\x00")).last_error_code is None


def test_an_argument_left_out_reads_as_unknown_not_as_clear():
    status = parsed((0x50, b"\x00"))

    assert status.defrost is None
    assert status.error is False
    assert status.error_code is None


def test_a_thermostat_polls_it_and_a_vam_does_not():
    assert isinstance(Controller("00:11:22:33:44:55").unit_status, UnitStatus)
    vam = Controller("00:11:22:33:44:55", device_type=DEVICE_TYPE_VENTILATION)
    assert not hasattr(vam, "unit_status")
