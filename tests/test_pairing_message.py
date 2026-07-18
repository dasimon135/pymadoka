"""Unit tests for pairing failure diagnostics."""

from pymadoka.connection import pairing_failure_message

ADDRESS = "AA:BB:CC:DD:EE:FF"


def test_timeout_points_to_the_thermostat_prompt():
    """A pairing timeout means the prompt is waiting on the thermostat screen.

    str(TimeoutError()) is empty, so the generic message ended with a bare
    colon and gave no clue what to do next.
    """
    message = pairing_failure_message(ADDRESS, TimeoutError())

    assert ADDRESS in message
    assert "timed out" in message
    assert "thermostat" in message


def test_other_errors_keep_the_original_reason():
    message = pairing_failure_message(ADDRESS, RuntimeError("boom"))

    assert ADDRESS in message
    assert "boom" in message
