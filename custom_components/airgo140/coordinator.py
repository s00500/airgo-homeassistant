"""Connection handling for one AirGo 140.

The gun is usually unplugged, so being unreachable is normal: entities go
unavailable and a timer reconnects as soon as Home Assistant sees it again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import time

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .protocol import (
    COMMAND_UUID,
    NOTIFY_UUID,
    PARAMS_UUID,
    Command,
    Param,
    ProtocolError,
    SetError,
    decode_notification,
    decode_read,
    encode_command,
    encode_params,
    value_range,
)

_LOGGER = logging.getLogger(__name__)

REPLY_TIMEOUT = 4.0  # Answer frames were measured arriving up to 2.5 s after a write.
LINK_TIMEOUT = 15.0  # The gun pushes status about once per second.
TICK = timedelta(seconds=15)

SET_ERROR_TEXT = {
    SetError.WRONG_STATE: (
        "Not possible right now: settings need the gun idle, "
        "target temperature and fan need it heating"
    ),
    SetError.UNKNOWN_ID: "The gun does not know this parameter",
    SetError.BAD_VALUE: "The gun refused this value",
}


class AirGoCoordinator(DataUpdateCoordinator[dict[int, int]]):
    """Holds the BLE link and the latest parameters."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=None
        )
        self.address: str = entry.unique_id or ""
        self.data = {}
        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()
        self._last_frame = 0.0
        self._ack: asyncio.Future[bool] | None = None
        self._ack_expected = 0
        self._set_error: asyncio.Future[int] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._stopping = False

    @property
    def connected(self) -> bool:
        """Return whether the link is up and has delivered parameters."""
        return self._client is not None and bool(self.data)

    @property
    def heating(self) -> bool:
        """Return whether a heating session is active."""
        return self.data.get(Param.ACTIVE_GEAR, 0) != 0

    @property
    def fahrenheit(self) -> bool:
        """Return whether the gun reports Fahrenheit."""
        return self.data.get(Param.TEMP_UNIT, 0) == 1

    async def async_start(self) -> None:
        """Start watching for the gun. Never fails if it is unplugged."""
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_tick, TICK)
        )
        self._schedule_connect()

    async def async_stop(self) -> None:
        """Stop and disconnect."""
        self._stopping = True
        while self._unsubs:
            self._unsubs.pop()()
        client, self._client = self._client, None
        if client is not None:
            await self._async_disconnect(client)

    async def _async_disconnect(self, client: BleakClient) -> None:
        """Close a link. A proxy may time out here, and the link is gone to us anyway."""
        try:
            await client.disconnect()
        except (BleakError, TimeoutError, OSError):
            _LOGGER.debug("%s: disconnect failed", self.name, exc_info=True)

    @callback
    def _on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self._schedule_connect()

    async def _on_tick(self, _now: object) -> None:
        client = self._client
        if client is None:
            self._schedule_connect()
        elif time.monotonic() - self._last_frame > LINK_TIMEOUT:
            _LOGGER.debug("%s: status stream stopped, reconnecting", self.name)
            self._client = None
            self.async_update_listeners()
            # A proxy can take 20 s to answer, and it disconnects by address, so a new
            # link must not start until this is done.
            async with self._connect_lock:
                await self._async_disconnect(client)
            self._schedule_connect()

    @callback
    def _schedule_connect(self) -> None:
        if self._stopping or self._client is not None or self._connect_lock.locked():
            return
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            return
        self.config_entry.async_create_background_task(
            self.hass, self._async_connect(device), f"{DOMAIN} connect {self.address}"
        )

    async def _async_connect(self, device) -> None:
        async with self._connect_lock:
            if self._client is not None or self._stopping:
                return
            client: BleakClient | None = None
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=2,
                )
                self._last_frame = time.monotonic()
                await client.start_notify(NOTIFY_UUID, self._on_notify)
                self._client = client
                await self._async_read(client)
            except (BleakError, TimeoutError, OSError) as err:
                _LOGGER.debug("%s: connect failed: %s", self.name, err)
                self._client = None
                if client is not None:
                    await self._async_disconnect(client)
                self.async_update_listeners()

    @callback
    def _on_disconnect(self, client: BleakClient) -> None:
        if self._client is not client:
            return  # A link we already replaced; it must not touch the current one.
        self._client = None
        for future in (self._ack, self._set_error):
            if future is not None and not future.done():
                future.cancel()
        self.async_update_listeners()

    @callback
    def _on_notify(self, _sender: object, data: bytearray) -> None:
        try:
            frame = decode_notification(bytes(data))
        except ProtocolError as err:
            _LOGGER.debug("%s: dropped frame: %s", self.name, err)
            return
        self._last_frame = time.monotonic()
        if frame.ack_rejected or frame.ack_command is not None:
            # An ack for another command is a late answer to an earlier one.
            if (
                self._ack is not None
                and not self._ack.done()
                and (frame.ack_rejected or frame.ack_command == self._ack_expected)
            ):
                self._ack.set_result(not frame.ack_rejected)
        elif frame.set_error is not None:
            if self._set_error is not None and not self._set_error.done():
                self._set_error.set_result(frame.set_error)
        elif frame.params:
            self.async_set_updated_data({**self.data, **frame.params})

    async def _async_read(self, client: BleakClient) -> None:
        """Read all parameters. Status frames only carry IDs 1-15."""
        data = await client.read_gatt_char(PARAMS_UUID)
        try:
            params = decode_read(bytes(data))
        except ProtocolError as err:
            # A link MTU under 155 truncates this frame and the firmware refuses
            # long reads; live status (79+) can still work.
            _LOGGER.warning(
                "%s: settings unreadable (%d bytes, link MTU too small?): %s",
                self.name,
                len(data),
                err,
            )
            return
        self.async_set_updated_data({**self.data, **params})

    def _require_client(self) -> BleakClient:
        if self._client is None:
            raise HomeAssistantError(f"{self.name} is not connected")
        return self._client

    async def async_set_params(self, params: dict[int, int]) -> None:
        """Write parameters after range-checking them, and verify by reading back."""
        for pid, value in params.items():
            low, high = value_range(pid, self.fahrenheit)
            if not low <= value <= high:
                raise HomeAssistantError(
                    f"Value {value} for parameter {int(pid)} is outside {low}-{high}"
                )
        frame = encode_params(params)
        async with self._op_lock:
            client = self._require_client()
            self._set_error = self.hass.loop.create_future()
            try:
                await client.write_gatt_char(PARAMS_UUID, frame, response=True)
                # Success sends no frame and a rejection arrives seconds later, so the
                # state decides: the firmware applies a write before answering it.
                await self._async_read(client)
                if all(self.data.get(pid) == value for pid, value in params.items()):
                    return
                done, _ = await asyncio.wait({self._set_error}, timeout=REPLY_TIMEOUT)
                if self._set_error.cancelled():
                    raise HomeAssistantError(f"{self.name} disconnected")
                if not done:
                    raise HomeAssistantError("The gun did not apply the value")
                code = self._set_error.result()
                raise HomeAssistantError(
                    SET_ERROR_TEXT.get(code, f"The gun rejected the write ({code})")
                )
            except (BleakError, TimeoutError) as err:
                raise HomeAssistantError(f"Write failed: {err}") from err
            finally:
                self._set_error.cancel()
                self._set_error = None

    async def async_command(
        self, command: int, confirmed: Callable[[dict[int, int]], bool]
    ) -> None:
        """Send a command. Without an answer, the gun's actual state decides."""
        async with self._op_lock:
            client = self._require_client()
            self._ack = self.hass.loop.create_future()
            self._ack_expected = command
            try:
                await client.write_gatt_char(
                    COMMAND_UUID, encode_command(command), response=True
                )
                done, _ = await asyncio.wait({self._ack}, timeout=REPLY_TIMEOUT)
                if self._ack.cancelled():
                    raise HomeAssistantError(f"{self.name} disconnected")
                if not done:
                    await self._async_read(client)
                    if confirmed(self.data):
                        return
                    # The firmware stays silent when a weak power supply blocks heat.
                    raise HomeAssistantError(
                        "The gun did not respond; a too-weak power supply is the usual cause"
                    )
                if not self._ack.result():
                    raise HomeAssistantError("Not possible in the gun's current state")
            except (BleakError, TimeoutError) as err:
                raise HomeAssistantError(f"Command failed: {err}") from err
            finally:
                self._ack.cancel()
                self._ack = None

    async def async_start_gear(self, gear: int) -> None:
        """Start heating on a gear, or switch to it while heating."""
        if not 1 <= gear <= 5:
            raise HomeAssistantError(f"Gear {gear} is outside 1-5")
        await self.async_command(gear, lambda d: d.get(Param.ACTIVE_GEAR) == gear)

    async def async_stop_heating(self) -> None:
        """Stop heating."""
        await self.async_command(Command.STOP, lambda d: d.get(Param.ACTIVE_GEAR) == 0)
