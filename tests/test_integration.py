"""Integration tests against a simulated gun.

The simulation follows the firmware behaviour documented in PROTOCOL.md: settings
only while idle, target and fan only while heating, stop rejected when idle, and
silence when a weak power supply blocks heat.
"""

import asyncio
from datetime import timedelta
import struct
import time
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.airgo140 import coordinator as coord
from custom_components.airgo140.const import DOMAIN
from custom_components.airgo140.protocol import (
    COMMAND_UUID,
    NOTIFY_UUID,
    PARAMS_UUID,
    crc,
    decode_read,
)
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "AirGo140_7A16"
DEVICE = "airgo140_7a16"

IDLE = {
    1: 104, 2: 122, 3: 101, 4: 140, 5: 27745, 6: 3608, 7: 19, 8: 0, 9: 0, 10: 0, 11: 0,
    12: -5000, 13: -5000, 14: -5000, 15: -5000,
    16: 250, 17: 3, 18: 300, 19: 4, 20: 360, 21: 4, 22: 300, 23: 2, 24: 120, 25: 5,
    26: 5, 27: 0, 28: 0, 29: 140, 30: 140, 31: 1, 32: 5, 33: 4, 34: 0, 35: 0,
}  # fmt: skip


def _short(kind, arg):
    body = struct.pack("<III", 16, kind, arg)
    return body + struct.pack("<I", crc(body))


def status_frame(params):
    body = struct.pack("<III", 12 + 4 * len(params) + 4, 1, len(params))
    for pid, value in params.items():
        body += struct.pack("<hH", value, pid)
    return body + struct.pack("<I", crc(body))


class FakeGun:
    """A BleakClient double behaving like the firmware."""

    def __init__(self):
        self.params = dict(IDLE)
        self.writes = []
        self.notify = None
        self.disconnected_callback = None
        self.connected = True
        self.connects = 0
        self.weak_power = False
        self.reply_delay = 0.0  # The real gun answers up to 2.5 s after a write.
        self.drop_acks = False

    # BleakClient surface.
    async def start_notify(self, uuid, callback):
        assert uuid == NOTIFY_UUID
        self.notify = callback

    async def read_gatt_char(self, uuid):
        assert uuid == PARAMS_UUID
        body = struct.pack("<II", 8 + 4 * 35 + 4, 35)
        for pid in range(1, 36):
            body += struct.pack("<hH", self.params[pid], pid)
        return bytearray(body + struct.pack("<I", crc(body)))

    async def write_gatt_char(self, uuid, data, response=False):
        assert response, "FFE1 and FFE2 have no write-without-response"
        self.writes.append((uuid, bytes(data)))
        if uuid == COMMAND_UUID:
            self._command(struct.unpack_from("<I", data, 4)[0])
        else:
            self._set(decode_read(bytes(data)))

    async def disconnect(self):
        self.connected = False

    # Firmware behaviour.
    @property
    def heating(self):
        return self.params[11] != 0

    def _push(self, frame):
        self.notify(None, bytearray(frame))

    def _reply(self, frame):
        if self.drop_acks:
            return
        if self.reply_delay:
            asyncio.get_running_loop().call_later(self.reply_delay, self._push, frame)
        else:
            self._push(frame)

    def _command(self, cmd):
        if 1 <= cmd <= 5:
            if self.weak_power:
                return  # The firmware sends nothing here.
            self.params[11] = cmd
            self.params[12] = self.params[16 + (cmd - 1) * 2]
            self.params[13] = self.params[17 + (cmd - 1) * 2]
            self._reply(_short(3, cmd))
        elif cmd == 6 and self.heating:
            self.params[11] = 0
            self.params[12] = self.params[13] = -5000
            self._reply(_short(3, 6))
        else:
            self._reply(_short(3, 8))

    def _set(self, values):
        for pid, value in values.items():
            live = pid in (12, 13)
            if live != self.heating:
                self._reply(_short(4, 2))
                return
            self.params[pid] = value


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations, enable_bluetooth):
    yield


@pytest.fixture
def gun():
    return FakeGun()


@pytest.fixture
async def loaded(hass: HomeAssistant, gun):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME, data={})
    entry.add_to_hass(hass)

    async def connect(cls, device, name, disconnected_callback=None, **kwargs):
        gun.disconnected_callback = disconnected_callback
        gun.connects += 1
        return gun

    ble = BLEDevice(ADDRESS, NAME, details=None)
    with (
        patch.object(coord, "establish_connection", connect),
        patch.object(coord.bluetooth, "async_ble_device_from_address", return_value=ble),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def state(hass, entity_id):
    s = hass.states.get(entity_id)
    assert s is not None, entity_id
    return s.state


async def enable_heat(hass, entry):
    ent = er.async_get(hass)
    entity_id = ent.async_get_entity_id("select", DOMAIN, f"{ADDRESS}_heat")
    ent.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entity_id


async def test_sensors_follow_captured_values(hass, loaded):
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"
    assert state(hass, f"sensor.{DEVICE}_input_voltage") == "27.75"
    assert state(hass, f"sensor.{DEVICE}_heater_resistance") == "3.61"
    assert state(hass, f"sensor.{DEVICE}_firmware") == "1.22"
    assert state(hass, f"binary_sensor.{DEVICE}_heating") == "off"
    # -5000 means not applicable, not a temperature.
    assert state(hass, f"sensor.{DEVICE}_target_temperature") == STATE_UNKNOWN


async def test_live_status_frame_updates_only_sent_ids(hass, loaded, gun):
    gun._push(status_frame({7: 123}))
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{DEVICE}_temperature") == "123"
    assert state(hass, f"number.{DEVICE}_gear_3_temperature") == "360"


async def test_corrupt_frame_is_ignored(hass, loaded, gun):
    frame = bytearray(status_frame({7: 99}))
    frame[12] ^= 1
    gun._push(frame)
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"


async def test_heat_control_is_disabled_by_default(hass, loaded):
    ent = er.async_get(hass)
    entity_id = ent.async_get_entity_id("select", DOMAIN, f"{ADDRESS}_heat")
    assert entity_id is not None
    assert ent.async_get(entity_id).disabled
    assert hass.states.get(entity_id) is None


async def test_setting_write_sends_correct_frame(hass, loaded, gun):
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.{DEVICE}_gear_1_temperature", "value": 275},
        blocking=True,
    )
    uuid, frame = gun.writes[-1]
    assert uuid == PARAMS_UUID
    assert decode_read(frame) == {16: 275}
    assert state(hass, f"number.{DEVICE}_gear_1_temperature") == "275"


async def test_heat_lifecycle(hass, loaded, gun):
    select = await enable_heat(hass, loaded)
    assert state(hass, select) == "off"

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "gear3"}, blocking=True
    )
    gun._push(status_frame({k: gun.params[k] for k in (11, 12, 13)}))
    await hass.async_block_till_done()
    assert state(hass, select) == "gear3"
    assert state(hass, f"binary_sensor.{DEVICE}_heating") == "on"
    assert state(hass, f"number.{DEVICE}_target_temperature") == "360"
    # Stored settings are refused by the gun while heating, so they go unavailable.
    assert state(hass, f"number.{DEVICE}_volume") == STATE_UNAVAILABLE

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.{DEVICE}_target_temperature", "value": 300},
        blocking=True,
    )
    assert decode_read(gun.writes[-1][1]) == {12: 300}

    await hass.services.async_call(
        "button", "press", {"entity_id": f"button.{DEVICE}_stop_heating"}, blocking=True
    )
    gun._push(status_frame({k: gun.params[k] for k in (11, 12, 13)}))
    await hass.async_block_till_done()
    assert state(hass, select) == "off"
    assert state(hass, f"number.{DEVICE}_target_temperature") == STATE_UNAVAILABLE
    assert state(hass, f"number.{DEVICE}_volume") == "4"


async def test_stop_while_idle_reports_rejection(hass, loaded):
    with pytest.raises(HomeAssistantError, match="current state"):
        await hass.services.async_call(
            "button", "press", {"entity_id": f"button.{DEVICE}_stop_heating"}, blocking=True
        )


async def test_weak_power_supply_is_reported(hass, loaded, gun):
    select = await enable_heat(hass, loaded)
    gun.weak_power = True
    with (
        patch.object(coord, "REPLY_TIMEOUT", 0.01),
        pytest.raises(HomeAssistantError, match="power supply"),
    ):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select, "option": "gear1"}, blocking=True
        )


async def test_late_rejection_is_not_reported_as_success(hass, loaded, gun):
    gun.reply_delay = 0.7
    gun.params[11] = 1
    with pytest.raises(HomeAssistantError, match="idle"):
        await loaded.runtime_data.async_set_params({33: 2})


async def test_late_command_answer_is_waited_for(hass, loaded, gun):
    gun.reply_delay = 1.6
    await loaded.runtime_data.async_start_gear(2)
    assert gun.params[11] == 2
    # A rejection that arrives late must still surface as a rejection.
    gun.params[11] = 0
    with pytest.raises(HomeAssistantError, match="current state"):
        await loaded.runtime_data.async_stop_heating()


async def test_started_gun_is_never_reported_as_failed(hass, loaded, gun):
    # The worst failure: telling the user a running heater did not start.
    gun.drop_acks = True
    with patch.object(coord, "REPLY_TIMEOUT", 0.01):
        await loaded.runtime_data.async_start_gear(3)
    assert gun.params[11] == 3
    assert loaded.runtime_data.heating


async def test_ack_of_another_command_is_ignored(hass, loaded, gun):
    gun.drop_acks = True
    runtime = loaded.runtime_data
    with patch.object(coord, "REPLY_TIMEOUT", 0.2):
        task = asyncio.create_task(runtime.async_stop_heating())
        await asyncio.sleep(0.05)
        gun._push(_short(3, 1))  # Late answer to an earlier gear command.
        with pytest.raises(HomeAssistantError, match="did not respond|state") as err:
            gun.params[11] = 1  # Still heating: nothing confirms the stop.
            await task
    assert "did not respond" in str(err.value)


async def test_gun_rejecting_a_write_is_reported(hass, loaded, gun):
    # Simulate the state changing on the gun before Home Assistant hears about it.
    gun.params[11] = 1
    with pytest.raises(HomeAssistantError, match="idle"):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": f"number.{DEVICE}_volume", "value": 2},
            blocking=True,
        )
    assert gun.params[33] == 4


async def test_out_of_range_never_reaches_the_gun(hass, loaded, gun):
    runtime = loaded.runtime_data
    with pytest.raises(HomeAssistantError, match="outside"):
        await runtime.async_set_params({33: 9})
    with pytest.raises(HomeAssistantError, match="outside"):
        await runtime.async_set_params({16: 900})
    assert gun.writes == []


async def test_disconnect_then_reconnect(hass, loaded, gun):
    gun.disconnected_callback(gun)
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{DEVICE}_temperature") == STATE_UNAVAILABLE

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"


async def test_silent_link_is_dropped_and_reconnected(hass, loaded, gun):
    assert gun.connects == 1
    loaded.runtime_data._last_frame = time.monotonic() - 60
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    assert not gun.connected  # The dead link was closed...
    assert gun.connects == 2  # ...and replaced straight away.
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"


async def test_silent_link_reconnects_when_proxy_disconnect_times_out(hass, loaded, gun):
    async def timeout():
        raise TimeoutError("Timeout waiting for BluetoothDeviceConnectionResponse")

    gun.disconnect = timeout
    loaded.runtime_data._last_frame = time.monotonic() - 60
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    assert gun.connects == 2
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"


async def test_no_new_link_until_old_one_is_closed(hass, loaded, gun):
    release = asyncio.Event()

    async def slow():
        await release.wait()

    gun.disconnect = slow
    loaded.runtime_data._last_frame = time.monotonic() - 60
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    loaded.runtime_data._schedule_connect()  # As an advertisement would.
    await asyncio.sleep(0)
    connects_while_closing = gun.connects

    release.set()  # Before asserting: unloading would otherwise wait on it forever.
    await hass.async_block_till_done()
    assert connects_while_closing == 1
    assert gun.connects == 2


async def test_proxy_timeout_during_write_is_reported(hass, loaded, gun):
    async def timeout(*args, **kwargs):
        raise TimeoutError("proxy timeout")

    gun.write_gatt_char = timeout
    with pytest.raises(HomeAssistantError, match="proxy timeout"):
        await loaded.runtime_data.async_set_params({33: 2})


async def test_late_callback_from_replaced_link_is_ignored(hass, loaded, gun):
    gun.disconnected_callback(FakeGun())
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{DEVICE}_temperature") == "19"


async def test_disconnect_during_write_is_reported(hass, loaded, gun):
    async def fail(*args, **kwargs):
        raise BleakError("gone")

    gun.write_gatt_char = fail
    with pytest.raises(HomeAssistantError, match="gone"):
        await loaded.runtime_data.async_set_params({33: 2})


async def test_setup_succeeds_when_gun_is_unplugged(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME, data={})
    entry.add_to_hass(hass)
    with patch.object(coord.bluetooth, "async_ble_device_from_address", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert state(hass, f"sensor.{DEVICE}_temperature") == STATE_UNAVAILABLE
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def _service_info():
    return BluetoothServiceInfoBleak(
        name=NAME,
        address=ADDRESS,
        rssi=-50,
        manufacturer_data={},
        service_data={},
        service_uuids=["0000ffe0-0000-1000-8000-00805f9b34fb"],
        source="local",
        device=BLEDevice(ADDRESS, NAME, details=None),
        advertisement=None,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


async def test_config_flow_bluetooth_discovery(hass):
    with patch("custom_components.airgo140.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=_service_info(),
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == NAME
        assert result["result"].unique_id == ADDRESS


async def test_config_flow_user_lists_only_airgo(hass):
    other = _service_info()
    other.name = "SomethingElse"
    other.address = "11:22:33:44:55:66"
    with (
        patch(
            "custom_components.airgo140.config_flow.async_discovered_service_info",
            return_value=[_service_info(), other],
        ),
        patch("custom_components.airgo140.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert list(result["data_schema"].schema["address"].container) == [ADDRESS]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": ADDRESS}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_config_flow_user_no_devices(hass):
    with patch(
        "custom_components.airgo140.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
