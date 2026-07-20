"""This module contains the device controller.
"""
import asyncio
import logging

from typing import Union, Dict
from enum import Enum
from pymadoka.feature import Feature, NotImplementedException
from pymadoka.connection import (
    DEFAULT_PAIR_TIMEOUT,
    Connection,
    ConnectionException,
)
from pymadoka.features.fanspeed import FanSpeed
from pymadoka.features.operationmode import OperationMode
from pymadoka.features.power import PowerState
from pymadoka.features.setpoint import SetPoint
from pymadoka.features.temperatures import Temperatures
from pymadoka.features.clean_filter import CleanFilterIndicator,ResetCleanFilterTimer
from pymadoka.features.eye_brightness import EyeBrightness

logger = logging.getLogger(__name__)


class Controller:
    """This class implements the device controller.
    It stores all the features supported by the device and provides methods to operate globally on all the features.
    However, each feature can be queried/updated independently by accesing the feature attributes.

    Attributes:
        status (dict[string,FeatureStatus]): Last status collected from the features
        connection (Connection): Connection used to communicate with the device
        fan_speed (Feature): Feature used to control the fan speed
        operation_mode (Feature): Feature used to control the fan speed
        power_state (Feature): Feature used to control the fan speed
        set_point (Feature): Feature used to control the fan speed
        set_point (Feature): Feature used to control the fan speed
        clean_filter_indicator (Feature): Feature used to control the fan speed
    """
    def __init__(self, address: str, adapter: str = "hci0", reconnect: bool = True, hass=None, name: str = None, candidates_callback=None, pair_timeout: float = DEFAULT_PAIR_TIMEOUT):
        """Inits the controller with the device address.

        Args:
            address (str): MAC address of the device
            adapter (str): Bluetooth adapter for the connection
            hass: Home Assistant instance (enables bleak_retry_connector path)
            name (str): User-friendly display name for the device
            candidates_callback: callback returning an ordered list of BLEDevice
                candidates (preferred path first); when provided, the HA connect
                path tries them in order instead of letting HA pick by RSSI.
            pair_timeout (float): budget for one pair() call. Raise it around a
                user-driven pairing: numeric comparison needs a human to accept
                on the thermostat screen, which the default cannot accommodate.
        """

        if adapter is None:
            adapter = "hci0"

        self.status = {}
        self.info = {}
        self.connection = Connection(
            address,
            adapter=adapter,
            reconnect=reconnect,
            hass=hass,
            name=name,
            candidates_callback=candidates_callback,
            pair_timeout=pair_timeout,
        )

        self.fan_speed = FanSpeed(self.connection)
        self.operation_mode = OperationMode(self.connection)
        self.power_state = PowerState(self.connection)
        self.set_point = SetPoint(self.connection)
        self.temperatures = Temperatures(self.connection)
        self.clean_filter_indicator = CleanFilterIndicator(self.connection)
        self.reset_clean_filter_timer = ResetCleanFilterTimer(self.connection)
        self.eye_brightness = EyeBrightness(self.connection)


    async def start(self):
        """Start the connection to the device.
        """
        await self.connection.start()


    async def stop(self):
        """Stop the connection.
        """
        await self.connection.cleanup()

    async def update(self, query_retries: int = 1):
        """Iterate over all the features and query their status.

        A single feature query can fail transiently (a chunked notification
        response that could not be reassembled, or a short timeout), especially
        when the link is relayed through a BLE proxy. Such a failure is retried
        once per feature and, if it still fails, that feature is skipped rather
        than aborting the whole poll — so the rest of the features (and the
        entities that depend on them) still update.

        A dead link (ConnectionAbortedError) stops the poll immediately, and a
        poll where NO feature answered raises ConnectionException: without it a
        connected-but-unresponsive device would look like a successful update
        of stale, previously accumulated statuses.
        """

        answered = 0
        for var in vars(self).values():
            if not isinstance(var, Feature):
                continue

            for attempt in range(query_retries + 1):
                try:
                    await var.query()
                    answered += 1
                    break
                except NotImplementedException as e:
                    if not isinstance(var, ResetCleanFilterTimer):
                        raise e
                    break
                except ConnectionAbortedError as e:
                    logger.debug(f"Connection aborted: {str(e)}")
                    raise e
                except (asyncio.TimeoutError, ConnectionException) as e:
                    if attempt < query_retries:
                        logger.debug(
                            f"Query attempt {attempt + 1}/{query_retries + 1} failed "
                            f"for {var.__class__.__name__}, retrying: {str(e)}"
                        )
                        await asyncio.sleep(0.5)
                        continue
                    logger.warning(
                        f"Query failed for {var.__class__.__name__} after "
                        f"{query_retries + 1} attempts, skipping: {str(e)}"
                    )
                except Exception as e:
                    logger.error(f"Failed to update {var.__class__.__name__}: {str(e)}")
                    break

        if answered == 0:
            raise ConnectionException("No feature answered any query")


    def refresh_status(self) -> Dict[str,Union[int,str,bool,dict,Enum]]:
        """Collect the status from all the features into a single status dictionary with basic types.

        Returns:
            dict[str,Union[int,str,bool,dict,Enum]]: Dictionary with the status of each feature represented with basic types
        """
        for k,v in vars(self).items():
            if isinstance(v,Feature):
                if v.status is not None:
                    self.status[k] = vars(v.status)

        return self.status


    async def read_info(self) -> Dict[str,str]:
        """Reads the device info (Hardware revision, Software revision, Model, Manufacturer, etc)
        Returns:
            Dict[str,str]: Dictionary with the device info
        """
        self.info = await self.connection.read_info()
        return self.info
