"""Tests for the temperature sensor entity (sensor.py)."""
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.joule_sous_vide.const import DOMAIN
from custom_components.joule_sous_vide.joule_ble import JouleBLEError

from .conftest import TEST_ENTRY_ID

SENSOR_UNIQUE_ID = f"{TEST_ENTRY_ID}_current_temperature"


def _get_sensor_entity_id(hass: HomeAssistant) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, SENSOR_UNIQUE_ID)
    assert entity_id is not None, "Temperature sensor entity not found in registry"
    return entity_id


# ---------------------------------------------------------------------------
# Entity registration
# ---------------------------------------------------------------------------


async def test_sensor_is_registered(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The temperature sensor is registered in the entity registry."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id("sensor", DOMAIN, SENSOR_UNIQUE_ID)
    assert entry is not None


async def test_sensor_unique_id(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The sensor has the expected unique ID."""
    registry = er.async_get(hass)
    entry = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, SENSOR_UNIQUE_ID)
    )
    assert entry.unique_id == SENSOR_UNIQUE_ID


async def test_sensor_device_info(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The sensor is associated with the Joule device."""
    from homeassistant.helpers import device_registry as dr

    dev_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    entity_id = _get_sensor_entity_id(hass)
    entity_entry = entity_registry.async_get(entity_id)
    device = dev_registry.async_get(entity_entry.device_id)

    assert device is not None
    assert device.manufacturer == "ChefSteps"
    assert device.model == "Joule"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


async def test_sensor_state_is_unknown_before_telemetry(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """Do not invent a zero reading before the device sends telemetry."""
    from custom_components.joule_sous_vide.const import DOMAIN
    from custom_components.joule_sous_vide.coordinator import JouleCoordinator

    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = _get_sensor_entity_id(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_sensor_unit_of_measurement(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Sensor reports temperature in degrees Celsius."""
    entity_id = _get_sensor_entity_id(hass)
    state = hass.states.get(entity_id)

    assert state.attributes.get("unit_of_measurement") == UnitOfTemperature.CELSIUS


async def test_sensor_becomes_unavailable_on_ble_failure(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """When the coordinator cannot reach the device, the sensor shows Unavailable."""
    from custom_components.joule_sous_vide.coordinator import JouleCoordinator

    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    mock_ble_api.ensure_connected.side_effect = JouleBLEError("Connection lost")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = _get_sensor_entity_id(hass)
    state = hass.states.get(entity_id)

    assert state.state == STATE_UNAVAILABLE


async def test_sensor_recovers_after_ble_failure(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
) -> None:
    """After a BLE failure, the sensor recovers when the device becomes reachable again."""
    from custom_components.joule_sous_vide.coordinator import JouleCoordinator

    coordinator: JouleCoordinator = hass.data[DOMAIN][setup_integration.entry_id]

    # Cause a failure.
    mock_ble_api.ensure_connected.side_effect = JouleBLEError("Lost")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = _get_sensor_entity_id(hass)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    # Recover.
    mock_ble_api.ensure_connected.side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


@pytest.mark.parametrize("temperature", [0.0, 23.5])
async def test_sensor_reports_real_temperature_including_zero(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ble_api: MagicMock,
    temperature: float,
) -> None:
    """An actual zero reading is valid; only missing telemetry is unknown."""
    from custom_components.joule_sous_vide.joule_proto import (
        FIELD_CIRCULATOR_DATA_POINT, encode_field_bytes, encode_field_float,
    )

    coordinator = hass.data[DOMAIN][setup_integration.entry_id]
    entity_id = _get_sensor_entity_id(hass)
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    coordinator._try_decode_message(
        encode_field_bytes(
            FIELD_CIRCULATOR_DATA_POINT, encode_field_float(10, temperature),
        ),
        source="4323-read",
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id).state) == pytest.approx(temperature)
