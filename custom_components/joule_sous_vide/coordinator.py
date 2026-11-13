"""Data update coordinator for the Joule Sous Vide integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BLE_AUTH_KEY,
    CONF_MAC_ADDRESS,
    DEFAULT_COOK_TIME_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TARGET_TEMPERATURE,
    DEFAULT_TEMPERATURE_UNIT,
    DOMAIN,
    READ_CHAR_UUID,
    SUBSCRIBE_CHAR_UUID,
)
from .joule_ble import JouleBLEAPI, JouleBLEError
from .joule_proto import (
    CirculatorDataPoint,
    ProgramStep,
    build_compact_start_cook_message,
    build_identify_circulator_message,
    build_live_feed_message,
    build_start_cook_message,
    build_start_key_exchange_message,
    build_stop_cook_message,
    build_submit_key_message,
    decode_stream_message,
    FIELD_START_PROGRAM_REPLY,
    FIELD_IDENTIFY_CIRCULATOR_REPLY,
)

_LOGGER = logging.getLogger(__name__)

PROXY_POLL_INTERVAL: float = 1.0  # seconds between 4323 polls (proxy mode)

# Result enum codes returned by the device
_RESULT_NAMES: dict[int, str] = {
    0: "CS_SUCCESS",
    3: "CS_ERROR_INTERNAL",
    5: "CS_ERROR_NOT_FOUND",
    7: "CS_ERROR_INVALID_PARAM",
    8: "CS_ERROR_INVALID_STATE",
    13: "CS_ERROR_TIMEOUT",
}


class JouleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single owner of the BLE connection; provides data to all entities.

    Entities must not create their own BLE connections. They read exclusively
    from coordinator.data and call coordinator methods for control actions.

    The Joule uses a notification-triggered-read pattern:
    - Notifications on 4325 are "data ready" signals (may contain no payload)
    - The actual protobuf data must be READ from 4323 after each notification
    """

    NOTIFICATION_TIMEOUT: float = 10.0  # seconds; overridden in tests
    KEY_EXCHANGE_TIMEOUT: float = 60.0  # seconds; user must press button

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self.api = JouleBLEAPI(hass, entry.data[CONF_MAC_ADDRESS])
        self._is_cooking: bool = False
        self._target_temperature: float = DEFAULT_TARGET_TEMPERATURE  # always °C
        self._cook_time_minutes: float = DEFAULT_COOK_TIME_MINUTES
        self._temperature_unit: str = entry.options.get(
            "temperature_unit", DEFAULT_TEMPERATURE_UNIT
        )
        self._latest_data_point: CirculatorDataPoint | None = None
        self._notification_received: asyncio.Event = asyncio.Event()
        self._key_exchange_received = asyncio.Event()
        self._submit_key_received = asyncio.Event()
        self._pending_auth_reply: asyncio.Event | None = None
        self._auth_poll_baseline: bytes | None = None
        self._subscribed: bool = False
        self._authenticated: bool = False
        self._start_program_reply_received: bool = False
        self._start_program_reply_result: int | None = None
        self._stop_circulator_reply_result: int | None = None
        self._proxy_poll_task: asyncio.Task | None = None
        self._last_polled_data: bytes | None = None  # dedup for proxy polling
        # Load persisted auth key from config entry options (if previously paired)
        stored_key = entry.options.get(CONF_BLE_AUTH_KEY)
        self._auth_key: bytes | None = (
            bytes.fromhex(stored_key) if stored_key else None
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    @staticmethod
    def _new_handle() -> int:
        """Generate a random handle for a single outgoing message.

        iOS PacketLogger capture (v0.16.0) revealed the official app uses a
        DIFFERENT random handle for each outgoing StreamMessage.  The handle
        is a correlation ID — the device echoes it back in the reply so the
        sender can match responses to requests.  Reusing a single handle for
        all messages (our v0.9.0–v0.15.0 approach) caused the Joule to
        silently ignore cook commands, likely because the firmware uses the
        handle to route messages internally.
        """
        return random.randint(1, 2**31 - 1)

    def _on_notification(self, characteristic: Any, data: bytearray) -> None:
        """Handle a BLE notification from bleak (runs on the event loop).

        The Joule uses notification-triggered-read: notifications on 4325 are
        "data ready" signals.  The actual protobuf data lives on 4323 (the read
        characteristic).  We schedule a read from 4323 every time a notification
        fires.  If the notification itself carries protobuf data we try to
        decode that too.
        """
        _LOGGER.warning(
            "NOTIFICATION on 4325: %d bytes, raw=%s",
            len(data),
            "[redacted]" if data else "(empty)",
        )
        # Schedule a read from 4323 (the real data source)
        self.hass.async_create_task(self._read_and_process())
        # Also try to decode inline notification data (if non-empty)
        if data and len(data) > 0:
            self._try_decode_message(bytes(data), source="notification")

    async def _read_and_process(self) -> None:
        """Read 4323 and process any data found (notification-triggered-read)."""
        try:
            read_data = await self.api.read_characteristic(READ_CHAR_UUID)
            if read_data and len(read_data) > 0:
                _LOGGER.warning(
                    "READ from 4323 (triggered by notification): %d bytes: %s",
                    len(read_data), "[redacted]",
                )
                self._try_decode_message(read_data, source="4323-read")
            else:
                _LOGGER.warning(
                    "READ from 4323 after notification: empty — signalling anyway",
                )
                self._notification_received.set()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to read 4323 after notification")
            self._notification_received.set()

    def _try_decode_message(self, data: bytes, source: str) -> None:
        """Decode a protobuf StreamMessage and update internal state."""
        if "poll" in source and data == self._auth_poll_baseline:
            return
        try:
            msg = decode_stream_message(data)
            if msg.start_key_exchange_reply is not None:
                if self._pending_auth_reply is not self._key_exchange_received:
                    return
                key = msg.start_key_exchange_reply.secret_key
                _LOGGER.warning(
                    "Got StartKeyExchangeReply from %s! key=%s result=%s",
                    source, "[redacted]", msg.start_key_exchange_reply.result,
                )
                if msg.start_key_exchange_reply.result == 0 and key:
                    self._auth_key = key
                    self._persist_auth_key(key)
                else:
                    self._auth_key = None
                self._key_exchange_received.set()
                self._notification_received.set()
            elif msg.submit_key_reply is not None:
                if self._pending_auth_reply is not self._submit_key_received:
                    return
                _LOGGER.warning(
                    "Got SubmitKeyReply from %s! result=%s",
                    source, msg.submit_key_reply.result,
                )
                self._authenticated = msg.submit_key_reply.result == 0
                self._submit_key_received.set()
                self._notification_received.set()
            elif msg.start_program_reply is not None:
                result = msg.start_program_reply.result
                result_name = _RESULT_NAMES.get(result, f"UNKNOWN({result})")
                _LOGGER.warning(
                    "Got StartProgramReply from %s! result=%d (%s)",
                    source, result, result_name,
                )
                self._start_program_reply_received = True
                self._start_program_reply_result = result
                self._notification_received.set()
            elif msg.stop_circulator_reply is not None:
                result = msg.stop_circulator_reply.result
                result_name = _RESULT_NAMES.get(result, f"UNKNOWN({result})")
                _LOGGER.warning(
                    "Got StopCirculatorReply from %s! result=%d (%s)",
                    source, result, result_name,
                )
                self._stop_circulator_reply_result = result
                self._notification_received.set()
            elif msg.identify_circulator_reply is not None:
                _LOGGER.warning(
                    "Got IdentifyCirculatorReply from %s! result=%d",
                    source, msg.identify_circulator_reply.result,
                )
                self._notification_received.set()
            elif msg.pong is not None:
                _LOGGER.warning("Got PONG from %s!", source)
                self._notification_received.set()
            elif msg.circulator_data_point is not None:
                dp = msg.circulator_data_point
                _LOGGER.warning(
                    "CirculatorDataPoint from %s: bath_temp=%.2f step=%s "
                    "feed=%d seq=%d ts=%d err=%s remain=%d",
                    source, dp.bath_temp, dp.program_step,
                    dp.feed_id, dp.sequence_number, dp.timestamp,
                    dp.error_state, dp.time_remaining,
                )
                self._latest_data_point = dp
                self._notification_received.set()
            else:
                _LOGGER.warning(
                    "Message from %s: handle=%d end=%s sender=%s — unrecognized",
                    source, msg.handle, msg.end,
                    msg.sender_address.hex() if msg.sender_address else "none",
                )
                self._notification_received.set()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to decode message from %s: %s", source, "[redacted]")

    def _persist_auth_key(self, key: bytes) -> None:
        """Save the BLE auth key to config entry options for future connections."""
        _LOGGER.debug("Persisting BLE auth key")
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_BLE_AUTH_KEY: key.hex()},
        )

    async def _safe_write(
        self, label: str, payload: bytes, *, response: bool = True,
    ) -> bool:
        """Write a payload to 4322 with error handling. Returns True on success."""
        self._notification_received.clear()
        try:
            async with asyncio.timeout(5):
                if response:
                    await self.api.write_message(payload)
                else:
                    await self.api.write_message_no_response(payload)
            _LOGGER.warning("Write OK: %s (resp=%s)", label, response)
            return True
        except (TimeoutError, JouleBLEError) as err:
            _LOGGER.warning("Write FAIL: %s: %s", label, err)
            return False

    async def _try_write_and_wait(
        self, label: str, payload: bytes, timeout: float,
        *, no_response: bool = False, reply_event: asyncio.Event | None = None,
    ) -> bool:
        """Write a payload to 4322 and wait for data.

        When no_response=False (default), uses write-with-response (ATT Write
        Request).  When no_response=True, uses write-without-response (ATT
        Write Command).  After the write, waits for a notification on 4325
        (triggers a read from 4323).  Polls 4323 every 5 seconds as keepalive.
        Returns True on response.
        """
        try:
            _LOGGER.warning(
                "%s (%d bytes): %s", label, len(payload), "[redacted]",
            )
            response_event = reply_event if reply_event is not None else self._notification_received
            response_event.clear()
            if reply_event is not None:
                # A characteristic can retain an older reply. Snapshot it before
                # writing; blind polls must not treat unchanged bytes as a reply.
                async with asyncio.timeout(10):
                    self._auth_poll_baseline = await self.api.read_characteristic(READ_CHAR_UUID)
                self._pending_auth_reply = reply_event

            try:
                async with asyncio.timeout(10):
                    if no_response:
                        await self.api.write_message_no_response(payload)
                    else:
                        await self.api.write_message(payload)
                _LOGGER.warning("Write succeeded for %s", label)
            except TimeoutError:
                _LOGGER.warning("Write timed out for %s", label)
                return False
            except JouleBLEError as err:
                _LOGGER.warning("Write failed for %s: %s", label, err)
                return False

            # Poll: wait for notification OR check 4323+4325 periodically.
            # Use faster polling when connected via proxy (notifications unreliable).
            elapsed = 0.0
            poll_interval = (
                PROXY_POLL_INTERVAL
                if self.api.is_connected_via_proxy
                else 5.0
            )
            while elapsed < timeout:
                wait_time = min(poll_interval, timeout - elapsed)
                try:
                    async with asyncio.timeout(wait_time):
                        await response_event.wait()
                    _LOGGER.warning("Got response after %s!", label)
                    return True
                except TimeoutError:
                    elapsed += wait_time

                # Read BOTH 4323 and 4325 — response could be on either
                for char_uuid, char_name in [
                    (READ_CHAR_UUID, "4323"),
                    (SUBSCRIBE_CHAR_UUID, "4325"),
                ]:
                    read_data = await self.api.read_characteristic(char_uuid)
                    if read_data and len(read_data) > 0:
                        _LOGGER.warning(
                            "Poll %s after %s: %d bytes: %s",
                            char_name, label, len(read_data), "[redacted]",
                        )
                        self._try_decode_message(
                            read_data, source=f"{char_name}-poll-{label}",
                        )
                        if response_event.is_set():
                            return True

                _LOGGER.warning(
                    "Poll after %s at %.0fs: both empty", label, elapsed,
                )

            _LOGGER.warning("No response to %s after %.0fs", label, timeout)
            return False
        finally:
            if reply_event is not None:
                self._pending_auth_reply = None
                self._auth_poll_baseline = None

    async def _read_and_log(self, char_uuid: str, label: str) -> bytes | None:
        """Read a characteristic and log the result. Returns raw bytes."""
        data = await self.api.read_characteristic(char_uuid)
        if data and len(data) > 0:
            _LOGGER.warning(
                "%s: %d bytes: %s", label, len(data), "[redacted]",
            )
        else:
            _LOGGER.warning("%s: empty", label)
        return data

    async def _start_proxy_poller(self) -> None:
        """Start the background 4323 poller for proxy connections."""
        if self._proxy_poll_task is not None:
            return
        self._proxy_poll_task = self.hass.async_create_background_task(
            self._proxy_poll_loop(), "joule_proxy_poll"
        )
        _LOGGER.warning("Started background 4323 poller (proxy mode)")

    async def _stop_proxy_poller(self) -> None:
        """Cancel the background 4323 poller."""
        if self._proxy_poll_task is not None:
            self._proxy_poll_task.cancel()
            try:
                await self._proxy_poll_task
            except asyncio.CancelledError:
                pass
            self._proxy_poll_task = None
            _LOGGER.warning("Stopped background 4323 poller")

    async def _proxy_poll_loop(self) -> None:
        """Poll 4323 every PROXY_POLL_INTERVAL seconds (proxy mode).

        ESPHome Bluetooth Proxies don't forward notifications on 4325,
        but the Joule still updates 4323 server-side.  This loop reads
        4323 directly as a substitute for the notification-triggered-read
        pattern.  Identical consecutive reads are deduplicated.
        """
        while True:
            await asyncio.sleep(PROXY_POLL_INTERVAL)
            try:
                data = await self.api.read_characteristic(READ_CHAR_UUID)
                if data and data != self._last_polled_data:
                    self._last_polled_data = data
                    _LOGGER.warning(
                        "Proxy poll: new data from 4323: %d bytes: %s",
                        len(data), "[redacted]",
                    )
                    self._try_decode_message(data, source="4323-proxy-poll")
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Proxy poll: read failed")

    async def async_shutdown(self) -> None:
        """Cancel the proxy poller and scheduled refreshes.

        Called automatically by HA via config_entry.async_on_unload.
        BLE disconnect is handled separately by async_unload_entry.
        """
        await self._stop_proxy_poller()
        await super().async_shutdown()

    async def _key_exchange(self) -> bool:
        """Send one compact request and wait for physical button confirmation.

        Keep one exchange pending until its reply arrives. Use empty addresses
        and one write with response, as in the Joule Remote client, then wait
        for the key-exchange reply rather than an unrelated notification.
        """
        from homeassistant.components.persistent_notification import (
            async_create,
            async_dismiss,
        )

        self._auth_key = None
        self._authenticated = False
        async_create(
            self.hass,
            "**Press the button on top of your Joule now** to complete pairing.\n\n"
            "Keep Joule nearby and disconnect other apps before pairing.",
            title="Joule Pairing Required",
            notification_id="joule_sous_vide_pairing",
        )
        try:
            payload = build_start_key_exchange_message(
                sender=b"", recipient=b"", handle=self._new_handle(),
            )
            got_reply = await self._try_write_and_wait(
                "StartKeyExchange", payload, self.KEY_EXCHANGE_TIMEOUT,
                reply_event=self._key_exchange_received,
            )
            if got_reply and self._auth_key:
                return await self._submit_key(self._auth_key)
            _LOGGER.warning("Key exchange failed or timed out")
            return False
        finally:
            async_dismiss(self.hass, "joule_sous_vide_pairing")

    async def _submit_key(self, key: bytes) -> bool:
        """Submit a secret key to authenticate with the Joule.

        Uses empty addresses, matching the working Joule Remote client.
        A rejection or timeout ends this attempt without speculative writes.

        Returns True if the device accepted the key.
        """
        _LOGGER.debug("Submitting stored BLE auth key")
        self._authenticated = False

        # Empty addresses; encoded length depends on the key length.
        payload_empty = build_submit_key_message(
            secret_key=key,
            sender=b"",
            recipient=b"",
            handle=self._new_handle(),
        )
        got_reply = await self._try_write_and_wait(
            "SubmitKey-empty-addrs", payload_empty, self.NOTIFICATION_TIMEOUT,
            reply_event=self._submit_key_received,
        )
        if got_reply and self._authenticated:
            _LOGGER.warning("Authentication successful (empty addrs)!")
            return True

        _LOGGER.warning("SubmitKeyRequest failed or was rejected")
        return False

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the Joule for current state.

        Authentication flow (first connection, no stored key):
          1. Subscribe to notifications on 4325
          2. Send StartKeyExchangeRequest — user presses Joule button
          3. Receive StartKeyExchangeReply with secret key
          4. Send SubmitKeyRequest with the key
          5. Receive SubmitKeyReply — now authorized

        Reconnection with stored key:
          1. Subscribe to notifications on 4325
          2. Send SubmitKeyRequest with stored key
          3. Receive SubmitKeyReply — now authorized

        After authentication:
          - Send BeginLiveFeed to request streaming data
          - Joule responds with CirculatorDataPoint via notifications
        """
        try:
            reconnected = await self.api.ensure_connected()
            if reconnected:
                _LOGGER.warning("Fresh BLE connection — resetting state")
                await self._stop_proxy_poller()
                self._last_polled_data = None
                self._subscribed = False
                self._authenticated = False
                self._start_program_reply_received = False
                self._start_program_reply_result = None
                self._stop_circulator_reply_result = None

            # Step 1: Subscribe to notifications on 4325
            if not self._subscribed:
                # Enable Service Changed indications first (required by Joule
                # firmware — the official iOS app does this before anything else)
                sc_ok = await self.api.enable_service_changed_indications()
                _LOGGER.warning(
                    "Service Changed indications: %s",
                    "enabled" if sc_ok else "not available",
                )

                # Brief delay after Service Changed to let firmware process
                # the indication registration before we send commands
                if sc_ok:
                    await asyncio.sleep(0.5)

                await self.api.subscribe(self._on_notification)
                self._subscribed = True
                _LOGGER.warning("Subscribed to 4325 notifications")

                # Verify CCCD was actually written
                cccd_ok = await self.api.verify_and_enable_notifications()
                _LOGGER.warning("CCCD verification: %s", "OK" if cccd_ok else "FAILED")

                # Log MTU after subscribe — start_notify may have triggered
                # MTU exchange via AcquireNotify
                _LOGGER.warning(
                    "MTU after subscribe: %d", self.api.mtu_size,
                )

                # Read 4323 to clear any stale buffer — the official app
                # always reads 4323 on notification; if the device waits for
                # us to consume stale data before accepting commands, this
                # unblocks it.
                stale = await self.api.read_characteristic(READ_CHAR_UUID)
                _LOGGER.warning(
                    "Initial 4323 read (clear buffer): %s",
                    "[redacted]" if stale else "empty",
                )

                # Start proxy poller if connected via proxy (notifications
                # on 4325 are unreliable through ESPHome BT proxies)
                if self.api.is_connected_via_proxy:
                    await self._start_proxy_poller()

            # Step 2: Authenticate if needed
            if not self._authenticated:
                if self._auth_key is not None:
                    _LOGGER.warning("Have stored auth key — submitting")
                    authenticated = await self._submit_key(self._auth_key)
                    if not authenticated:
                        _LOGGER.warning(
                            "Stored key rejected — starting fresh key exchange"
                        )
                        self._auth_key = None
                        authenticated = await self._key_exchange()
                else:
                    _LOGGER.warning(
                        "No auth key — starting key exchange "
                        "(press Joule button!)"
                    )
                    authenticated = await self._key_exchange()

                if not authenticated:
                    _LOGGER.warning(
                        "Authentication failed — will retry next poll"
                    )

            # Step 3: Request live data feed
            if self._authenticated:
                payload = build_live_feed_message(
                    sender=b"",
                    recipient=b"",
                    handle=self._new_handle(),
                )
                await self._try_write_and_wait(
                    "BeginLiveFeed", payload, self.NOTIFICATION_TIMEOUT,
                )

        except JouleBLEError as err:
            raise UpdateFailed(f"BLE communication failed: {err}") from err

        current_temperature: float | None = None
        if self._latest_data_point is not None:
            current_temperature = self._latest_data_point.bath_temp

            step = self._latest_data_point.program_step
            if step in (
                ProgramStep.PRE_HEAT,
                ProgramStep.WAIT_FOR_FOOD,
                ProgramStep.COOK,
            ):
                self._is_cooking = True
            elif step in (ProgramStep.UNKNOWN, ProgramStep.WAIT_FOR_REMOVE_FOOD):
                self._is_cooking = False

        return {
            "current_temperature": current_temperature,
            "is_cooking": self._is_cooking,
            "target_temperature": self._target_temperature,  # °C
            "cook_time_minutes": self._cook_time_minutes,
            "temperature_unit": self._temperature_unit,
        }

    async def async_start_cooking(
        self, target_temperature: float, cook_time_minutes: float
    ) -> None:
        """Send a protobuf StartProgramRequest to the device.

        Sends both compact (~30 byte) and full (68 byte) cook messages.
        After each, checks for a real StartProgramReply (field 51) or a
        program_step change in CirculatorDataPoint — NOT just "any notification"
        (the live feed streams data every ~1s regardless of commands sent).
        """
        self._target_temperature = target_temperature
        self._cook_time_minutes = cook_time_minutes
        try:
            await self.api.ensure_connected()
            cook_time_seconds = int(cook_time_minutes * 60)

            feed_id = 0
            seq_num = 0
            step_before = ProgramStep.UNKNOWN
            if self._latest_data_point is not None:
                feed_id = self._latest_data_point.feed_id
                seq_num = self._latest_data_point.sequence_number
                step_before = self._latest_data_point.program_step
            _LOGGER.warning(
                "StartProgramRequest: temp=%.1f°C cook_time=%ds "
                "feed_id=%d seq=%d step_before=%s (per-msg handles)",
                target_temperature, cook_time_seconds,
                feed_id, seq_num, step_before,
            )

            # Reset reply tracking before sending cook commands
            self._start_program_reply_received = False
            self._start_program_reply_result = None

            # Pre-cook: refresh the live feed session.
            # The Joule's firmware returns CS_ERROR_TIMEOUT (result=13) if the
            # live feed has expired (stops ~5s after a StopCirculator).  Sending
            # BeginLiveFeed right before StartProgram ensures the feed session
            # is active.  Wait briefly for fresh CirculatorDataPoint data so the
            # optimistic-concurrency feed_id/seq are current.
            livefeed_payload = build_live_feed_message(
                sender=b"",
                recipient=b"",
                handle=self._new_handle(),
            )
            got_data = await self._try_write_and_wait(
                "BeginLiveFeed-pre-cook", livefeed_payload, 3.0,
            )
            if got_data and self._latest_data_point is not None:
                # Use fresh feed_id/seq from the renewed live feed
                feed_id = self._latest_data_point.feed_id
                seq_num = self._latest_data_point.sequence_number
                _LOGGER.warning(
                    "Refreshed feed: feed_id=%d seq=%d",
                    feed_id, seq_num,
                )

            # Pre-cook: IdentifyCirculatorRequest (iOS does this before cook)
            identify_payload = build_identify_circulator_message(
                sender=b"",
                recipient=b"",
                handle=self._new_handle(),
            )
            await self._safe_write("IdentifyCirculator", identify_payload)
            await asyncio.sleep(0.5)

            # Attempt 1: compact message (~21–30 bytes)
            compact_payload = build_compact_start_cook_message(
                target_temperature,
                sender=b"",
                recipient=b"",
                handle=self._new_handle(),
                feed_id=feed_id,
                sequence_number=seq_num,
            )
            await self._try_write_and_wait(
                "StartProgram-compact", compact_payload, 5.0,
            )

            # Check for real response (StartProgramReply or step change)
            cook_accepted = False
            if self._start_program_reply_received:
                if self._start_program_reply_result == 0:
                    _LOGGER.warning("Compact cook: StartProgramReply result=0 — cook accepted!")
                    cook_accepted = True
                else:
                    result_name = _RESULT_NAMES.get(
                        self._start_program_reply_result or -1, "UNKNOWN",
                    )
                    _LOGGER.warning(
                        "Compact cook: StartProgramReply result=%s (%s) — "
                        "Joule REJECTED the command, trying full message",
                        self._start_program_reply_result, result_name,
                    )
                    # Reset for next attempt
                    self._start_program_reply_received = False
                    self._start_program_reply_result = None
            elif (
                self._latest_data_point is not None
                and self._latest_data_point.program_step != step_before
                and self._latest_data_point.program_step != ProgramStep.UNKNOWN
            ):
                _LOGGER.warning(
                    "Compact cook: program_step changed %s → %s! Cook accepted.",
                    step_before, self._latest_data_point.program_step,
                )
                cook_accepted = True
            else:
                _LOGGER.warning(
                    "Compact cook: no StartProgramReply, step still %s "
                    "— trying full message",
                    self._latest_data_point.program_step
                    if self._latest_data_point else "unknown",
                )

            # Attempt 2: full iOS-matching message (68 bytes) — skip if compact succeeded
            if not cook_accepted:
                full_payload = build_start_cook_message(
                    target_temperature,
                    cook_time_seconds,
                    sender=b"",
                    recipient=b"",
                    handle=self._new_handle(),
                    feed_id=feed_id,
                    sequence_number=seq_num,
                )
                await self._try_write_and_wait(
                    "StartProgram-full", full_payload, 5.0,
                )

                # Final check
                if self._start_program_reply_received:
                    if self._start_program_reply_result == 0:
                        _LOGGER.warning("Full cook: StartProgramReply result=0 — cook accepted!")
                        cook_accepted = True
                    else:
                        result_name = _RESULT_NAMES.get(
                            self._start_program_reply_result or -1, "UNKNOWN",
                        )
                        _LOGGER.warning(
                            "Full cook: StartProgramReply result=%s (%s) — "
                            "Joule REJECTED the command",
                            self._start_program_reply_result, result_name,
                        )
                elif (
                    self._latest_data_point is not None
                    and self._latest_data_point.program_step != step_before
                    and self._latest_data_point.program_step != ProgramStep.UNKNOWN
                ):
                    _LOGGER.warning(
                        "Full cook: program_step changed %s → %s! Cook accepted.",
                        step_before, self._latest_data_point.program_step,
                    )
                    cook_accepted = True
                else:
                    _LOGGER.warning(
                        "Both cook messages sent — NO successful reply, "
                        "program_step still %s. Joule rejected the commands.",
                        self._latest_data_point.program_step
                        if self._latest_data_point else "unknown",
                    )
        except JouleBLEError as err:
            raise HomeAssistantError(f"Failed to start cooking: {err}") from err

        if cook_accepted:
            self._is_cooking = True
        else:
            _LOGGER.warning(
                "Cook command NOT accepted — switch stays off. "
                "Check Joule logs for error details."
            )
        # Always publish state — settings (target_temp, cook_time) are user
        # intent and should be reflected in entities even if the cook was rejected.
        self._publish_state()

    def _publish_state(self) -> None:
        """Push current state to entities without triggering a full poll.

        Used after cook commands to reflect optimistic state immediately.
        A full _async_update_data() poll would read stale CirculatorDataPoint
        data (still step=UNKNOWN) and override the is_cooking flag.
        """
        current_temp = None
        if self._latest_data_point is not None:
            current_temp = self._latest_data_point.bath_temp
        self.async_set_updated_data({
            "current_temperature": current_temp,
            "is_cooking": self._is_cooking,
            "target_temperature": self._target_temperature,
            "cook_time_minutes": self._cook_time_minutes,
            "temperature_unit": self._temperature_unit,
        })

    async def async_set_target_temperature(self, value_celsius: float) -> None:
        """Update the target temperature (always in °C) without starting a cook."""
        self._target_temperature = value_celsius
        await self.async_refresh()

    async def async_set_temperature_unit(self, unit: str) -> None:
        """Update the display unit preference and persist it to the config entry."""
        self._temperature_unit = unit
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, "temperature_unit": unit},
        )
        await self.async_refresh()

    async def async_set_cook_time(self, value: float) -> None:
        """Update the cook time without starting a cook."""
        self._cook_time_minutes = value
        await self.async_refresh()

    async def async_stop_cooking(self) -> None:
        """Send a protobuf StopCirculatorRequest to the device.

        Sends an empty-body StopCirculatorRequest (no feedId/sequenceNumber).
        The iOS app sends these optional optimistic-concurrency fields, but at
        MTU=23 including them can push the message over the 20-byte ATT payload
        limit, forcing a Long Write.  Omitting them keeps the message at 12
        bytes (single Write Request) and avoids CS_ERROR_INVALID_STATE (result=8)
        from the firmware's concurrency check.
        """
        try:
            await self.api.ensure_connected()

            self._stop_circulator_reply_result = None
            _LOGGER.warning("StopCirculatorRequest: empty body (no concurrency fields)")

            payload = build_stop_cook_message(
                sender=b"",
                recipient=b"",
                handle=self._new_handle(),
                feed_id=0,
                sequence_number=0,
            )
            await self._try_write_and_wait(
                "StopCirculator", payload, 5.0,
            )
        except JouleBLEError as err:
            raise HomeAssistantError(f"Failed to stop cooking: {err}") from err

        if self._stop_circulator_reply_result == 0:
            _LOGGER.warning("Stop accepted (result=0) — switching off")
            self._is_cooking = False
            self._publish_state()
        elif self._stop_circulator_reply_result is not None:
            result_name = _RESULT_NAMES.get(
                self._stop_circulator_reply_result, "UNKNOWN",
            )
            _LOGGER.warning(
                "Stop REJECTED: result=%d (%s) — switch stays on",
                self._stop_circulator_reply_result, result_name,
            )
        else:
            _LOGGER.warning(
                "No StopCirculatorReply received — assuming stopped"
            )
            self._is_cooking = False
            self._publish_state()
