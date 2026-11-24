"""Unit status thermistors (functions 0x4111 and 0x0111), daikin_madoka#162.

Frames come from the HCI capture attached to #162 (BRC1H, FBA100A2VEB
indoor, AZAS100M7V1B outdoor), 2026-11-19 22:11.
"""

import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest

from pymadoka.feature import NotImplementedException
from pymadoka.features.unit_thermistors import (
    INDOOR_IDS,
    OUTDOOR_IDS,
    UnitThermistors,
    _PageStatus,
)

PENDING = None  # marker: argument answered with length 0xFF


def frame(cmd: int, *args: tuple[int, bytes | None]) -> bytes:
    """A rebuilt answer: size, cmd id (3 bytes), then id/len/value."""
    body = bytearray([0x00, cmd >> 8, cmd & 0xFF])
    for arg_id, value in args:
        if value is PENDING:
            body += bytes([arg_id, 0xFF])
        else:
            body += bytes([arg_id, len(value)]) + value
    return bytes([len(body) + 1]) + bytes(body)


def page(page_no: int, values: dict[int, bytes | None]) -> bytes:
    return frame(0x0111, (0x01, bytes([page_no])), (0x02, b"\x00"), *values.items())


INDOOR_PENDING = page(0, {i: PENDING for i in INDOOR_IDS})
INDOOR_VALUES = page(0, {
    0x40: b"OFF", 0x41: b"\x00\x00", 0x42: b"\x00", 0x43: b"\x00\x00",
    0x44: b"\x00", 0x45: b"\x00", 0x46: b"\x00", 0x47: b"\x00",
    0x48: b"\x00", 0x49: b"\x01", 0x4A: b"\x00\x0e", 0x4B: b"\x00\x0e",
    0x4C: b"\x00\x0d", 0x4D: b"\xff\xff", 0x4E: b"\xff\xff", 0x4F: b"\xff\xff",
})
OUTDOOR_PENDING = page(1, {i: PENDING for i in OUTDOOR_IDS})
OUTDOOR_VALUES = page(1, {
    0x40: b"\x00\x00", 0x41: b"\x00\x00\x00", 0x43: b"\x00\x00", 0x44: b"\x00",
    0x45: b"\x01\x05", 0x46: b"\x01\x0d", 0x4A: b"\x00\x0a", 0x4B: b"\x00\x0d",
    0x4C: b"\x00\x25",
})
MONITOR_ACK = frame(0x4111, (0xFE, b"\x00"))


def parsed(data: bytes) -> _PageStatus:
    status = _PageStatus(0, INDOOR_IDS)
    status.parse(bytearray(data))
    return status


def test_the_indoor_request_is_the_app_frame():
    assert bytes(_PageStatus(0, INDOOR_IDS).serialize()) == bytes(
        [0x01, 0x01, 0x00, 0x02, 0x01, 0x00]
        + [b for i in range(0x40, 0x50) for b in (i, 0x00)]
    )


def test_the_outdoor_request_asks_only_the_arguments_the_page_has():
    assert bytes(_PageStatus(1, OUTDOOR_IDS).serialize()) == bytes(
        [0x01, 0x01, 0x01, 0x02, 0x01, 0x00]
        + [b for i in (0x40, 0x41, 0x43, 0x44, 0x45, 0x46, 0x4A, 0x4B, 0x4C) for b in (i, 0x00)]
    )


def test_an_answer_of_0xff_lengths_is_pending():
    assert parsed(INDOOR_PENDING).pending is True


def test_values_are_signed_big_endian_degrees():
    status = parsed(INDOOR_VALUES)
    assert status.pending is False
    assert status.temperature(0x4A) == 14
    assert status.temperature(0x4C) == 13
    assert parsed(page(0, {0x4A: b"\xff\xfb"})).temperature(0x4A) == -5


def test_ffff_means_not_fitted():
    assert parsed(INDOOR_VALUES).temperature(0x4D) is None


def test_bit_14_is_a_flag_not_part_of_the_temperature():
    """Four reads on the #162 reporter's unit over three days, none during a
    defrost; with bit 14 cleared each one matched the reads around it."""
    for raw, degrees in (
        (b"\x40\x06", 6),
        (b"\x40\x02", 2),
        (b"\x40\x00", 0),
        (b"\x40\x0c", 12),
    ):
        assert parsed(page(0, {0x4B: raw})).temperature(0x4B) == degrees


def test_an_empty_page_is_not_pending_and_has_no_values():
    status = parsed(page(1, {i: b"" for i in OUTDOOR_IDS}))
    assert status.pending is False
    assert status.temperature(0x4A) is None


def feature(*answers) -> tuple[UnitThermistors, AsyncMock]:
    thermistors = UnitThermistors(connection=None)
    send = AsyncMock(side_effect=list(answers))
    thermistors._send_command = send
    return thermistors, send


START = call(0x4111, bytearray([0xFE, 0x01, 0x01]))
STOP = call(0x4111, bytearray([0xFE, 0x01, 0x00]))


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("pymadoka.features.unit_thermistors.asyncio.sleep", AsyncMock()) as sleep:
        yield sleep


@pytest.mark.asyncio
async def test_a_read_plays_the_app_sequence():
    thermistors, send = feature(
        MONITOR_ACK, INDOOR_PENDING, INDOOR_PENDING, INDOOR_VALUES,
        OUTDOOR_PENDING, OUTDOOR_VALUES, MONITOR_ACK,
    )

    status = await thermistors.query()

    assert send.await_args_list[0] == START
    assert send.await_args_list[-1] == STOP
    assert [c.args[1][2] for c in send.await_args_list[1:-1]] == [0, 0, 0, 1, 1]
    assert (status.indoor_suction, status.indoor_liquid_pipe, status.indoor_gas_pipe) == (14, 14, 13)
    assert (status.outdoor_air, status.outdoor_heat_exchanger, status.outdoor_discharge) == (10, 13, 37)
    assert thermistors.status is status


@pytest.mark.asyncio
async def test_an_empty_outdoor_page_leaves_the_outdoor_values_empty():
    empty = page(1, {i: b"" for i in OUTDOOR_IDS})
    thermistors, _ = feature(MONITOR_ACK, INDOOR_VALUES, empty, MONITOR_ACK)

    status = await thermistors.query()

    assert status.indoor_suction == 14
    assert status.outdoor_air is None


@pytest.mark.asyncio
async def test_a_page_that_never_fills_gives_up_after_six_answers_and_stops():
    thermistors, send = feature(MONITOR_ACK, *[INDOOR_PENDING] * 6, MONITOR_ACK)

    with pytest.raises(asyncio.TimeoutError):
        await thermistors.query()

    assert send.await_count == 8
    assert send.await_args_list[-1] == STOP
    assert thermistors.status is None


@pytest.mark.asyncio
async def test_stop_is_sent_after_an_error_and_its_own_failure_is_ignored():
    thermistors, send = feature(MONITOR_ACK, ConnectionError("gone"), ConnectionError("gone"))

    with pytest.raises(ConnectionError):
        await thermistors.query()

    assert send.await_args_list[-1] == STOP


def test_it_cannot_be_written():
    with pytest.raises(NotImplementedException):
        UnitThermistors(connection=None).update_cmd_id()
