"""Decoding of the indoor/outdoor temperatures (function 0x0110).

The outdoor value is one byte. 0xFF means the unit reports no outdoor sensor.
Below zero the protocol's reverse engineer decodes it as sign and magnitude
(bit 7 set = negative, low seven bits = degrees): see the OpenHAB binding,
bundles/org.openhab.binding.bluetooth.daikinmadoka, command
GetIndoorOutoorTemperatures.java, lines 66-75. Read as a plain unsigned byte,
-5 C came out as 133 C. No capture below 0 C has confirmed the rule yet.
"""

import pytest

from pymadoka.features.temperatures import TemperaturesStatus


def _decode(indoor: int, outdoor: int) -> TemperaturesStatus:
    status = TemperaturesStatus(0, 0)
    status.set_values({0x40: bytearray([indoor]), 0x41: bytearray([outdoor])})
    return status


@pytest.mark.parametrize(
    ("raw", "celsius"),
    [(0x00, 0), (0x12, 18), (0x7F, 127), (0x81, -1), (0x85, -5), (0x94, -20)],
)
def test_outdoor_is_sign_and_magnitude(raw: int, celsius: int) -> None:
    assert _decode(21, raw).outdoor == celsius


def test_outdoor_0xff_means_no_outdoor_sensor() -> None:
    assert _decode(21, 0xFF).outdoor is None


def test_indoor_is_unchanged() -> None:
    assert _decode(23, 0x05).indoor == 23
