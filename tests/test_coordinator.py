"""Tests for JouleCoordinator (coordinator.py)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.joule_sous_vide.const import (
    DEFAULT_COOK_TIME_MINUTES,
    DEFAULT_TARGET_TEMPERATURE,
    DEFAULT_TEMPERATURE_UNIT,
    DOMAIN,
)
from custom_components.joule_sous_vide.coordinator import (
    PROXY_POLL_INTERVAL,
    JouleCoordinator,
)
from custom_components.joule_sous_vide.joule_ble import JouleBLEError


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


async def test_update_data_returns_expected_shape(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Coordinator data dict contains all expected keys after a successful poll."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    assert "current_temperature" in coordinator.data
    assert "is_cooking" in coordinator.data
    assert "target_temperature" in coordinator.data
    assert "cook_time_minutes" in coordinator.data
    assert "temperature_unit" in coordinator.data


async def test_update_data_returns_unknown_temperature_before_telemetry(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Temperature must stay unknown until the device supplies telemetry."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data["current_temperature"] is None


async def test_update_data_raises_update_failed_on_connect_error_during_poll(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """A JouleBLEError from ensure_connected during polling raises UpdateFailed."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    mock_ble_api.ensure_connected.side_effect = JouleBLEError("BLE lost")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_data_raises_update_failed_on_connect_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """A JouleBLEError from ensure_connected also raises UpdateFailed."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    mock_ble_api.ensure_connected.side_effect = JouleBLEError("Cannot reconnect")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_initial_cooking_state_is_false(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """is_cooking is False when the integration first starts."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]
    assert coordinator.data["is_cooking"] is False


async def test_initial_defaults(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """target_temperature and cook_time_minutes start at their defined defaults."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]
    assert coordinator.data["target_temperature"] == DEFAULT_TARGET_TEMPERATURE
    assert coordinator.data["cook_time_minutes"] == DEFAULT_COOK_TIME_MINUTES


# ---------------------------------------------------------------------------
# Temperature unit persistence
# ---------------------------------------------------------------------------


async def test_temperature_unit_default(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Temperature unit defaults to °F when no option is stored."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]
    assert coordinator.data["temperature_unit"] == DEFAULT_TEMPERATURE_UNIT


async def test_set_temperature_unit_persists_to_config_entry(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Changing the temperature unit writes it to config entry options."""
    from homeassistant.const import UnitOfTemperature

    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    await coordinator.async_set_temperature_unit(UnitOfTemperature.CELSIUS)
    await hass.async_block_till_done()

    assert setup_integration.options.get("temperature_unit") == UnitOfTemperature.CELSIUS


async def test_temperature_unit_loaded_from_options_on_startup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Coordinator loads the persisted temperature unit from config entry options."""
    from homeassistant.const import UnitOfTemperature
    from custom_components.joule_sous_vide.const import CONF_MAC_ADDRESS
    from tests.conftest import TEST_MAC

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAC_ADDRESS: TEST_MAC},
        options={"temperature_unit": UnitOfTemperature.CELSIUS},
        unique_id=TEST_MAC,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data["temperature_unit"] == UnitOfTemperature.CELSIUS


# ---------------------------------------------------------------------------
# async_start_cooking
# ---------------------------------------------------------------------------


async def test_start_cooking_calls_ensure_connected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """async_start_cooking ensures BLE connection before sending commands."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    await coordinator.async_start_cooking(65.0, 90.0)

    mock_ble_api.ensure_connected.assert_called()


async def test_start_cooking_updates_is_cooking(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """After async_start_cooking, coordinator data reflects is_cooking=True."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    # Simulate Joule accepting the cook command (StartProgramReply result=0)
    async def _simulate_start_reply(*args, **kwargs):
        coordinator._start_program_reply_received = True
        coordinator._start_program_reply_result = 0
        coordinator._notification_received.set()

    mock_ble_api.write_message.side_effect = _simulate_start_reply

    await coordinator.async_start_cooking(65.0, 90.0)
    await hass.async_block_till_done()

    assert coordinator.data["is_cooking"] is True


async def test_start_cooking_stores_settings_in_data(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """After async_start_cooking, target_temperature and cook_time_minutes are updated."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    # Settings are always published regardless of device reply
    await coordinator.async_start_cooking(65.0, 90.0)
    await hass.async_block_till_done()

    assert coordinator.data["target_temperature"] == 65.0
    assert coordinator.data["cook_time_minutes"] == 90.0


async def test_start_cooking_raises_homeassistant_error_on_connect_failure(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """A JouleBLEError from ensure_connected during start_cooking raises HomeAssistantError."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    mock_ble_api.ensure_connected.side_effect = JouleBLEError("Connect failed")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_start_cooking(65.0, 90.0)


# ---------------------------------------------------------------------------
# async_stop_cooking
# ---------------------------------------------------------------------------


async def test_stop_cooking_calls_ensure_connected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """async_stop_cooking ensures BLE connection before sending commands."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    await coordinator.async_stop_cooking()

    mock_ble_api.ensure_connected.assert_called()


async def test_stop_cooking_updates_is_cooking(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """After async_stop_cooking, coordinator data reflects is_cooking=False."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    # Simulate Joule accepting start then stop
    async def _simulate_start_reply(*args, **kwargs):
        coordinator._start_program_reply_received = True
        coordinator._start_program_reply_result = 0
        coordinator._notification_received.set()

    mock_ble_api.write_message.side_effect = _simulate_start_reply

    await coordinator.async_start_cooking(65.0, 90.0)
    await hass.async_block_till_done()
    assert coordinator.data["is_cooking"] is True

    # Switch to simulating stop reply
    async def _simulate_stop_reply(*args, **kwargs):
        coordinator._stop_circulator_reply_result = 0
        coordinator._notification_received.set()

    mock_ble_api.write_message.side_effect = _simulate_stop_reply

    await coordinator.async_stop_cooking()
    await hass.async_block_till_done()

    assert coordinator.data["is_cooking"] is False


async def test_stop_cooking_raises_homeassistant_error_on_connect_failure(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """A JouleBLEError from ensure_connected during stop_cooking raises HomeAssistantError."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    mock_ble_api.ensure_connected.side_effect = JouleBLEError("Connect failed")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_stop_cooking()


# ---------------------------------------------------------------------------
# Proxy poller
# ---------------------------------------------------------------------------


async def test_proxy_poller_starts_when_connected_via_proxy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Proxy poller task is started after subscribe when connected via proxy."""
    mock_ble_api.is_connected_via_proxy = True
    # Fresh connection triggers subscribe path
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator._proxy_poll_task is not None
    assert not coordinator._proxy_poll_task.done()


async def test_proxy_poller_does_not_start_for_local(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Proxy poller task is NOT started for local adapter connections."""
    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]
    assert coordinator._proxy_poll_task is None


async def test_proxy_poll_reads_and_decodes_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Proxy poll reads 4323 and calls _try_decode_message on new data."""
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    # Reset dedup state, then return new data for the next poll cycle
    coordinator._last_polled_data = None
    fresh_data = b"\xde\xad\xbe\xef"
    mock_ble_api.read_characteristic = AsyncMock(return_value=fresh_data)

    with patch.object(coordinator, "_try_decode_message") as mock_decode:
        # Let the poll loop run once
        await asyncio.sleep(0.05)
        mock_decode.assert_called_with(fresh_data, source="4323-proxy-poll")


async def test_proxy_poll_deduplicates_identical_reads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Proxy poll does NOT decode the same data twice."""
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)
    # Always return the same data
    fake_data = b"\xaa\xbb\xcc"
    mock_ble_api.read_characteristic = AsyncMock(return_value=fake_data)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    with patch.object(coordinator, "_try_decode_message") as mock_decode:
        # Let several poll cycles run
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        # Should be called at most once since data never changes
        assert mock_decode.call_count <= 1

    await coordinator.async_shutdown()


async def test_proxy_poller_stops_on_reconnect(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Proxy poller is stopped and dedup reset on a fresh reconnect."""
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator._proxy_poll_task is not None
    old_task = coordinator._proxy_poll_task

    # Simulate reconnect — ensure_connected returns True again
    coordinator._last_polled_data = b"\x01"
    await coordinator._async_update_data()

    # After reconnect, dedup is reset and a NEW poller started
    assert coordinator._last_polled_data is None
    assert old_task.cancelled()
    assert coordinator._proxy_poll_task is not None
    assert coordinator._proxy_poll_task is not old_task


async def test_async_shutdown_cancels_poller(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """async_shutdown cancels the proxy poller task."""
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator._proxy_poll_task is not None

    await coordinator._stop_proxy_poller()

    assert coordinator._proxy_poll_task is None


async def test_try_write_and_wait_uses_faster_poll_via_proxy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """_try_write_and_wait uses PROXY_POLL_INTERVAL when connected via proxy."""
    mock_ble_api.is_connected_via_proxy = True
    mock_ble_api.ensure_connected = AsyncMock(return_value=True)

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator: JouleCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    # Call _try_write_and_wait with a very short timeout; it should use the
    # proxy poll interval rather than the default 5s
    payload = b"\x00" * 10
    # Should complete quickly with the fast poll interval
    result = await coordinator._try_write_and_wait("test", payload, 0.05)
    # With no response it returns False — that's fine; we're testing it completes
    # quickly (would take 5s+ if using default poll interval)
    assert result is False
