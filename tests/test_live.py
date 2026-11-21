"""Runs the integration against a real gun. Opt in with AIRGO_LIVE=1.

Changes nothing on the gun: it only reads, and sends stop while idle, which the
firmware rejects. It refuses to run if the gun is heating.
"""

import asyncio
import os
from unittest.mock import patch

from bleak import BleakClient, BleakScanner
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_socket import enable_socket, socket_allow_hosts

from custom_components.airgo140 import coordinator as coord
from custom_components.airgo140.const import DOMAIN
from custom_components.airgo140.protocol import Param
from homeassistant.exceptions import HomeAssistantError

pytestmark = pytest.mark.skipif(
    os.environ.get("AIRGO_LIVE") != "1", reason="set AIRGO_LIVE=1 with a gun in range"
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    # Not enable_bluetooth: that fixture mocks the scanner, and this needs the real
    # radio. The Bluetooth components count as set up so they stay out of the way.
    yield


def _macos_backends():
    """The harness reports Linux, so bleak has to be told about macOS explicitly."""
    try:
        from bleak.backends.corebluetooth.client import BleakClientCoreBluetooth
        from bleak.backends.corebluetooth.scanner import BleakScannerCoreBluetooth
    except ImportError:
        return {}, {}
    return {"backend": BleakScannerCoreBluetooth}, {"backend": BleakClientCoreBluetooth}


async def _real_connection(cls, device, name, disconnected_callback=None, **kwargs):
    client = BleakClient(
        device, disconnected_callback=disconnected_callback, **_macos_backends()[1]
    )
    await client.connect()
    return client


async def test_real_gun(hass):
    # The harness blocks sockets to keep tests offline. The Bluetooth stack needs a
    # local Unix socket, so this opt-in test allows that and nothing beyond it.
    enable_socket()
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    device = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith("AirGo140_"),
        timeout=15,
        **_macos_backends()[0],
    )
    assert device is not None, "no gun in range"
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=device.address, title=device.name, data={}
    )
    entry.add_to_hass(hass)
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})

    with (
        patch.object(coord, "establish_connection", _real_connection),
        patch.object(coord.bluetooth, "async_register_callback", return_value=lambda: None),
        patch.object(
            coord.bluetooth, "async_ble_device_from_address", return_value=device
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        for _ in range(100):
            if coordinator.connected and len(coordinator.data) == 35:
                break
            await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        try:
            assert coordinator.connected
            assert len(coordinator.data) == 35
            assert not coordinator.heating, "gun is heating; test only runs idle"

            slug = device.name.lower()
            temp = float(hass.states.get(f"sensor.{slug}_temperature").state)
            volts = float(hass.states.get(f"sensor.{slug}_input_voltage").state)
            print(f"\nreal gun: {temp} deg, {volts} V, firmware "
                  f"{hass.states.get(f'sensor.{slug}_firmware').state}, "
                  f"gear 3 {hass.states.get(f'number.{slug}_gear_3_temperature').state}")
            assert -20 < temp < 500
            assert 4 < volts < 30

            # Status keeps flowing, proving notifications reach the integration.
            before = coordinator._last_frame
            await asyncio.sleep(3)
            assert coordinator._last_frame > before

            # Rejected by the firmware while idle; proves write and ack round trip.
            with pytest.raises(HomeAssistantError, match="current state"):
                await coordinator.async_stop_heating()
            # Same for a live parameter write.
            with pytest.raises(HomeAssistantError, match="idle"):
                await coordinator.async_set_params({Param.TARGET_TEMP: 300})
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
