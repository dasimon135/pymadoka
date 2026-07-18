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

class ConnectionException(MadokaError):
    """Generic connection/protocol failure (legacy name, kept for compat)."""
    pass


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
    ):
        self.reconnect = reconnect
        self.adapter = adapter
        self.address = address
        self.name = name or address
        self.hass = hass
        self.candidates_callback = candidates_callback
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
        # Fire-and-forget cleanup tasks: keep a reference so they cannot be
        # garbage-collected mid-flight.
        self._bg_tasks: set = set()

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
        if self.client:
            try:
                await self.client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            await self.client.disconnect()
        self.connection_status = ConnectionStatus.DISCONNECTED

    async def start(self):
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
        auth_failures = 0
        for ble_device in candidates:
            source = None
            if isinstance(getattr(ble_device, "details", None), dict):
                source = ble_device.details.get("source")
            tried_sources.append(source)

            # Only adopt the advertised name when the caller did not provide
            # one (self.name defaults to the address).
            if getattr(ble_device, "name", None) and self.name == self.address:
                self.name = ble_device.name

            client = None
            pair_timed_out = False
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    disconnected_callback=self.on_disconnect,
                    max_attempts=2,
                )
                # Establish the authenticated bond BEFORE any GATT operation
                # (see _connect_via_ha_single). Pair on every path attempt:
                # the bond is stored per BLE adapter/proxy, so a different
                # candidate may still need to authenticate.
                try:
                    await asyncio.wait_for(client.pair(), timeout=8.0)
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
                self.connected_source = source
                self.connection_status = ConnectionStatus.CONNECTED
                self.last_error = None
                self._retry_delay = 5.0
                logger.info(
                    f"Connected to {self.address} ({self.name}) via "
                    f"{source or 'local adapter'}")
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
                # Disconnect INLINE before trying the next candidate: a
                # still-open failed link on this single-central device would
                # make every later path fail too, misclassifying an
                # all-paths-need-pairing situation as mixed/transient.
                if client is not None:
                    await self._disconnect_client(client)
                if pair_timed_out or is_pairing_error(e):
                    auth_failures += 1
                    detail = (
                        pairing_failure_message(self.address, e)
                        if pair_timed_out else e
                    )
                    logger.info(
                        f"{self.address}: path via {source or 'local adapter'} "
                        f"needs pairing, trying next path: {detail}")
                else:
                    logger.warning(
                        f"{self.address}: path via {source or 'local adapter'} "
                        f"failed: {e}")

        if auth_failures == len(candidates):
            self.last_error = PairingRequiredError(self.address, tried_sources=tried_sources)
            self.connection_status = ConnectionStatus.ABORTED
            raise self.last_error
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
                    await asyncio.wait_for(self.client.pair(), timeout=8.0)
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
                await asyncio.sleep(1.5)

            if not self.client.is_connected:
                # The device dropped the link during pair/subscribe/settle;
                # do NOT stamp CONNECTED over the disconnect or the state
                # machine lies forever. The outer loop will retry.
                logger.warning(f"{self.address} dropped the link right after connecting, retrying")
                return

            self.connection_status = ConnectionStatus.CONNECTED
            self._retry_delay = 5.0  # reset backoff on successful connect
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
