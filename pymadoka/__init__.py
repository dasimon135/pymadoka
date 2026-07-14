from .controller import Controller
from .connection import Connection, discover_devices, force_device_disconnect
from .connection import ConnectionException
from .feature import Feature, FeatureStatus, NotImplementedException
from .features.clean_filter import CleanFilterIndicator, CleanFilterIndicatorStatus, ResetCleanFilterTimer, ResetCleanFilterTimerStatus
from .features.eye_brightness import EyeBrightness, EyeBrightnessStatus
from .features.fanspeed import FanSpeed, FanSpeedStatus, FanSpeedEnum
from .features.operationmode import OperationMode, OperationModeStatus, OperationModeEnum
from .features.power import PowerState, PowerStateStatus
from .features.setpoint import SetPoint, SetPointStatus
from .features.temperatures import Temperatures, TemperaturesStatus

# NOTE: .cli is intentionally NOT imported here: it needs the [cli] extra
# (click) and would break `import pymadoka` on a lean install.

__all__ = [
    "Controller",
    "Connection",
    "ConnectionException",
    "discover_devices",
    "force_device_disconnect",
    "Feature",
    "FeatureStatus",
    "NotImplementedException",
    "CleanFilterIndicator",
    "CleanFilterIndicatorStatus",
    "ResetCleanFilterTimer",
    "ResetCleanFilterTimerStatus",
    "EyeBrightness",
    "EyeBrightnessStatus",
    "FanSpeed",
    "FanSpeedStatus",
    "FanSpeedEnum",
    "OperationMode",
    "OperationModeStatus",
    "OperationModeEnum",
    "PowerState",
    "PowerStateStatus",
    "SetPoint",
    "SetPointStatus",
    "Temperatures",
    "TemperaturesStatus",
]
