"""Unit tests for the Ventilation codec and the controller's feature choice.

Function 0x0031 was reverse-engineered by @Frank802 on a VAM350J8VEB; no VAM is
available to run these against real hardware, so everything here is pinned to
the bytes in his capture rather than to a live unit.
"""

import pytest

from pymadoka.controller import (
    DEVICE_TYPE_THERMOSTAT,
    DEVICE_TYPE_VENTILATION,
    Controller,
)
from pymadoka.features.fanspeed import FanSpeedEnum
from pymadoka.features.ventilation import (
    Ventilation,
    VentilationModeEnum,
    VentilationStatus,
)

# The captured frame, arguments only:
#   12 01 07   supported modes = auto | heat exchange | bypass
#   13 01 08 / 15 01 10 / 16 01 10   not modelled
#   20 01 00   mode = AUTO
#   21 01 01   fan speed = LOW
CAPTURED = {
    0x12: bytearray([0x07]),
    0x13: bytearray([0x08]),
    0x15: bytearray([0x10]),
    0x16: bytearray([0x10]),
    0x20: bytearray([0x00]),
    0x21: bytearray([0x01]),
}


def test_parses_the_captured_frame():
    status = VentilationStatus()
    status.set_values(CAPTURED)

    assert status.ventilation_mode is VentilationModeEnum.AUTO
    assert status.fan_speed is FanSpeedEnum.LOW
    assert status.supported_modes == 0x07


def test_unmodelled_arguments_are_ignored_not_fatal():
    """A byte we do not model must not abort the poll for everything else."""
    status = VentilationStatus()
    status.set_values(CAPTURED | {0x99: bytearray([0xFF])})

    assert status.ventilation_mode is VentilationModeEnum.AUTO


def test_unknown_mode_value_becomes_none_rather_than_raising():
    status = VentilationStatus()
    status.set_values({0x20: bytearray([0x7F])})

    assert status.ventilation_mode is None


def test_mid_is_widened_the_way_fanspeed_widens_it():
    """0x0021 uses 0x0050's encoding, where 2..4 all mean MID."""
    for raw in (2, 3, 4):
        status = VentilationStatus()
        status.set_values({0x21: bytearray([raw])})
        assert status.fan_speed is FanSpeedEnum.MID


def test_unknown_fan_speed_becomes_none():
    status = VentilationStatus()
    status.set_values({0x21: bytearray([0x63])})

    assert status.fan_speed is None


def test_a_write_serializes_only_the_argument_it_sets():
    """The unit applies whatever it is sent, so a companion argument would
    silently overwrite a value the caller never meant to touch."""
    values = VentilationStatus(fan_speed=FanSpeedEnum.HIGH).get_values()

    assert values == {0x21: bytearray([0x05])}


def test_a_mode_write_carries_no_fan_speed():
    values = VentilationStatus(
        ventilation_mode=VentilationModeEnum.BYPASS
    ).get_values()

    assert values == {0x20: bytearray([0x02])}


def test_supported_modes_bitmask_is_read_per_mode():
    status = VentilationStatus()
    status.set_values({0x12: bytearray([0x03])})  # auto | heat exchange

    assert status.supports_mode(VentilationModeEnum.AUTO)
    assert status.supports_mode(VentilationModeEnum.HEAT_EXCHANGE)
    assert not status.supports_mode(VentilationModeEnum.BYPASS)


def test_an_unreported_bitmask_assumes_every_mode_is_supported():
    """Argument 0x12 is not guaranteed to be in every answer; refusing every
    mode because it was absent would leave the unit with no preset at all."""
    status = VentilationStatus()

    assert status.supports_mode(VentilationModeEnum.BYPASS)


def _ventilation_with_status(status):
    """A Ventilation whose base-class update() writes nothing to a device.

    Feature.update() needs a live connection; what is under test here is only
    what Ventilation.update() does around it.
    """
    feature = Ventilation.__new__(Ventilation)
    feature.status = status
    return feature


@pytest.mark.asyncio
async def test_update_keeps_the_fields_it_did_not_write(monkeypatch):
    """The base class replaces status wholesale with what was written, which
    would blank the untouched fields until the next poll and make a consumer
    reading in between see them flap to unknown."""

    async def fake_update(self, update_status):
        self.status = update_status
        return update_status

    monkeypatch.setattr("pymadoka.feature.Feature.update", fake_update)

    previous = VentilationStatus(
        ventilation_mode=VentilationModeEnum.BYPASS, fan_speed=FanSpeedEnum.LOW
    )
    previous.supported_modes = 0x07
    feature = _ventilation_with_status(previous)

    result = await feature.update(VentilationStatus(fan_speed=FanSpeedEnum.HIGH))

    assert result.fan_speed is FanSpeedEnum.HIGH
    assert result.ventilation_mode is VentilationModeEnum.BYPASS
    assert result.supported_modes == 0x07


@pytest.mark.asyncio
async def test_update_with_no_previous_status_keeps_only_what_was_written(
    monkeypatch,
):
    async def fake_update(self, update_status):
        self.status = update_status
        return update_status

    monkeypatch.setattr("pymadoka.feature.Feature.update", fake_update)

    feature = _ventilation_with_status(None)

    result = await feature.update(VentilationStatus(fan_speed=FanSpeedEnum.HIGH))

    assert result.fan_speed is FanSpeedEnum.HIGH
    assert result.ventilation_mode is None


def _controller(device_type):
    return Controller("00:11:22:33:44:55", device_type=device_type)


def test_a_ventilation_unit_gets_ventilation_and_not_fan_speed():
    """0x0050 on a VAM answers with zero-length arguments that never change:
    polling it is a round trip per poll for nothing."""
    controller = _controller(DEVICE_TYPE_VENTILATION)

    assert isinstance(controller.ventilation, Ventilation)
    assert not hasattr(controller, "fan_speed")


def test_a_thermostat_gets_fan_speed_and_not_ventilation():
    controller = _controller(DEVICE_TYPE_THERMOSTAT)

    assert hasattr(controller, "fan_speed")
    assert not hasattr(controller, "ventilation")


def test_the_default_is_a_thermostat():
    """Every release before this one had no device_type at all."""
    controller = Controller("00:11:22:33:44:55")

    assert controller.device_type == DEVICE_TYPE_THERMOSTAT
    assert hasattr(controller, "fan_speed")


def test_an_unknown_device_type_is_treated_as_a_thermostat():
    controller = _controller("something-new")

    assert hasattr(controller, "fan_speed")
    assert not hasattr(controller, "ventilation")


def test_device_type_is_not_polled_as_a_feature():
    """update() walks vars(self); a plain string attribute must not be picked
    up as something to query."""
    from pymadoka.feature import Feature

    controller = _controller(DEVICE_TYPE_VENTILATION)
    features = [v for v in vars(controller).values() if isinstance(v, Feature)]

    assert controller.device_type not in features
    assert any(isinstance(f, Ventilation) for f in features)
