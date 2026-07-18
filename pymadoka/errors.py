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
        via = ", ".join(str(s) for s in self.tried_sources) or "unknown"
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
