"""This module reads the indoor unit status the thermostat reports: defrost /
hot start, and the error, warning and alarm conditions with the current error
code.

Function 0x0100 is the one CleanFilterIndicator already queries for argument
0x62. This feature asks the same function for three other arguments, in its
own query so the filter sign keeps the exact frame it has always used:

- 0x41, one byte of bit flags: bit 2 is set while the indoor unit defrosts or
  hot-starts (the fan holds back cold air). Bit 0 quick start, bit 1 test
  operation and bit 3 duty rotation are not exposed.
- 0x50, one byte of bit flags: bit 0 error, bit 1 warning, bit 2 alarm.
- 0x90, four bytes: unit number, two ASCII characters and a number, shown on
  the thermostat as "U4-01". Two zero characters, or no argument, mean no
  error.

Layout from the MIT-licensed isakLevinson/madoka integration (decoder.py,
NOTIFICATION_BITS); its defrost flag was confirmed on a BRC1H with a ducted
FBA100A by the reporter of daikin_madoka#159.

A thermostat that leaves an argument out of its answer gets None for the
values it carries, not False: "not reported" must not read as "all clear".
"""

from typing import Dict, Optional

from pymadoka.connection import Connection
from pymadoka.feature import Feature, FeatureStatus, NotImplementedException


class UnitStatusStatus(FeatureStatus):
    """Indoor unit status.

    Attributes:
        defrost (Optional[bool]): True while the unit defrosts or hot-starts
        error (Optional[bool]): True while the unit reports an error
        warning (Optional[bool]): True while the unit reports a warning
        alarm (Optional[bool]): True while the unit reports an alarm
        error_code (Optional[str]): Current error code, e.g. "U4-01", or None
    """

    SPECIAL_IDX = 0x41
    ERRORS_IDX = 0x50
    ERROR_CODE_IDX = 0x90

    DEFROST_BIT = 2
    ERROR_BIT = 0
    WARNING_BIT = 1
    ALARM_BIT = 2

    def __init__(self):
        self.defrost: Optional[bool] = None
        self.error: Optional[bool] = None
        self.warning: Optional[bool] = None
        self.alarm: Optional[bool] = None
        self.error_code: Optional[str] = None

    @staticmethod
    def _flags(value: Optional[bytes]) -> Optional[int]:
        return int.from_bytes(value, "big") if value else None

    @staticmethod
    def _error_code(value: Optional[bytes]) -> Optional[str]:
        # [unit][char][char][number]; anything else means no code to show.
        if value is None or len(value) != 4:
            return None
        _unit, first, second, number = value
        if not (chr(first).isalnum() and chr(second).isalnum()):
            return None
        return f"{chr(first)}{chr(second)}-{number:02d}"

    def set_values(self, values: Dict[int, bytearray]):
        """See base class."""
        special = self._flags(values.get(self.SPECIAL_IDX))
        if special is not None:
            self.defrost = bool(special & (1 << self.DEFROST_BIT))

        errors = self._flags(values.get(self.ERRORS_IDX))
        if errors is not None:
            self.error = bool(errors & (1 << self.ERROR_BIT))
            self.warning = bool(errors & (1 << self.WARNING_BIT))
            self.alarm = bool(errors & (1 << self.ALARM_BIT))

        if self.ERROR_CODE_IDX in values:
            self.error_code = self._error_code(bytes(values[self.ERROR_CODE_IDX]))

    def get_values(self) -> Dict[int, bytearray]:
        """See base class."""
        return {
            self.SPECIAL_IDX: bytes([0x00]),
            self.ERRORS_IDX: bytes([0x00]),
            self.ERROR_CODE_IDX: bytes([0x00]),
        }


class UnitStatus(Feature):
    """Reads the indoor unit status (defrost, error, warning, alarm, code).

    Attributes:
        status (UnitStatusStatus): Current status
    """

    def __init__(self, connection: Connection):
        """See base class."""
        self.status = None
        super().__init__(connection)

    def query_cmd_id(self) -> int:
        """See base class."""
        return 256

    def update_cmd_id(self) -> int:
        """See base class.

        Raises:
            NotImplementedException: This feature cannot be updated
        """
        raise NotImplementedException("This feature cannot be updated")

    def new_status(self) -> FeatureStatus:
        """See base class."""
        return UnitStatusStatus()
