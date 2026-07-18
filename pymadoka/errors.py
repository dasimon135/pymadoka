"""Typed errors so callers can distinguish failure modes.

The HA integration maps these to actionable repair issues:
PairingRequiredError -> "confirm the pairing prompt on the thermostat screen";
DeviceUnreachableError -> "device out of range / no proxy sees it".
"""
from __future__ import annotations

from typing import Optional, Sequence


class MadokaError(Exception):
    """Base class for all pymadoka errors."""


class PairingRequiredError(MadokaError):
    """Every attempted path refused the authenticated bond.

    tried_sources lists the proxy source MACs that were attempted
    (None entries = local adapter / unknown source).
    """

    def __init__(self, address: str, tried_sources: Optional[Sequence[Optional[str]]] = None):
        self.address = address
        self.tried_sources = list(tried_sources or [])
        via = ", ".join(s if s is not None else "local adapter" for s in self.tried_sources) or "unknown"
        super().__init__(
            f"{address} refused the authenticated bond on every attempted "
            f"path (tried via: {via}) — confirm the pairing prompt on the "
            "thermostat screen"
        )


class DeviceUnreachableError(MadokaError):
    """No BLE path to the device (out of range / no proxy sees it)."""

    def __init__(self, address: str):
        self.address = address
        super().__init__(f"No BLE path to {address}: device not seen by any adapter/proxy")


# Substrings (lowercased) that identify an authentication/bonding rejection
# in bleak / bleak-esphome error text. String matching is deliberate: the
# GATT status only survives as text through the proxy stack.
_PAIRING_ERROR_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "pairing failed",
    "authentication failed",
    "error=5 ",          # ATT error 0x05 = insufficient authentication
)


def is_pairing_error(exc: BaseException) -> bool:
    """True if the exception denotes a missing/refused authenticated bond.

    A TimeoutError from pair() counts: it almost always means the numeric
    comparison prompt is sitting unanswered on the thermostat screen.
    """
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PAIRING_ERROR_MARKERS)
