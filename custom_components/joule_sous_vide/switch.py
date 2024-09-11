from homeassistant.helpers.entity import ToggleEntity
import logging

from .joule_ble import JouleBLEAPI  # Import the BLE API class

_LOGGER = logging.getLogger(__name__)

DOMAIN = "joule_sous_vide"

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Joule sous vide switch."""
    mac_address = config.get("mac_address")  # Get the MAC address from config
    async_add_entities([JouleSousVideSwitch(mac_address)])

class JouleSousVideSwitch(ToggleEntity):
    """Representation of a Joule Sous Vide Switch."""

    def __init__(self, mac_address):
        """Initialize the switch."""
        self._is_on = False
        self._target_temperature = 60
        self._cook_time_minutes = 0
        self._joule_api = JouleBLEAPI(mac_address)
        self._joule_api.connect()

    @property
    def name(self):
        """Return the name of the switch."""
        return "Joule Sous Vide"

    @property
    def is_on(self):
        """Return true if the switch is on."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the device on."""
        _LOGGER.info("Starting Joule Sous Vide")
        self._joule_api.set_temperature(self._target_temperature)
        self._joule_api.set_cook_time(self._cook_time_minutes)
        self._joule_api.start_cooking()
        self._is_on = True
        await self.async_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the device off."""
        _LOGGER.info("Stopping Joule Sous Vide")
        self._joule_api.stop_cooking()
        self._is_on = False
        await self.async_update_ha_state()

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return {
            "target_temperature": self._target_temperature,
            "cook_time_minutes": self._cook_time_minutes,
        }

    async def async_update(self):
        """Fetch new state data for the switch."""
        # Update current temperature from Joule
        current_temperature = self._joule_api.get_current_temperature()
        _LOGGER.info(f"Current temperature: {current_temperature}")
        # Add logic to alert when cooking is done
