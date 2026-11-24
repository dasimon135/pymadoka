"""This module reads the thermistor temperatures the Madoka Assistant app
shows on its "Unit status" screen.

Sequence found by the reporter of daikin_madoka#162 from an HCI capture of
the app, and replayed from an ESP32:

1. Function 0x4111, argument 0xFE = 1: start thermistor monitoring.
2. Function 0x0111, page 0 (indoor): arguments 0x01 = page, 0x02 = unit 0,
   then 0x40 to 0x4F with length 0.
3. Function 0x0111, page 1 (outdoor): same, but only the arguments that
   page has (OUTDOOR_IDS); asking for others empties the answer.
4. Function 0x4111, argument 0xFE = 0: stop monitoring.

The first answers to a page carry length 0xFF for every argument while the
unit acquires the readings; the app asks again every 0.5 to 0.7 s and the
values arrive on the third or fourth answer. The outdoor page answers empty
while the indoor one is still pending, so it is only asked afterwards.

Arguments 0x4A, 0x4B, 0x4C are 2-byte big-endian whole degrees Celsius,
FF FF when the sensor is not fitted. Bit 14 is a flag of unknown meaning,
not part of the reading: the #162 reporter saw 0x4006, 0x4002, 0x4000 and
0x400C on liquid pipe and outdoor heat exchanger, outside any defrost, and
each matched the reads around it once the bit was cleared. It is only
cleared when bit 15 is 0: below zero the encoding is not confirmed yet, and
a two's complement value such as FF FB (-5) has bit 14 set as well.

    0x4A  indoor Th1 suction air       outdoor Th1 outdoor air
    0x4B  indoor Th2 liquid pipe       outdoor Th2 heat exchanger
    0x4C  indoor Th3 gas pipe          outdoor Th3 compressor discharge

The other arguments (indoor 0x40 = ASCII "OFF", 0x49, outdoor 0x45/0x46)
are not understood and not exposed.
"""

import asyncio
import logging
from typing import Dict, Optional, Sequence

from pymadoka.connection import Connection
from pymadoka.feature import Feature, FeatureStatus, NotImplementedException, ParseException

logger = logging.getLogger(__name__)

MONITOR_CMD_ID = 0x4111
PAGE_CMD_ID = 0x0111
INDOOR_PAGE = 0x00
OUTDOOR_PAGE = 0x01
INDOOR_IDS: Sequence[int] = tuple(range(0x40, 0x50))
OUTDOOR_IDS: Sequence[int] = (0x40, 0x41, 0x43, 0x44, 0x45, 0x46, 0x4A, 0x4B, 0x4C)
TH1, TH2, TH3 = 0x4A, 0x4B, 0x4C
PENDING_SIZE = 0xFF
FLAG_BIT = 0x4000
MAX_PAGE_ANSWERS = 6
PAGE_RETRY_DELAY = 0.6


class _PageStatus(FeatureStatus):
    """One answer of function 0x0111 for one page.

    The base parser turns a 0xFF length into an empty value, which would make
    "still acquiring" look like "page has no such sensor"; this one keeps the
    two apart.
    """

    def __init__(self, page: int, ids: Sequence[int]):
        self.page = page
        self.ids = ids
        self.values: Dict[int, bytes] = {}
        self.pending = False

    def get_values(self) -> Dict[int, bytearray]:
        out: Dict[int, bytearray] = {0x01: bytes([self.page]), 0x02: bytes([0x00])}
        for arg_id in self.ids:
            out[arg_id] = b""
        return out

    def set_values(self, values: Dict[int, bytearray]):
        self.values = {k: bytes(v) for k, v in values.items()}

    def parse(self, data: bytearray):
        # size(1), cmd id(3), then id(1) len(1) value(len) per argument
        if len(data) < 4 or data[0] != len(data):
            raise ParseException("Message size and data size mismatch")
        values: Dict[int, bytes] = {}
        pending = False
        i = 4
        while i < len(data):
            if i + 1 >= len(data):
                raise ParseException("Truncated argument header")
            arg_id, size = data[i], data[i + 1]
            if size == PENDING_SIZE:
                pending |= arg_id in (TH1, TH2, TH3)
                size = 0
            if i + 2 + size > len(data):
                raise ParseException("Truncated argument value")
            values[arg_id] = bytes(data[i + 2 : i + 2 + size])
            i += 2 + size
        self.pending = pending
        self.set_values(values)

    def temperature(self, arg_id: int) -> Optional[int]:
        value = self.values.get(arg_id)
        if value is None or len(value) != 2 or value == b"\xff\xff":
            return None
        raw = int.from_bytes(value, "big")
        if raw & 0x8000:
            # Assumed two's complement; not yet seen on a real unit.
            return raw - 0x10000
        return raw & ~FLAG_BIT


class _MonitorStatus(FeatureStatus):
    """Function 0x4111: argument 0xFE starts (1) or stops (0) monitoring."""

    def __init__(self, on: bool):
        self.on = on

    def get_values(self) -> Dict[int, bytearray]:
        return {0xFE: bytes([0x01 if self.on else 0x00])}

    def set_values(self, values: Dict[int, bytearray]):
        pass


class UnitThermistorsStatus(FeatureStatus):
    """Thermistor temperatures in whole degrees Celsius; None if not fitted
    or not reported.
    """

    def __init__(self):
        self.indoor_suction: Optional[int] = None
        self.indoor_liquid_pipe: Optional[int] = None
        self.indoor_gas_pipe: Optional[int] = None
        self.outdoor_air: Optional[int] = None
        self.outdoor_heat_exchanger: Optional[int] = None
        self.outdoor_discharge: Optional[int] = None

    def set_pages(self, indoor: _PageStatus, outdoor: _PageStatus):
        self.indoor_suction = indoor.temperature(TH1)
        self.indoor_liquid_pipe = indoor.temperature(TH2)
        self.indoor_gas_pipe = indoor.temperature(TH3)
        self.outdoor_air = outdoor.temperature(TH1)
        self.outdoor_heat_exchanger = outdoor.temperature(TH2)
        self.outdoor_discharge = outdoor.temperature(TH3)

    def get_values(self) -> Dict[int, bytearray]:
        return {}

    def set_values(self, values: Dict[int, bytearray]):
        raise NotImplementedException("Filled by UnitThermistors.query from two pages")


class UnitThermistors(Feature):
    """Reads the unit status thermistors. Not held by Controller: one read is
    about ten round trips, so the caller decides how often to run it.
    """

    def __init__(self, connection: Connection):
        self.status = None
        super().__init__(connection)

    def query_cmd_id(self) -> int:
        return PAGE_CMD_ID

    def update_cmd_id(self) -> int:
        raise NotImplementedException("This feature cannot be updated")

    def new_status(self) -> FeatureStatus:
        return UnitThermistorsStatus()

    async def _read_page(self, page: int, ids: Sequence[int]) -> _PageStatus:
        for attempt in range(MAX_PAGE_ANSWERS):
            if attempt:
                await asyncio.sleep(PAGE_RETRY_DELAY)
            answer = _PageStatus(page, ids)
            answer.parse(bytearray(await self._send_command(PAGE_CMD_ID, answer.serialize())))
            logger.debug(f"Thermistor page {page} answer {attempt + 1}: {answer.values}")
            if not answer.pending:
                return answer
        raise asyncio.TimeoutError(
            f"Thermistor page {page} still pending after {MAX_PAGE_ANSWERS} answers"
        )

    async def query(self) -> FeatureStatus:
        """Start monitoring, read the indoor then the outdoor page, stop."""
        await self._send_command(MONITOR_CMD_ID, _MonitorStatus(True).serialize())
        try:
            indoor = await self._read_page(INDOOR_PAGE, INDOOR_IDS)
            outdoor = await self._read_page(OUTDOOR_PAGE, OUTDOOR_IDS)
        finally:
            try:
                await self._send_command(MONITOR_CMD_ID, _MonitorStatus(False).serialize())
            except Exception as err:  # the app's monitoring ends with the screen; best effort
                logger.debug(f"Could not stop thermistor monitoring: {err}")
        status = UnitThermistorsStatus()
        status.set_pages(indoor, outdoor)
        self.status = status
        return status
