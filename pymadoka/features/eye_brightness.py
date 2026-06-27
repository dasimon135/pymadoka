"""This module contains the classes used to control the Eye Brightness feature (LED brightness of the controller).
"""

from typing import Dict
from pymadoka.feature import Feature, FeatureStatus
from pymadoka.connection import Connection


class EyeBrightnessStatus(FeatureStatus):

    """
    This class is used to store the controller eye (LED) brightness level.

    Attributes:
        brightness (int): Brightness level (0-19)
    """
    EYE_BRIGHTNESS_IDX = 0x33

    def __init__(self, brightness: int):
        """Inits the feature with the brightness level.

        Args:
            brightness (int): Brightness level (0-19)
        """
        self.brightness = brightness

    def set_values(self, values: Dict[int, bytearray]):
        """See base class."""
        self.brightness = values[self.EYE_BRIGHTNESS_IDX][0]

    def get_values(self) -> Dict[int, bytearray]:
        """See base class."""
        return {self.EYE_BRIGHTNESS_IDX: bytes([self.brightness])}


class EyeBrightness(Feature):

    """
    This class is used to retrieve and update the controller eye (LED) brightness.

    Attributes:
        status (EyeBrightnessStatus): Current status
    """
    def __init__(self, connection: Connection):
        """See base class."""
        self.status = None
        super().__init__(connection)

    def query_cmd_id(self) -> int:
        """See base class."""
        return 770

    def update_cmd_id(self) -> int:
        """See base class."""
        return 17154

    def new_status(self) -> FeatureStatus:
        """See base class."""
        return EyeBrightnessStatus(0)
