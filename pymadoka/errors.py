"""Typed errors so callers can distinguish failure modes.

The HA integration maps these to actionable repair issues:
PairingRequiredError -> "confirm the pairing prompt on the thermostat screen";
DeviceUnreachableError -> "device out of range / no proxy sees it".
"""
from __future__ import annotations

import re
from typing import Literal, Mapping, Optional, Sequence

# Why a PairingRequiredError was raised. The distinction is epistemic, not
# cosmetic:
#   "rejected"       a path EXPLICITLY refused the authenticated bond. Proof.
#                    A human has to re-pair at the thermostat.
#   "timeout_streak" every path merely timed out while pairing, for enough
#                    consecutive rounds to give up. An INFERENCE: on a bonded
#                    path a pairing timeout means congestion (several
#                    thermostats re-encrypting through the same proxies after
#                    a restart), not a missing bond.
#   "unbonded_path"  every path the backend actually chose was outside the
#                    caller's allowed set, for enough consecutive rounds to
#                    give up. Not a pairing FAILURE at all: pairing was never
#                    attempted, precisely so that no prompt would appear. A
#                    fact about ROUTING, not an accusation about a bond — the
#                    fix is to pair with the proxy named in `evidence`, and no
#                    existing bond may be evicted on the strength of it.
# Consumers must not treat the last two as proof of a missing bond: acting on
# them puts a pairing prompt on a screen nobody is watching, and repeated
# prompts jam the BRC1H's SMP stack.
PairingFailureReason = Literal["rejected", "timeout_streak", "unbonded_path"]

# Per-path verdict carried in PairingRequiredError.evidence. "unbonded" means
# the path was skipped BEFORE pair(): it records where the connection landed
# and says nothing whatsoever about that proxy's bond.
PathVerdict = Literal["rejected", "timeout", "transient", "unbonded"]


class MadokaError(Exception):
    """Base class for all pymadoka errors."""


class PairingRequiredError(MadokaError):
    """Pairing could not be completed on any attempted path.

    Attributes:
        address: the device MAC.
        tried_sources: the proxy source MACs that were attempted, in order
            (None entries = local adapter / unknown source).
        reason: "rejected" when at least one path explicitly refused the bond
            (proof), "timeout_streak" when every path merely timed out for
            enough consecutive rounds (an inference — see
            PairingFailureReason). Defaults to "rejected" so pre-0.3.10 call
            sites keep their meaning.
        timeout_rounds: consecutive all-timed-out rounds behind the verdict;
            0 for a rejection.
        evidence: per-source verdict ("rejected" / "timeout" / "transient") for
            the round that produced the error. Lets a consumer attribute a
            refusal to a SPECIFIC proxy even when several paths were tried;
            empty when the caller did not supply one.
    """

    def __init__(
        self,
        address: str,
        tried_sources: Optional[Sequence[Optional[str]]] = None,
        *,
        reason: PairingFailureReason = "rejected",
        timeout_rounds: int = 0,
        evidence: Optional[Mapping[Optional[str], str]] = None,
    ):
        self.address = address
        self.tried_sources = list(tried_sources or [])
        self.reason: PairingFailureReason = reason
        self.timeout_rounds = timeout_rounds
        self.evidence = dict(evidence or {})
        via = ", ".join(str(s) if s is not None else "local adapter" for s in self.tried_sources) or "unknown"
        if reason == "unbonded_path":
            # Names what to DO, because this one is actionable and precise: we
            # know exactly which proxy the connection keeps landing on, and
            # that pairing with it has never been attempted.
            super().__init__(
                f"{address} could only be reached through a path that is not "
                f"allowed to pair (landed on: {via}) for {timeout_rounds} "
                "consecutive rounds — no pairing was attempted, so nothing "
                "was prompted on the thermostat; pair with that proxy "
                "deliberately to make the path usable"
            )
            return
        if reason == "timeout_streak":
            # Deliberately NOT "refused": nothing refused anything. Saying so
            # would be factually false on a bonded path under congestion, and
            # it is the claim that drives consumers to convict the device.
            super().__init__(
                f"{address} did not complete pairing within the budget on any "
                f"attempted path (tried via: {via}) for {timeout_rounds} "
                "consecutive rounds — this is often congestion and clears on "
                "its own; if it persists, re-pair the device and confirm the "
                "prompt on the thermostat screen"
            )
            return
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
    "org.bluez.error.authentication",  # BlueZ AuthenticationRejected/Canceled/Timeout
)

# ATT error 0x05 = insufficient authentication; word boundary so that
# e.g. "error=51" does not match, and a message ending in "error=5" does.
_ATT_ERROR_5_RE = re.compile(r"\berror=5\b")


def is_pairing_error(exc: BaseException) -> bool:
    """True if the exception denotes a missing/refused authenticated bond.

    Marker-only contract: classification is based solely on the error text.
    A bare TimeoutError is NOT classified here: only the pair() call site
    knows a timeout means an unanswered prompt — handle it there.
    """
    text = str(exc).lower()
    if any(marker in text for marker in _PAIRING_ERROR_MARKERS):
        return True
    return _ATT_ERROR_5_RE.search(text) is not None
