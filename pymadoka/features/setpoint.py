"""This module contains the classes used to control the Set Point feature (temperatures set by the user)
"""

from typing import Dict
from pymadoka.feature import Feature, FeatureStatus
from pymadoka.connection import Connection

class SetPointStatus(FeatureStatus):
    """
    This class is used to store the Set Point temperatures.
    
    The values must be set as in Celsius degrees and are converted to the device format when read/written.

    No ranges validation is performed.

    Attributes:
        cooling_set_point (int): Cooling set point
        heating_set_point (int): Heating set point
    """
    
    COOLING_IDX = (0x20,2)
    HEATING_IDX = (0x21,2)
    RANGE_ENABLED_IDX = (0x30,1)
    MODE_IDX = (0x31,1)
    MINIMUM_DIFFERENTIAL_IDX = (0x32,1)
    MIN_COOLING_LOWERLIMIT_IDX = (0xa0,1)
    MIN_HEATING_LOWERLIMIT_IDX = (0xa1,1)
    COOLING_LOWERLIMIT_IDX = (0xa2,2)
    HEATING_LOWERLIMIT_IDX = (0xa3,2)
    COOLING_LOWERLIMIT_SYMBOL_IDX = (0xa4,1)
    HEATING_LOWERLIMIT_SYMBOL_IDX = (0xa5,1)
    MAX_COOLING_UPPERLIMIT_IDX = (0xb0,1)
    MAX_HEATING_UPPERLIMIT_IDX = (0xb1,1)
    COOLING_UPPERLIMIT_IDX = (0xb2,2)
    HEATING_UPPERLIMIT_IDX = (0xb3,2)
    COOLING_UPPERLIMIT_SYMBOL_IDX = (0xb4,1)
    HEATING_UPPERLIMIT_SYMBOL_IDX = (0xb5,1)
    
    def __init__(self,cooling_set_point:int, heating_set_point:int):
        """Inits the status with the set points

        Args:
            cooling_set_point (int): Cooling set point
            heating_set_point (int): Heating set point
        """
        # Raw params captured on parse; echoed back on serialize so an update
        # does not clobber device-side settings (range mode, limits).
        self._raw_values = {}
        self.cooling_set_point = cooling_set_point
        self.heating_set_point = heating_set_point
        self.range_enabled = 0
        self.mode = 0
        self.min_differential = 0
        self.min_cooling_lowerlimit = 0
        self.min_heating_lowerlimit = 0
        self.cooling_lowerlimit = 0
        self.heating_lowerlimit = 0
        self.cooling_lowerlimit_symbol = 0
        self.heating_lowerlimit_symbol = 0
        self.max_cooling_upperlimit = 0
        self.max_heating_upperlimit = 0
        self.cooling_upperlimit = 0
        self.heating_upperlimit = 0
        self.cooling_upperlimit_symbol = 0
        self.heating_upperlimit_symbol = 0
        
    @staticmethod
    def _temperature(values:Dict[int,bytes], idx, default:int=0) -> int:
        """Decode a 2-byte temperature param (device format = value * 128)."""
        raw = values.get(idx[0])
        if raw is None:
            return default
        return round(int.from_bytes(raw,"big")/128.0)

    @staticmethod
    def _flag(values:Dict[int,bytes], idx, default:int=0) -> int:
        """Decode a 1-byte param (raw value, NOT scaled by 128).

        Verified on hardware (BRC1H diagnostics dump): mode arrives as 0x01,
        max_cooling_upperlimit as 0x20 (=32) — plain integers. The historical
        /128 decode read every 1-byte param as 0, which silently disabled
        range mode detection.
        """
        raw = values.get(idx[0])
        if raw is None:
            return default
        return int.from_bytes(raw,"big")

    def set_values(self, values:Dict[str,bytearray]):
        """See base class.

        Only the set points are mandatory; every other param is optional so a
        firmware variant (or a shorter response) cannot fail the whole parse.
        """

        self._raw_values = {key: bytes(value) for key, value in values.items()}
        self.cooling_set_point = round(int.from_bytes(values[self.COOLING_IDX[0]],"big")/128.0)
        self.heating_set_point = round(int.from_bytes(values[self.HEATING_IDX[0]],"big")/128.0)
        self.range_enabled = self._flag(values, self.RANGE_ENABLED_IDX)
        self.mode = self._flag(values, self.MODE_IDX)
        self.min_differential = self._flag(values, self.MINIMUM_DIFFERENTIAL_IDX)
        self.min_cooling_lowerlimit = self._flag(values, self.MIN_COOLING_LOWERLIMIT_IDX)
        self.min_heating_lowerlimit = self._flag(values, self.MIN_HEATING_LOWERLIMIT_IDX)
        self.cooling_lowerlimit = self._temperature(values, self.COOLING_LOWERLIMIT_IDX)
        self.heating_lowerlimit = self._temperature(values, self.HEATING_LOWERLIMIT_IDX)
        self.cooling_lowerlimit_symbol = self._flag(values, self.COOLING_LOWERLIMIT_SYMBOL_IDX)
        self.heating_lowerlimit_symbol = self._flag(values, self.HEATING_LOWERLIMIT_SYMBOL_IDX)
        self.max_cooling_upperlimit = self._flag(values, self.MAX_COOLING_UPPERLIMIT_IDX)
        self.max_heating_upperlimit = self._flag(values, self.MAX_HEATING_UPPERLIMIT_IDX)
        self.cooling_upperlimit = self._temperature(values, self.COOLING_UPPERLIMIT_IDX)
        self.heating_upperlimit = self._temperature(values, self.HEATING_UPPERLIMIT_IDX)
        self.cooling_upperlimit_symbol = self._flag(values, self.COOLING_UPPERLIMIT_SYMBOL_IDX)
        self.heating_upperlimit_symbol = self._flag(values, self.HEATING_UPPERLIMIT_SYMBOL_IDX)
        
        
    def get_values(self) -> Dict[str,bytearray]:
        """See base class.

        A freshly built status serializes the legacy default payload (used for
        queries). A status parsed from the device echoes the device's own raw
        params back, with only the set points replaced, so updating a set point
        does not reset range mode or the configured limits.
        """
        values = {}
        values[self.COOLING_IDX[0]] = (self.cooling_set_point*128).to_bytes(self.COOLING_IDX[1],"big")
        values[self.HEATING_IDX[0]] = (self.heating_set_point*128).to_bytes(self.HEATING_IDX[1],"big")
        values[self.RANGE_ENABLED_IDX[0]] = (0).to_bytes(self.RANGE_ENABLED_IDX[1],"big")
        values[self.MODE_IDX[0]] = (2).to_bytes(self.MODE_IDX[1],"big")
        values[self.MINIMUM_DIFFERENTIAL_IDX[0]] = (0).to_bytes(self.MINIMUM_DIFFERENTIAL_IDX[1],"big")
        values[self.MIN_COOLING_LOWERLIMIT_IDX[0]] = (0).to_bytes(self.MIN_COOLING_LOWERLIMIT_IDX[1],"big")
        values[self.MIN_HEATING_LOWERLIMIT_IDX[0]] = (0).to_bytes(self.MIN_HEATING_LOWERLIMIT_IDX[1],"big")
        values[self.COOLING_LOWERLIMIT_IDX[0]] = (0).to_bytes(self.COOLING_LOWERLIMIT_IDX[1],"big")
        values[self.HEATING_LOWERLIMIT_IDX[0]] = (0).to_bytes(self.HEATING_LOWERLIMIT_IDX[1],"big")
        values[self.COOLING_LOWERLIMIT_SYMBOL_IDX[0]] = (0).to_bytes(self.COOLING_LOWERLIMIT_SYMBOL_IDX[1],"big")
        values[self.HEATING_LOWERLIMIT_SYMBOL_IDX[0]] = (0).to_bytes(self.HEATING_LOWERLIMIT_SYMBOL_IDX[1],"big")
        values[self.MAX_COOLING_UPPERLIMIT_IDX[0]] = (0).to_bytes(self.MAX_COOLING_UPPERLIMIT_IDX[1],"big")
        values[self.MAX_HEATING_UPPERLIMIT_IDX[0]] = (0).to_bytes(self.MAX_HEATING_UPPERLIMIT_IDX[1],"big")
        values[self.COOLING_UPPERLIMIT_IDX[0]] = (0).to_bytes(self.COOLING_UPPERLIMIT_IDX[1],"big")
        values[self.HEATING_UPPERLIMIT_IDX[0]] = (0).to_bytes(self.HEATING_UPPERLIMIT_IDX[1],"big")
        values[self.COOLING_UPPERLIMIT_SYMBOL_IDX[0]] = (0).to_bytes(self.COOLING_UPPERLIMIT_SYMBOL_IDX[1],"big")
        values[self.HEATING_UPPERLIMIT_SYMBOL_IDX[0]] = (0).to_bytes(self.HEATING_UPPERLIMIT_SYMBOL_IDX[1],"big")

        if self._raw_values:
            for key, raw in self._raw_values.items():
                if key not in (self.COOLING_IDX[0], self.HEATING_IDX[0]):
                    values[key] = raw

        return values

class SetPoint(Feature):
    """
    This class is used to control the Set Point temperatures (temperatures set by the user)

    Attributes:
        status (SetPointStatus): Current status
    """
    def __init__(self, connection: Connection):
        """See base class."""
        self.status = None
        super().__init__(connection)

    def query_cmd_id(self) -> int:
        """See base class."""
        return 64
    
    def update_cmd_id(self) -> int:
        """See base class."""
        return 16448

    def new_status(self) -> FeatureStatus:
        """See base class."""
        return SetPointStatus(0,0)
