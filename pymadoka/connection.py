import asyncio
from asyncio.exceptions import CancelledError
import logging

from enum import Enum

from bleak import BleakClient, BleakScanner
from typing import Dict

from pymadoka.errors import (
    DeviceUnreachableError,
    MadokaError,
    PairingRequiredError,
    is_pairing_error,
)
from pymadoka.transport import Transport, TransportDelegate
from pymadoka.consts import NOTIFY_CHAR_UUID, WRITE_CHAR_UUID, SEND_MAX_TRIES

logger = logging.getLogger(__name__)

# Delay after pairing + notification subscription before declaring the
# connection usable (lets the fresh bond and subscription settle).
SETTLE_DELAY = 1.5

# Consecutive rounds in which EVERY candidate path timed out while pairing
# before we conclude the bond is really missing. A timeout is ambiguous: it
# also happens when an existing bond's SMP encryption is merely slow (several
# thermostats reconnecting through the same proxies after a restart). Only an
# explicit authentication rejection is immediate proof; timeouts must survive
# this many rounds, because each wrong accusation puts a pairing prompt on the
# thermostat screen and repeated prompts jam the BRC1H's SMP stack.
PAIRING_TIMEOUT_ROUNDS = 3

# Budget for a single pair() call. Sized for the common case — re-encrypting an
# existing bond — because an automatic reconnect must not sit on the BRC1H's
# single central slot. A REAL pairing is numeric-comparison: the user compares
# a 6-digit code with the thermostat screen and accepts there, which no 8s
# budget can accommodate. Callers that know a human is standing at the device
# (a manual "pair now" action) raise pair_timeout for the duration.
DEFAULT_PAIR_TIMEOUT = 8.0

# Consecutive rounds in which EVERY path the backend chose was outside the
# caller's allowed set before we say so out loud. A single such round is
# ordinary: habluetooth re-scores every path on every connect (RSSI, failure
# counts, free slots all move), so the winner changes minute to minute and the
# next poll may well land on an allowed proxy. A streak means the scoring
# genuinely favours a path nobody may pair on, which only a human can resolve.
# Skipped rounds are cheap — a connect and a disconnect, no SMP, nothing on the
# thermostat screen — so this can afford to be patient.
UNBONDED_PATH_ROUNDS = 3


class ConnectionException(MadokaError):
    """Generic connection/protocol failure (legacy name, kept for compat)."""
    pass


class _UnbondedPath(Exception):
    """Internal marker: the backend landed on a path we may not pair on.

    Private and never raised past _connect_via_ha. It exists so the skip can
    reuse the candidate loop's existing teardown (disconnect inline, attribute
    the real path, move to the next candidate) instead of duplicating it, while
    staying impossible to confuse with a real BLE failure — notably by
    is_pairing_error(), which must never see it as a refused bond.
    """

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"connection landed on {source}, which may not pair")


class ConnectionStatus(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ABORTED = 3


def pairing_failure_message(address: str, exc: BaseException) -> str:
    """Human-actionable log message for a failed pairing attempt.

    str(TimeoutError()) is empty, and a pairing timeout almost always means
    the confirmation prompt is sitting unanswered on the thermostat screen —
    say so instead of ending the message with a bare colon.
    """
    if isinstance(exc, TimeoutError):
        return (
            f"Pairing with {address} timed out — confirm the pairing prompt "
            "on the thermostat screen (required once per Bluetooth proxy)"
        )
    return f"Pairing with {address} did not complete: {exc}"


def connected_path_source(client) -> str | None:
    """The proxy that ACTUALLY carried this link, or None if unknowable.

    Returning None rather than quietly substituting the candidate is the whole
    point: a caller that cannot tell "the two agree" from "I could not read the
    real one" has a fix that may be doing nothing at all while every log line
    and every test still looks healthy. Callers fall back explicitly, and say
    so at DEBUG when they do.

    Under Home Assistant the BLEDevice handed to establish_connection is
    advisory only. habluetooth's HaBleakClientWrapper keeps just the ADDRESS
    (`self.__address = address_or_ble_device.address`) and throws the device
    away; connect() then calls _async_get_best_available_backend_and_device(),
    which re-sorts every scanner that sees the address by RSSI and
    score_connection_path(). So the candidate we picked says what we INTENDED,
    never what happened — and the two disagree in practice (daikin_madoka #53:
    a thermostat paired through a proxy that had been filtered out of its
    candidate list entirely).

    Recording the intention as fact is what lets a proxy that never carried a
    session be marked as holding a bond, and a refusal be charged to a proxy
    that was not even in the conversation.

    Once the link is up the wrapper publishes the winning scanner, so the truth
    is readable exactly when it matters most: a bond REJECTION is raised by
    client.pair(), i.e. after establish_connection has already succeeded and
    this attribute is set. (A failure to connect at all leaves nothing to read,
    and that case genuinely cannot be attributed to a path — callers must not
    pretend otherwise.)

    Every read is guarded: the attribute is private, and other backends (a
    local adapter, a plain BleakClient in a test) do not have it.
    """
    scanner = getattr(client, "_connected_scanner", None)
    source = getattr(scanner, "source", None)
    if isinstance(source, str) and source:
        return source
    return None


async def discover_devices(timeout=5, adapter="hci0", force_disconnect=True):
    """Trigger a bluetooth devices discovery on the adapter for the timeout interval."""
    scanner = BleakScanner(adapter=adapter)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return scanner.discovered_devices

async def force_device_disconnect(address):
    """Force a device disconnect so it can be listed during the scan."""
    logger.debug("Forcing disconnect...")
    process = await asyncio.create_subprocess_exec(
        "bluetoothctl", "disconnect", address,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.debug(f"Disconnect failed: {stderr.decode().strip()}")


class Connection(TransportDelegate):
    """Bluetooth client.

    Attributes:
        candidates_callback: callback returning an ordered list of BLEDevice
            candidates (preferred path first); when provided, the HA connect
            path tries them in order instead of letting HA pick by RSSI.
        connected_source: source MAC of the scanner/proxy that served the
            current connection (None when unknown).
        last_error: last classified MadokaError, None after a successful
            connect.
    """

    client: BleakClient = None

    def __init__(
        self,
        address: str,
        adapter: str,
        reconnect: bool = True,
        hass=None,
        name: str = None,
        candidates_callback=None,
        pair_timeout: float = DEFAULT_PAIR_TIMEOUT,
        allowed_sources_callback=None,
    ):
        self.reconnect = reconnect
        # Public and mutable: callers widen it around a user-driven pairing.
        self.pair_timeout = pair_timeout
        self.adapter = adapter
        self.address = address
        self.name = name or address
        self.hass = hass
        self.candidates_callback = candidates_callback
        # Returns the source MACs this device may PAIR through, or None/empty
        # for "unrestricted". A callback rather than a list because the answer
        # changes without the Connection being rebuilt: opening a pairing
        # window (a user standing at the thermostat) lifts the restriction for
        # a few minutes, and a successful session adds a proxy to the set.
        self.allowed_sources_callback = allowed_sources_callback
        self.connected_source = None
        self.last_error = None
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.last_info = None
        self.transport = Transport(self)
        self.current_future = None
        self.requests = {}
        self._is_starting = False
        self._closing = False
        self._paired = False
        self._operation_lock = asyncio.Lock()
        self._retry_delay = 5.0
        # Consecutive rounds where every path timed out while pairing; reset
        # by any successful connect. See PAIRING_TIMEOUT_ROUNDS.
        self._pairing_timeout_rounds = 0
        # Consecutive rounds where every path was outside the allowed set;
        # reset by any successful connect. See UNBONDED_PATH_ROUNDS.
        self._unbonded_path_rounds = 0
        # Sources that have EXPLICITLY refused the authenticated bond, kept
        # across rounds. A rejection is durable proof — a bond does not come
        # back without a human — so it must not be thrown away just because a
        # different path failed transiently in the same round: a single
        # flapping proxy could otherwise postpone a legitimate conclusion
        # forever. Cleared per source the moment that source authenticates.
        self._rejected_sources: set = set()
        # Fire-and-forget cleanup tasks: keep a reference so they cannot be
        # garbage-collected mid-flight.
        self._bg_tasks: set = set()

    @property
    def pairing_timeout_rounds(self) -> int:
        """Consecutive rounds where every path timed out while pairing.

        Read-only view of the ambiguity streak (see PAIRING_TIMEOUT_ROUNDS).
        Reset to 0 by any successful connect and by the streak verdict itself,
        so it must NOT be used to tell the two PairingRequiredError kinds
        apart — read `PairingRequiredError.reason` for that.
        """
        return self._pairing_timeout_rounds

    def reset_pairing_timeout_rounds(self) -> None:
        """Forget the ambiguity streak.

        For consumers that know the situation changed outside the library —
        a user re-paired, a proxy was added — and do not want stale rounds
        counting toward the next verdict.
        """
        self._pairing_timeout_rounds = 0

    def resume_pairing_timeout_rounds(self, rounds: int) -> None:
        """Restore a streak carried across Connection rebuilds.

        A consumer that recreates the Connection (Home Assistant rebuilds it
        on every config entry retry) would otherwise restart the count at zero
        every time and never reach PAIRING_TIMEOUT_ROUNDS. Values are clamped
        to >= 0; this is the supported way to seed the counter, so nothing
        needs to touch the private attribute.
        """
        self._pairing_timeout_rounds = max(0, int(rounds))

    @property
    def rejected_sources(self) -> frozenset:
        """Sources that explicitly refused the bond and have not since worked."""
        return frozenset(self._rejected_sources)

    def _path_may_pair(self, source) -> bool:
        """May we call pair() on the path we actually landed on?

        FAILS OPEN at every step, deliberately. This guard exists to stop an
        unwanted pairing prompt, which is an annoyance; refusing to connect is
        an outage. Whenever the answer is not a confident "no", it is "yes":

        * no callback, or one that returns nothing — the caller is not using
          the feature, or has no bond on record yet (a fresh install has to be
          able to pair with SOMETHING);
        * a callback that raises — a broken policy must not strand the device,
          so it is logged and ignored;
        * an unreadable path — a local adapter or a plain BleakClient never
          names a scanner, and there is no proxy to restrict in that case.

        Only a path that is positively known AND positively absent from a
        non-empty allowed set is refused.
        """
        if self.allowed_sources_callback is None:
            return True
        try:
            allowed = self.allowed_sources_callback()
        except Exception:  # noqa: BLE001
            logger.exception(
                f"allowed_sources_callback failed for {self.address}; "
                "letting this path pair rather than stranding the device")
            return True
        if not allowed:
            return True
        if source is None:
            logger.debug(
                f"{self.address}: the backend did not name the path it used, "
                "so the allowed-source restriction cannot be applied to it")
            return True
        return source in set(allowed)

    def discard_request(self, cmd_id: int, cmd_response) -> None:
        """Remove a pending response future from the request queue.

        Called on timeout/cancellation so a late response cannot resolve an
        abandoned future and desync the FIFO for this cmd_id.
        """
        queue = self.requests.get(cmd_id)
        if not queue:
            return
        try:
            queue.remove(cmd_response)
        except ValueError:
            pass

    def on_disconnect(self, client: BleakClient):
        # A failed candidate's client is never assigned to self.client; its
        # late disconnect callback must not clobber the live connection state.
        if client is not self.client:
            return
        self.connection_status = ConnectionStatus.DISCONNECTED
        # The path that served us is gone; never let callers read a stale one.
        self.connected_source = None
        # Re-pair on the next connect: the bond is stored per BLE adapter/proxy,
        # so a reconnect may land on a peer that still needs to authenticate.
        # Skipping pair() there fails every GATT op with "Insufficient
        # authentication".
        self._paired = False
        logger.info(f"Disconnected {self.address}")
        if self.reconnect and not self._is_starting and not self._closing:
            # Fire-and-forget reconnect goes through _background_start so a
            # typed error can never become an unhandled task exception; keep
            # a reference so the task cannot be GC'd mid-flight.
            t = asyncio.create_task(self._background_start())
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    async def cleanup(self):
        self._closing = True
        self.reconnect = False
        self._paired = False
        # Quiesce background tasks BEFORE disconnecting: an in-flight
        # _background_start that already passed the _closing check could
        # otherwise complete a connect AFTER the disconnect below, leaving
        # the BRC1H's single central slot occupied after an entry unload.
        tasks = [t for t in self._bg_tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            try:
                await self.client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            await self.client.disconnect()
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.connected_source = None

    async def start(self):
        """Run the connection loop until connected or aborted.

        Typed MadokaError subclasses (PairingRequiredError,
        DeviceUnreachableError) propagate to the caller. If a (background)
        start is already running, this call returns immediately without
        raising; callers relying on typed-error signaling should check
        connection_status / last_error afterwards.
        """
        if self._is_starting:
            logger.debug(f"start() already running for {self.address}, skipping")
            return
        self._is_starting = True
        logger.debug(f"Starting connection manager on {self.address}")
        self.connection_status = ConnectionStatus.CONNECTING
        try:
            while self.connection_status not in (ConnectionStatus.CONNECTED, ConnectionStatus.ABORTED):
                if self._closing:
                    # cleanup() was called: stop any in-flight (re)connect loop.
                    break
                try:
                    if self.hass is not None:
                        await self._connect_via_ha()
                    else:
                        if self.client is None:
                            await self._select_device()
                        await self._connect()
                    if self.connection_status != ConnectionStatus.CONNECTED:
                        await asyncio.sleep(2.0)
                except ConnectionAbortedError:
                    self.connection_status = ConnectionStatus.ABORTED
                except CancelledError:
                    # Propagate cancellation (e.g. asyncio.wait_for timeout in the
                    # caller) instead of looping forever.
                    logger.debug(f"Connection task cancelled for {self.address}")
                    raise
                except MadokaError:
                    # Classified failure: status/last_error were already stamped
                    # where it was raised. Typed errors are the caller's signal —
                    # never swallow.
                    raise
                except Exception as e:
                    logger.error(f"Unexpected error in connection loop for {self.address}: {e}")
                    self.connection_status = ConnectionStatus.ABORTED
        finally:
            self._is_starting = False

    async def _background_start(self):
        """start() wrapper for fire-and-forget reconnects: never raises.

        A typed error during an automatic reconnect has no caller to signal;
        it is recorded in last_error (done at the raise site) and logged, and
        the next explicit start() from the integration will retry/report.
        """
        try:
            await self.start()
        except MadokaError as e:
            logger.warning(f"Background reconnect for {self.address} gave up: {e}")

    async def _connect_via_ha(self):
        """Connect via HA, trying candidate paths in order when available.

        Without a candidates_callback (or when it fails) this degrades to the
        legacy single-device path where HA picks one BLEDevice by RSSI.

        With candidates: try each BLEDevice in order (preferred path first).
        A pairing/bond rejection on one path falls through to the next; if
        EVERY path rejects the bond, raise PairingRequiredError. An empty
        candidate list raises DeviceUnreachableError. Mixed/transient
        failures return after a backoff so the outer start() loop retries.
        """
        if self.candidates_callback is None:
            return await self._connect_via_ha_single()
        try:
            candidates = list(self.candidates_callback())
        except Exception:  # noqa: BLE001
            logger.exception(
                f"candidates_callback failed for {self.address}; "
                "falling back to single-device path")
            return await self._connect_via_ha_single()
        from bleak_retry_connector import establish_connection

        if not candidates:
            self.last_error = DeviceUnreachableError(self.address)
            self.connection_status = ConnectionStatus.ABORTED
            raise self.last_error

        tried_sources = []
        # Per-path verdict for THIS round, aligned with tried_sources:
        # "rejected" | "timeout" | "transient". Split by evidence strength: a
        # rejection proves the bond is gone, a timeout only suggests it (see
        # PAIRING_TIMEOUT_ROUNDS).
        verdicts: list = []
        # Same order as verdicts, but only holds a source when the path is
        # PROVEN — i.e. a link was established and the wrapper named the
        # scanner that carried it. tried_sources is the human-facing list and
        # falls back to the candidate we offered, which is fine for a message
        # and unusable as evidence: charging a refusal to a proxy we merely
        # aimed at is the bug this split exists to prevent (#53). None here
        # means "this round cannot say which path failed", and consumers
        # already skip falsy sources.
        evidence_sources: list = []
        for ble_device in candidates:
            source = None
            if isinstance(getattr(ble_device, "details", None), dict):
                source = ble_device.details.get("source")
            tried_sources.append(source)
            evidence_sources.append(None)

            # Only adopt the advertised name when the caller did not provide
            # one (self.name defaults to the address).
            if getattr(ble_device, "name", None) and self.name == self.address:
                self.name = ble_device.name

            client = None
            pair_timed_out = False
            try:
                # max_attempts=1: exactly ONE path decision per call. Field
                # incident 2026-07-18: with max_attempts=2, a transient failure
                # of attempt 1 let the retry silently fail over to the
                # strongest-RSSI — unbonded — proxy, which then held the
                # BRC1H's single central slot through 30s SMP auth timeouts
                # (device unreachable >20 min). Under HA, habluetooth's
                # HaBleakClientWrapper keeps only the ADDRESS of the
                # BLEDevice and rescores every path (RSSI + failure counts)
                # on each connect attempt, so any retry inside
                # establish_connection can hop paths. (bleak-retry-connector's
                # ble_device_callback parameter is vestigial in 4.6.0 —
                # declared but never read — so it cannot pin the path.)
                # Retries and failover belong to THIS loop.
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    disconnected_callback=self.on_disconnect,
                    max_attempts=1,
                )
                # BEFORE pair(), and this order is the entire fix. Filtering
                # the candidate list cannot control where the connection
                # lands: habluetooth keeps only the address and re-scores
                # every path itself (see connected_path_source), so the link
                # we now hold may well run through a proxy the caller
                # explicitly excluded. Pairing there starts a real
                # numeric-comparison exchange, which puts a 6-digit prompt on
                # the thermostat screen that no unattended retry can answer —
                # the harassment this guard exists to end. The link is already
                # up, so the real path is readable exactly when it is still
                # cheap to walk away from it.
                landed_on = connected_path_source(client)
                if not self._path_may_pair(landed_on):
                    raise _UnbondedPath(landed_on)
                # Establish the authenticated bond BEFORE any GATT operation
                # (see _connect_via_ha_single). Pair on every path attempt:
                # the bond is stored per BLE adapter/proxy, so a different
                # candidate may still need to authenticate.
                try:
                    await asyncio.wait_for(
                        client.pair(), timeout=self.pair_timeout)
                except TimeoutError:
                    # Marker-only classifier contract (is_pairing_error):
                    # only this call site knows a timeout means the
                    # confirmation prompt sat unanswered on the thermostat
                    # screen — flag it as an auth failure instead of
                    # round-tripping through error text.
                    pair_timed_out = True
                    raise
                await client.start_notify(NOTIFY_CHAR_UUID, self.notification_handler)
                # Let the fresh bond and notification subscription settle
                # before the first command; proxied notifications can
                # otherwise be dropped and the chunked response fails to
                # reassemble.
                await asyncio.sleep(SETTLE_DELAY)
                if not client.is_connected:
                    # Deliberate divergence from the single path (which
                    # returns-with-warning): raising moves on to the NEXT
                    # candidate instead of retrying the same pick.
                    raise ConnectionException(
                        f"{self.address} dropped the link right after connecting")
                self.client = client
                self._paired = True
                # The path HA actually used, which is not necessarily the
                # candidate we offered (see connected_path_source). Everything
                # downstream — the caller's bonded-proxy bookkeeping, the
                # retained-refusal set, this round's evidence — has to key off
                # the real one or it describes a connection that never existed.
                real = connected_path_source(client)
                if real is None:
                    # Not a failure to connect — a failure to KNOW. Say so, or
                    # a backend that never names its scanner degrades to the
                    # old guess-as-fact behaviour without a trace anywhere.
                    logger.debug(
                        f"{self.address}: the backend did not name the path it "
                        f"used; falling back to the offered "
                        f"{source or 'local adapter'}")
                elif real != source:
                    logger.debug(
                        f"{self.address}: offered {source or 'local adapter'} "
                        f"but HA connected via {real}")
                actual = source if real is None else real
                self.connected_source = actual
                tried_sources[-1] = actual
                evidence_sources[-1] = actual
                self.connection_status = ConnectionStatus.CONNECTED
                self.last_error = None
                self._retry_delay = 5.0
                # Earlier timeouts were transient after all — this very path
                # just authenticated. Forget them, or an unlucky streak
                # spread over hours would eventually accuse a healthy bond.
                self._pairing_timeout_rounds = 0
                # Likewise: the routing just produced a usable path, so any
                # streak of rounds where it did not is history.
                self._unbonded_path_rounds = 0
                # Same for a past refusal ON THIS PATH: it just proved it
                # holds a bond, so the retained proof is stale and must go.
                self._rejected_sources.discard(actual)
                logger.info(
                    f"Connected to {self.address} ({self.name}) via "
                    f"{actual or 'local adapter'}")
                return
            except CancelledError:
                # Caller timeout cancelled us mid-connect: don't leak a live
                # link (the BRC1H accepts a single central) — disconnect it.
                # Fire-and-forget (we must re-raise promptly), but keep a
                # reference so the task cannot be GC'd mid-flight.
                if client is not None:
                    t = asyncio.create_task(self._disconnect_client(client))
                    self._bg_tasks.add(t)
                    t.add_done_callback(self._bg_tasks.discard)
                raise
            except Exception as e:  # noqa: BLE001
                # Read the real path BEFORE tearing the link down: a bond
                # REJECTION is raised by client.pair(), which only runs once
                # establish_connection has succeeded, so this is exactly the
                # failure we CAN attribute. A failure to connect at all leaves
                # client None and the path genuinely unknown — proven stays
                # False and nothing is charged to anyone.
                real = connected_path_source(client)
                proven = real is not None
                if client is not None and not proven:
                    logger.debug(
                        f"{self.address}: a link existed but the backend did "
                        f"not name it; this failure is charged to nobody")
                # For the log line and the human-facing tried_sources only.
                actual = real if proven else source
                if proven:
                    tried_sources[-1] = actual
                    evidence_sources[-1] = actual
                # Disconnect INLINE before trying the next candidate: a
                # still-open failed link on this single-central device would
                # make every later path fail too, misclassifying an
                # all-paths-need-pairing situation as mixed/transient.
                if client is not None:
                    await self._disconnect_client(client)
                if isinstance(e, _UnbondedPath):
                    # NOT a pairing verdict: pair() was never called, so this
                    # says nothing about any bond and must never be counted
                    # toward "every path failed auth". It records only where
                    # the connection landed.
                    verdicts.append("unbonded")
                    logger.info(
                        f"{self.address}: HA routed the connection through "
                        f"{actual or 'local adapter'}, which is not allowed to "
                        "pair; dropped it without pairing (no prompt on the "
                        "thermostat) and trying the next path")
                elif proven and actual in self._rejected_sources:
                    # This path refused the bond in an earlier round and has
                    # not authenticated since, so it is still known to hold
                    # none — whatever it failed with this time. Retaining the
                    # proof is what stops one flapping proxy from postponing a
                    # legitimate conclusion forever. Conservative by
                    # construction: only a PROVEN refusal is ever retained,
                    # never a timeout and never a plain transient failure.
                    #
                    # Ordered ABOVE pair_timed_out on purpose, and the order is
                    # the whole point: a keyless proxy's normal failure IS a
                    # timeout, because the prompt goes up on the thermostat and
                    # nobody answers it. Tested after it, this branch could
                    # only ever see the rarer failures, so the proof was
                    # discarded on exactly the rounds it was written for and
                    # the verdict decayed to "timeout" forever (Salon,
                    # 2026-08-28: one refusal, then timeouts for hours, and a
                    # consumer that acts only on proven refusals charged
                    # nobody). Still below _UnbondedPath: a round that never
                    # called pair() says nothing about any bond, retained proof
                    # or not.
                    verdicts.append("rejected")
                    logger.info(
                        f"{self.address}: path via {actual or 'local adapter'} "
                        f"failed ({e}) and already refused the bond earlier; "
                        "keeping that verdict")
                elif pair_timed_out:
                    verdicts.append("timeout")
                    logger.info(
                        f"{self.address}: pairing via "
                        f"{actual or 'local adapter'} timed out, trying next "
                        f"path: {pairing_failure_message(self.address, e)}")
                elif is_pairing_error(e):
                    verdicts.append("rejected")
                    # Retained proof is per PATH, so it may only be recorded
                    # against a path we actually reached.
                    if proven:
                        self._rejected_sources.add(actual)
                    logger.info(
                        f"{self.address}: path via {actual or 'local adapter'} "
                        f"refused the bond, trying next path: {e}")
                else:
                    verdicts.append("transient")
                    logger.warning(
                        f"{self.address}: path via {actual or 'local adapter'} "
                        f"failed: {e}")

        # Keyed on the PROVEN path, not the one we aimed at. Unattributable
        # attempts collapse under the single None key: consumers skip falsy
        # sources, so an unknown path cannot cost any proxy its bond. Later
        # duplicates win; proven sources are normally unique per round.
        evidence = dict(zip(evidence_sources, verdicts))
        auth_rejections = verdicts.count("rejected")
        pair_timeouts = verdicts.count("timeout")
        every_path_failed_auth = (
            auth_rejections + pair_timeouts == len(candidates)
        )
        # A round in which the backend never once routed us somewhere we are
        # allowed to pair. Tracked as a strict CONSECUTIVE streak — any round
        # that managed anything else clears it — because the scoring moves
        # constantly and an unlucky handful of rounds spread over hours must
        # not add up to an accusation.
        if verdicts and all(verdict == "unbonded" for verdict in verdicts):
            self._unbonded_path_rounds += 1
            if self._unbonded_path_rounds >= UNBONDED_PATH_ROUNDS:
                rounds = self._unbonded_path_rounds
                # Reset for the same reason the timeout streak does: leaving
                # the counter past the threshold would re-raise on every
                # subsequent round, including the user's own retry.
                self._unbonded_path_rounds = 0
                self.last_error = PairingRequiredError(
                    self.address, tried_sources=tried_sources,
                    reason="unbonded_path", timeout_rounds=rounds,
                    evidence=evidence)
                self.connection_status = ConnectionStatus.ABORTED
                raise self.last_error
            logger.info(
                f"{self.address}: every path HA chose this round was not "
                f"allowed to pair (round {self._unbonded_path_rounds}/"
                f"{UNBONDED_PATH_ROUNDS}); retrying without pairing")
        else:
            self._unbonded_path_rounds = 0
        if every_path_failed_auth and auth_rejections:
            # At least one path actively refused: unambiguous, report now.
            self._pairing_timeout_rounds = 0
            self.last_error = PairingRequiredError(
                self.address, tried_sources=tried_sources,
                reason="rejected", timeout_rounds=0, evidence=evidence)
            self.connection_status = ConnectionStatus.ABORTED
            raise self.last_error

        if every_path_failed_auth:
            # Timeouts only. Ambiguous between "prompt sitting unanswered" and
            # "existing bond encrypting slowly under load", so require a streak
            # before accusing — a wrong accusation costs a spurious pairing
            # prompt on the thermostat and, repeated, jams its SMP stack.
            self._pairing_timeout_rounds += 1
            if self._pairing_timeout_rounds >= PAIRING_TIMEOUT_ROUNDS:
                rounds = self._pairing_timeout_rounds
                # Reset like the rejection branch does. Without it the counter
                # stays past the threshold, so the very next classified-timeout
                # round re-raises immediately (4 >= 3) — including the
                # user-initiated retry that follows the accusation, which
                # deserves a fresh budget of PAIRING_TIMEOUT_ROUNDS.
                self._pairing_timeout_rounds = 0
                self.last_error = PairingRequiredError(
                    self.address, tried_sources=tried_sources,
                    reason="timeout_streak", timeout_rounds=rounds,
                    evidence=evidence)
                self.connection_status = ConnectionStatus.ABORTED
                raise self.last_error
            logger.info(
                f"{self.address}: every path timed out while pairing "
                f"(round {self._pairing_timeout_rounds}/{PAIRING_TIMEOUT_ROUNDS}); "
                "treating as transient and retrying")

        # Mixed/transient failures: keep the outer start() loop retrying as
        # today, with the same backoff as the single-device path.
        logger.info(f"Retrying {self.address} in {self._retry_delay:.0f}s")
        await asyncio.sleep(self._retry_delay)
        self._retry_delay = min(self._retry_delay * 2, 60.0)

    async def _connect_via_ha_single(self):
        """Connect using HA's BLE device registry and bleak_retry_connector."""
        from homeassistant.components.bluetooth import async_ble_device_from_address
        from bleak_retry_connector import establish_connection

        ble_device = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            logger.warning(f"Device {self.address} not found in HA BLE tracker, will retry...")
            await asyncio.sleep(5.0)
            return

        # Only adopt the advertised name when the caller did not provide one
        # (self.name defaults to the address), so a user-chosen name survives.
        if ble_device.name and self.name == self.address:
            self.name = ble_device.name

        try:
            self.client = await establish_connection(
                BleakClient,
                ble_device,
                self.address,
                disconnected_callback=self.on_disconnect,
                max_attempts=3,
            )

            # Establish the authenticated bond BEFORE any GATT operation.
            # Otherwise bleak connects unencrypted and only pairs reactively
            # when a read hits "Insufficient authentication" — which the BRC1H
            # handles poorly, dropping the link mid-exchange. Only needed once
            # per Connection lifetime: the bond persists across reconnects.
            just_paired = False
            if not self._paired:
                try:
                    await asyncio.wait_for(
                        self.client.pair(), timeout=self.pair_timeout)
                    self._paired = True
                    just_paired = True
                except Exception as pair_err:  # noqa: BLE001
                    # Surface loudly: an actually-refused bond means every
                    # later GATT exchange will be silently ignored.
                    logger.warning(pairing_failure_message(self.address, pair_err))

            await self.client.start_notify(NOTIFY_CHAR_UUID, self.notification_handler)

            if just_paired:
                # Let the fresh bond and notification subscription settle
                # before the first command; proxied notifications can
                # otherwise be dropped and the chunked response fails to
                # reassemble.
                await asyncio.sleep(SETTLE_DELAY)

            if not self.client.is_connected:
                # The device dropped the link during pair/subscribe/settle;
                # do NOT stamp CONNECTED over the disconnect or the state
                # machine lies forever. The outer loop will retry.
                logger.warning(f"{self.address} dropped the link right after connecting, retrying")
                return

            self.connection_status = ConnectionStatus.CONNECTED
            self.last_error = None  # invariant: None after a successful connect
            self._retry_delay = 5.0  # reset backoff on successful connect
            # A success forgives the ambiguity streak here exactly as it does
            # in the candidate loop: a device that recovers through the
            # fallback path would otherwise keep a stale streak armed and be
            # convicted by the next single timed-out round.
            self._pairing_timeout_rounds = 0
            logger.info(f"Connected to {self.address} ({self.name}) via bleak_retry_connector")
        except CancelledError:
            # Caller timeout cancelled us mid-connect: don't leak a live link
            # (the BRC1H accepts a single central) — detach and disconnect it.
            client, self.client = self.client, None
            if client is not None:
                # Same _bg_tasks anchoring as the candidate loop: keep a
                # reference so the disconnect task cannot be GC'd mid-flight.
                t = asyncio.create_task(self._disconnect_client(client))
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)
            raise
        except Exception as e:
            logger.error(f"Failed to connect to {self.address}: {e}")
            logger.info(f"Retrying {self.address} in {self._retry_delay:.0f}s")
            await asyncio.sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, 60.0)

    @staticmethod
    async def _disconnect_client(client: BleakClient) -> None:
        """Best-effort disconnect of an orphaned client."""
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("Orphaned client disconnect failed", exc_info=True)

    async def _connect(self):
        try:
            connected = self.client.is_connected
            if not connected:
                await self.client.connect()
                connected = self.client.is_connected

            if connected:
                logger.info(f"Connected to {self.address}")
                self.connection_status = ConnectionStatus.CONNECTED
                await self.client.start_notify(
                    NOTIFY_CHAR_UUID, self.notification_handler,
                )
            else:
                logger.warning(f"Failed to connect to {self.address}")

        except Exception as e:
            logger.error(f"Connection error for {self.address}: {e}")
            if not self.reconnect:
                raise e

    async def _select_device(self):
        """Create a BleakClient from the address string (standalone / non-HA path)."""
        logger.debug(f"Creating BleakClient for {self.address}")
        self.client = BleakClient(
            self.address,
            adapter=self.adapter,
            disconnected_callback=self.on_disconnect,
        )

    def notification_handler(self, sender: str, data: bytearray):
        self.transport.rebuild_chunk(data)

    def cmd_id_to_bytes(self, cmd_id: int):
        return bytearray([0x00]) + cmd_id.to_bytes(2, "big")

    def bytes_to_cmd_id(self, data: bytes):
        return int.from_bytes(data[2:4], "big")

    async def send(self, cmd_id: int, data: bytearray):
        cmd_response = asyncio.get_event_loop().create_future()
        if cmd_id not in self.requests:
            self.requests[cmd_id] = []

        self.requests[cmd_id].append(cmd_response)

        if self.connection_status is not ConnectionStatus.CONNECTED:
            cmd_response.cancel()
            return cmd_response

        payload = bytearray([0x00]) + self.cmd_id_to_bytes(cmd_id) + data
        payload[0] = len(payload)

        logger.debug(f"Sending cmd payload: {bytes(payload).hex()}")

        chunks = self.transport.split_in_chunks(payload)
        sent = 0

        self.current_cmd_id = cmd_id
        for chunknum, chunk in enumerate(chunks):
            for i in range(0, SEND_MAX_TRIES):
                try:
                    if self.connection_status is not ConnectionStatus.CONNECTED:
                        cmd_response.cancel()
                        return cmd_response

                    await self.client.write_gatt_char(WRITE_CHAR_UUID, chunk)
                    logger.debug(f"CMD {cmd_id}. Chunk #{chunknum+1}/{len(chunks)} sent with size {len(chunk)} bytes")
                    sent += 1
                    break
                except CancelledError:
                    # Propagate task cancellation instead of retrying: retrying
                    # here would un-cancel the caller (e.g. entry unload).
                    cmd_response.cancel()
                    raise
                except Exception as e:
                    logger.debug(f"Send command failed. Retrying ({i}/{SEND_MAX_TRIES}) for chunk #{chunknum} : {str(e)}")
                    await asyncio.sleep(1)

        if sent != len(chunks) and self.connection_status == ConnectionStatus.CONNECTED:
            raise ConnectionException("Command chunks could not be sent")

        return cmd_response

    def response_rebuilt(self, data: bytearray):
        if len(data) <= 4:
            return

        cmd_id = self.bytes_to_cmd_id(data)

        if cmd_id not in self.requests:
            return
        if len(self.requests[cmd_id]) > 0:
            req = self.requests[cmd_id].pop(0)
            if req.done():
                return
            req.set_result(data)

    def response_failed(self, data: bytearray):
        if len(data) <= 4:
            return

        cmd_id = self.bytes_to_cmd_id(data)

        if cmd_id not in self.requests:
            return

        if len(self.requests[cmd_id]) > 0:
            req = self.requests[cmd_id].pop(0)
            if req.done():
                return
            req.cancel()

    async def read_info(self) -> Dict[str, str]:
        try:
            if self.last_info:
                 return self.last_info

            if self.connection_status is not ConnectionStatus.CONNECTED:
                return {}

            values = {}

            for service in self.client.services:
                logger.debug("[Service] {0}: {1}".format(service.uuid, service.description))
                for char in service.characteristics:
                    if "read" in char.properties:
                        try:
                            raw = await self.client.read_gatt_char(char.uuid)
                            value = None

                            try:
                                if char.description.endswith(" ID"):
                                    value = raw.hex().replace("fe", "-").replace("ff", "")
                                else:
                                    value = raw.decode()
                            except Exception:
                                value = str(raw)
                            values[char.description] = value
                        except Exception as e:
                            logger.error(e)

            self.last_info = values
            return self.last_info
        except Exception as e:
            logger.error(e)
            raise e
