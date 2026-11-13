"""BLE API client for the ChefSteps Joule Sous Vide.

All methods are async. Uses bleak-retry-connector for reliable connection
establishment within HA's bluetooth stack.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from bleak import BleakClient, BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.core import HomeAssistant

from .const import (
    FILE_CHAR_UUID,
    JOULE_MANUFACTURER_ID,
    SUBSCRIBE_CHAR_UUID,
    WRITE_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)


class JouleBLEError(Exception):
    """Raised for any BLE communication failure with the Joule device."""


def mac_to_bytes(mac: str) -> bytes:
    """Convert a MAC address string like 'CF:8D:98:27:9B:98' to 6 bytes."""
    return bytes(int(b, 16) for b in mac.split(":"))


class JouleBLEAPI:
    """Manages the BLE connection and GATT characteristic I/O."""

    def __init__(self, hass: HomeAssistant, mac_address: str) -> None:
        self._hass = hass
        self.mac_address = mac_address
        self._client: BleakClient | None = None
        self._is_local: bool = True  # assume local until connect()
        # 8-byte circulator address from BLE manufacturer advertising data.
        # The Joule advertises under company ID 0x0159 (ChefSteps); the payload
        # is the 8-byte address used as recipientAddress in protobuf messages.
        self.recipient_address: bytes = self._extract_circulator_address()
        # 8-byte sender address — SDK uses JWT token field 'a', or falls back
        # to "aabbaabbaabbaabb" when no cloud token is available.
        self.sender_address: bytes = bytes.fromhex("aabbaabbaabbaabb")

    def _extract_circulator_address(self) -> bytes:
        """Extract the 8-byte circulator address from BLE manufacturer data.

        The Joule advertises manufacturer-specific data under company ID 0x0159.
        HA's bluetooth scanner strips the 2-byte company ID prefix, so the value
        at key 0x0159 is the raw 8-byte circulator address.

        Falls back to the MAC padded to 8 bytes if manufacturer data is not
        (yet) available — this fallback will likely NOT work but allows setup
        to proceed so the user sees a helpful error rather than a crash.
        """
        try:
            for info in async_discovered_service_info(self._hass, connectable=True):
                if info.address.upper() == self.mac_address.upper():
                    if JOULE_MANUFACTURER_ID in info.manufacturer_data:
                        addr = bytes(info.manufacturer_data[JOULE_MANUFACTURER_ID])
                        _LOGGER.warning(
                            "Circulator address from manufacturer data: %s (%d bytes)",
                            addr.hex(), len(addr),
                        )
                        return addr
                    _LOGGER.warning(
                        "Found device %s but no manufacturer data for ID 0x%04X. "
                        "Available keys: %s",
                        self.mac_address,
                        JOULE_MANUFACTURER_ID,
                        list(info.manufacturer_data.keys()),
                    )
                    break
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Could not query bluetooth scanner for manufacturer data"
            )

        # Fallback: pad MAC to 8 bytes (unlikely to work)
        fallback = mac_to_bytes(self.mac_address) + b"\x00\x00"
        _LOGGER.warning(
            "Using padded MAC as fallback circulator address: %s", fallback.hex(),
        )
        return fallback

    async def ensure_connected(self) -> bool:
        """Connect to the device if not already connected.

        Returns True if a fresh connection was established (caller should
        re-subscribe to notifications), False if already connected.
        """
        if self._client is not None and self._client.is_connected:
            return False
        await self.connect()
        return True

    def _find_best_ble_device(self) -> tuple:
        """Find the best BLEDevice, preferring local adapters over proxies.

        ESPHome Bluetooth Proxies may not support notifications, MTU exchange,
        or manufacturer data forwarding.  Local BlueZ adapters (hci0/hci1)
        handle all of these correctly.

        Returns (ble_device, source, is_local).
        """
        local_device = None
        local_source = None
        proxy_device = None
        proxy_source = None

        for info in async_discovered_service_info(self._hass, connectable=True):
            if info.address.upper() != self.mac_address.upper():
                continue

            source = info.source
            # Local BlueZ adapters have MAC-formatted sources (XX:XX:XX:XX:XX:XX)
            # ESPHome proxies have config entry ID sources (hex without colons)
            is_local = ":" in source and len(source) == 17

            _LOGGER.warning(
                "Joule seen by source=%s type=%s rssi=%s mfr_keys=%s",
                source,
                "LOCAL" if is_local else "PROXY",
                getattr(info, "rssi", "N/A"),
                list(info.manufacturer_data.keys()),
            )

            # Refresh circulator address from manufacturer data if available
            if JOULE_MANUFACTURER_ID in info.manufacturer_data:
                addr = bytes(info.manufacturer_data[JOULE_MANUFACTURER_ID])
                if len(addr) >= 8:
                    self.recipient_address = addr[:8]
                    _LOGGER.warning(
                        "Updated circulator address from %s: %s",
                        source, self.recipient_address.hex(),
                    )

            if is_local:
                local_device = info.device
                local_source = source
            else:
                proxy_device = info.device
                proxy_source = source

        if local_device:
            return local_device, local_source, True
        if proxy_device:
            return proxy_device, proxy_source, False

        # Fallback to HA's default selection
        fallback = async_ble_device_from_address(
            self._hass, self.mac_address, connectable=True
        )
        return fallback, "default", False

    async def connect(self) -> None:
        """Open a BLE connection to the device via HA's bluetooth stack."""
        try:
            ble_device, source, is_local = self._find_best_ble_device()
            if ble_device is None:
                raise JouleBLEError(
                    f"Device {self.mac_address} not found by bluetooth scanner"
                )
            _LOGGER.warning(
                "Connecting via %s (%s) name=%s rssi=%s",
                source,
                "LOCAL" if is_local else "PROXY",
                ble_device.name,
                getattr(ble_device, "rssi", "N/A"),
            )

            self._is_local = is_local

            client = await establish_connection(
                BleakClient, ble_device, self.mac_address
            )
            self._client = client

            # Request MTU exchange — the Nordic nRF SoftDevice may need a
            # larger MTU before it forwards writes to the application layer.
            # Default MTU is 23 (20-byte payload) which is too small for
            # full StreamMessage (30 bytes).
            await self._request_mtu(client)
            _LOGGER.warning(
                "Connected to Joule at %s (MTU=%d)",
                self.mac_address,
                client.mtu_size,
            )
        except BleakError as err:
            self._client = None
            raise JouleBLEError(f"Failed to connect to {self.mac_address}") from err
        except JouleBLEError:
            raise
        except Exception as err:
            self._client = None
            raise JouleBLEError(
                f"BLE backend error for {self.mac_address}: {err}"
            ) from err

    @property
    def mtu_size(self) -> int:
        """Return the current negotiated MTU (0 if not connected)."""
        if self._client is None:
            return 0
        try:
            return self._client.mtu_size
        except Exception:  # noqa: BLE001
            return 0

    @property
    def is_connected_via_proxy(self) -> bool:
        """Return True if connected through an ESPHome Bluetooth Proxy."""
        return (
            not self._is_local
            and self._client is not None
            and self._client.is_connected
        )

    async def _request_mtu(self, client: BleakClient) -> None:
        """Try to negotiate a larger MTU with the device.

        On BlueZ, AcquireWrite/AcquireNotify trigger MTU exchange.
        We also try the internal _acquire_mtu() if available.
        """
        try:
            # Try bleak's internal MTU acquisition (BlueZ-specific)
            backend = getattr(client, "_backend", None)
            if backend and hasattr(backend, "_acquire_mtu"):
                await backend._acquire_mtu()
                _LOGGER.warning(
                    "MTU after _acquire_mtu: %d", client.mtu_size,
                )
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("_acquire_mtu failed: %s", err)

        # Fallback: try to exchange MTU via D-Bus
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import BusType, Message, MessageType

            dbus_path = (
                f"/org/bluez/hci0/dev_{self.mac_address.replace(':', '_')}"
            )
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                # Request ATT MTU exchange by writing a tiny value to any
                # writable char — this triggers BlueZ to negotiate MTU.
                # We use the GATT characteristic AcquireWrite method.
                for service in client.services:
                    for char in service.characteristics:
                        if "write" in char.properties:
                            char_path = f"{dbus_path}/service000a/char{char.handle:04x}"
                            try:
                                reply = await bus.call(
                                    Message(
                                        destination="org.bluez",
                                        path=char.path,
                                        interface="org.bluez.GattCharacteristic1",
                                        member="AcquireWrite",
                                        signature="a{sv}",
                                        body=[{}],
                                    )
                                )
                                if reply.message_type == MessageType.METHOD_RETURN:
                                    fd, mtu = reply.body
                                    _LOGGER.warning(
                                        "AcquireWrite MTU: %d", mtu,
                                    )
                                    fd.close()
                                    return
                            except Exception:  # noqa: BLE001
                                pass
                            break
                    break
            finally:
                bus.disconnect()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("D-Bus MTU exchange failed: %s", err)

    async def enable_service_changed_indications(self) -> bool:
        """Enable indications on the Service Changed characteristic (0x2A05).

        The Joule firmware requires Service Changed indications to be enabled
        before it will process any application-level commands.  The official
        iOS app writes 0x0200 (enable indications) to the CCCD on 0x2A05 as
        the very first GATT operation after connecting.

        On BlueZ, raw CCCD descriptor writes on 0x2A05 return NotPermitted
        because the GATT service requires encryption.  Using start_notify()
        goes through BlueZ's StartNotify D-Bus API which handles encryption
        negotiation transparently (same as CoreBluetooth on iOS).
        """
        if self._client is None:
            return False

        service_changed_uuid = "00002a05-0000-1000-8000-00805f9b34fb"

        for service in self._client.services:
            for char in service.characteristics:
                if char.uuid == service_changed_uuid:
                    # Method 1: start_notify() — uses BlueZ StartNotify D-Bus
                    # API which handles encryption and CCCD writes internally.
                    # This enables indications (0x0200) for indicate-capable
                    # chars, or notifications (0x0100) for notify-capable chars.
                    try:
                        await self._client.start_notify(
                            char, lambda _c, _d: None,
                        )
                        # Verify CCCD value — must be 0x0200 (indications),
                        # not 0x0100 (notifications)
                        cccd_uuid = "00002902-0000-1000-8000-00805f9b34fb"
                        for desc in char.descriptors:
                            if desc.uuid == cccd_uuid:
                                try:
                                    val = bytes(
                                        await self._client.read_gatt_descriptor(
                                            desc.handle
                                        )
                                    )
                                    _LOGGER.warning(
                                        "0x2A05 CCCD (handle %d) = %s (%s)",
                                        desc.handle,
                                        val.hex(),
                                        "indications"
                                        if val == b"\x02\x00"
                                        else "notifications"
                                        if val == b"\x01\x00"
                                        else "unknown",
                                    )
                                    # If start_notify wrote 0x0100 instead of
                                    # 0x0200, force-write the correct value
                                    if val == b"\x01\x00":
                                        _LOGGER.warning(
                                            "Fixing: writing 0x0200 "
                                            "(indications) to CCCD"
                                        )
                                        await self._client.write_gatt_descriptor(
                                            desc.handle, b"\x02\x00"
                                        )
                                except BleakError as verr:
                                    _LOGGER.warning(
                                        "CCCD read/fix on 0x2A05 failed: %s",
                                        verr,
                                    )
                                break
                        _LOGGER.warning(
                            "Service Changed indications enabled via "
                            "start_notify (uuid=%s, props=%s)",
                            char.uuid,
                            char.properties,
                        )
                        return True
                    except BleakError as err:
                        _LOGGER.warning(
                            "start_notify on 0x2A05 failed: %s — "
                            "trying raw CCCD write",
                            err,
                        )

                    # Method 2: fallback to raw CCCD write (may work on
                    # non-BlueZ platforms like macOS/Windows)
                    cccd_uuid = "00002902-0000-1000-8000-00805f9b34fb"
                    for desc in char.descriptors:
                        if desc.uuid == cccd_uuid:
                            try:
                                await self._client.write_gatt_descriptor(
                                    desc.handle, b"\x02\x00"
                                )
                                _LOGGER.warning(
                                    "Service Changed indications enabled "
                                    "via raw CCCD write (handle %d)",
                                    desc.handle,
                                )
                                return True
                            except BleakError as err2:
                                _LOGGER.warning(
                                    "Raw CCCD write also failed: %s", err2,
                                )
                                return False

                    _LOGGER.warning(
                        "Service Changed char found but no CCCD descriptor"
                    )
                    return False

        _LOGGER.warning("Service Changed characteristic (0x2A05) not found")
        return False

    async def verify_and_enable_notifications(self) -> bool:
        """Verify CCCD on 4325 is 0x0001, manually write if needed.

        Must be called AFTER subscribe().  Returns True if notifications
        are confirmed enabled, False otherwise.

        The v0.9.6 GATT dump showed CCCD = 0x0000 (disabled) before subscribe.
        If bleak's start_notify() didn't successfully write the CCCD, this
        method writes it manually.
        """
        if self._client is None:
            return False

        cccd_uuid = "00002902-0000-1000-8000-00805f9b34fb"
        for service in self._client.services:
            for char in service.characteristics:
                if char.uuid == SUBSCRIBE_CHAR_UUID:
                    for desc in char.descriptors:
                        if desc.uuid == cccd_uuid:
                            return await self._check_and_write_cccd(desc.handle)
        _LOGGER.warning("CCCD descriptor not found on 4325!")
        return False

    async def _check_and_write_cccd(self, handle: int) -> bool:
        """Read CCCD at handle, write 0x0001 if not already enabled."""
        try:
            val = bytes(await self._client.read_gatt_descriptor(handle))
            _LOGGER.warning(
                "CCCD on 4325 (handle %d) after subscribe: %s",
                handle, val.hex(),
            )
            if val == b"\x00\x00":
                _LOGGER.warning(
                    "Notifications NOT enabled! Manually writing CCCD 0x0001..."
                )
                await self._client.write_gatt_descriptor(
                    handle, b"\x01\x00"
                )
                val2 = bytes(
                    await self._client.read_gatt_descriptor(handle)
                )
                _LOGGER.warning("CCCD after manual write: %s", val2.hex())
                return val2 == b"\x01\x00"
            if val == b"\x01\x00":
                _LOGGER.warning("CCCD correctly shows notifications enabled")
                return True
            _LOGGER.warning("CCCD unexpected value: %s", val.hex())
            return False
        except BleakError as err:
            _LOGGER.warning("CCCD verify/write failed: %s", err)
            return False

    async def disconnect(self) -> None:
        """Close the BLE connection."""
        try:
            if self._client is not None:
                await self._client.disconnect()
        except BleakError as err:
            _LOGGER.warning("Error during disconnect from %s: %s", self.mac_address, err)
        finally:
            self._client = None

    async def write_message(self, payload: bytes) -> None:
        """Write a protobuf-encoded message to the device (write-with-response)."""
        _LOGGER.warning(
            "BLE WRITE to %s (%d bytes): %s", WRITE_CHAR_UUID, len(payload), "[redacted]"
        )
        try:
            await self._client.write_gatt_char(
                WRITE_CHAR_UUID, bytearray(payload), response=True
            )
        except BleakError as err:
            # Preserve the ATT status without exposing the payload or key.
            code = err.args[0] if err.args and isinstance(err.args[0], int) else None
            raise JouleBLEError(
                f"Failed to write message to Joule (ATT status={code!s})"
            ) from err

    async def write_message_no_response(self, payload: bytes) -> None:
        """Write a protobuf-encoded message to 4322 with response=False."""
        _LOGGER.warning(
            "BLE WRITE-NR to %s (%d bytes): %s", WRITE_CHAR_UUID, len(payload), "[redacted]"
        )
        try:
            await self._client.write_gatt_char(
                WRITE_CHAR_UUID, bytearray(payload), response=False
            )
        except BleakError as err:
            raise JouleBLEError("Failed to write (no-response) to Joule") from err

    async def write_to_file_char(self, payload: bytes) -> None:
        """Write a message to the FILE characteristic (4326) using write-without-response."""
        _LOGGER.warning(
            "BLE WRITE-WOR to %s (%d bytes): %s",
            FILE_CHAR_UUID, len(payload), "[redacted]",
        )
        try:
            await self._client.write_gatt_char(
                FILE_CHAR_UUID, bytearray(payload), response=False
            )
        except BleakError as err:
            raise JouleBLEError("Failed to write to FILE char on Joule") from err

    async def read_characteristic(self, char_uuid: str) -> bytes | None:
        """Read a GATT characteristic by UUID. Returns None on error."""
        try:
            data = await self._client.read_gatt_char(char_uuid)
            _LOGGER.warning(
                "BLE READ from %s: %d bytes, raw=%s",
                char_uuid,
                len(data),
                "[redacted]",
            )
            return bytes(data)
        except BleakError as err:
            _LOGGER.warning("BLE READ failed on %s: %s", char_uuid, err)
            return None

    async def subscribe(self, callback: Callable[[Any, bytearray], None]) -> None:
        """Subscribe to notifications on the subscribe characteristic.

        ``callback`` is called with ``(characteristic, data)`` for each notification.
        """
        try:
            _LOGGER.warning("Subscribing to primary %s", SUBSCRIBE_CHAR_UUID)
            await self._client.start_notify(SUBSCRIBE_CHAR_UUID, callback)
            _LOGGER.warning("Subscribe complete")
        except BleakError as err:
            raise JouleBLEError("Failed to subscribe to Joule notifications") from err
