"""This module contains the classes used to control the Temperature feature (temperatures read by the device)
"""

from typing import Dict
from pymadoka.feature import Feature, FeatureStatus, NotImplementedException
from pymadoka.connection import Connection

class TemperaturesStatus(FeatureStatus):


    INDOOR_IDX = 0x40
    OUTDOOR_IDX = 0x41

    """
    This class is used to store the indoor/outdoor temperatures.

    Attributes:
        indoor (int): Indoor temperature
        outdoor (int): Outdoor temperature
    """
    def __init__(self,indoor:int, outdoor:int):
        """Inits the feature with the indoor/outdoor temperatures.
        
        Args:
            indoor (int): Indoor temperature
            outdoor (int): Outdoor temperature
        """
        self.indoor = indoor
        self.outdoor = outdoor
   
    def set_values(self, values:Dict[str,bytearray]):
        """See base class."""
        self.indoor = values[self.INDOOR_IDX][0]
        self.outdoor = self._decode_outdoor(values[self.OUTDOOR_IDX][0])

    @staticmethod
    def _decode_outdoor(raw: int):
        """One byte: 0xFF for no outdoor sensor, else sign and magnitude.

        Bit 7 set means below zero and the low seven bits are the degrees, so
        0x85 is -5 C. That is how the protocol's reverse engineer decodes it in
        the OpenHAB binding (GetIndoorOutoorTemperatures.java). Read as a plain
        unsigned byte, -5 C came out as 133 C. No capture below 0 C has
        confirmed it yet.
        """
        if raw == 0xFF:
            return None
        if raw & 0x80:
            return -(raw & 0x7F)
        return raw
        
    def get_values(self) -> Dict[str,bytearray]:
        """See base class."""
        values = {}
        values[self.INDOOR_IDX] = (self.indoor*128).to_bytes(2,"big")
        if self.outdoor is not None:
            values[self.OUTDOOR_IDX] = (self.outdoor*128).to_bytes(2,"big")
        return values

class Temperatures(Feature):

    """
    This class is used to retrieve the indoor/outdoor temperatures as registered by the device.

    Attributes:
        status (TemperaturesStatus): Current status
    """
    def __init__(self, connection: Connection):
        """See base class."""
        self.status = None
        super().__init__(connection)

    def query_cmd_id(self) -> int:
        """See base class."""
        return 272
    
    def update_cmd_id(self) -> int:
        """See base class. This method has not been implemented.

        Raises:
            NotImplementedException: This feature cannot be updated
        """
        raise NotImplementedException("This feature cannot be updated")

    def new_status(self) -> FeatureStatus:
        """See base class."""
        return TemperaturesStatus(0,0)
