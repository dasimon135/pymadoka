"""Unit tests for the clean filter reset frame.

The BRC1H keeps its filter sign on when the reset command 0x4220 carries
argument 0xFE alone. The ESPHome
component of daikin_madoka sends both (madoka_base.cpp, reset_filter) and
clears the sign on the same thermostats where this library's 0xFE-only frame
did not (daikin_madoka#148, #74).
"""

from pymadoka.features.clean_filter import (
    ResetCleanFilterTimer,
    ResetCleanFilterTimerStatus,
)


def test_reset_frame_matches_the_esphome_component():
    # madoka_base.cpp: {0x51, 0x01, 0x01, 0xFE, 0x01, 0x01}
    frame = ResetCleanFilterTimerStatus().serialize()

    assert bytes(frame) == bytes([0x51, 0x01, 0x01, 0xFE, 0x01, 0x01])


def test_reset_uses_command_0x4220():
    assert ResetCleanFilterTimer(connection=None).update_cmd_id() == 0x4220
