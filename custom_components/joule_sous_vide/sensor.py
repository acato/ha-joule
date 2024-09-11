from homeassistant.helpers.entity import Entity
import logging

from .joule_ble import JouleBLEAPI  # Import the BLE API class

_LOGGER = logging.getLogger(__name__)

DOMAIN = "joule_sous_vide"

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Joule temperature sensor."""
    mac_address = config.get("mac_address")  # Get the MAC address from config
    async_add_entities([JouleTemperatureSensor(mac_address)])

class JouleTemperatureSensor(Entity):
    """Representation of a Joule Temperature Sensor."""

    def __init__(self, mac_address):
        """Initialize the sensor."""
        self._joule_api = JouleBLEAPI(mac_address)
        self._joule_api.connect()
        self._temperature = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Joule Current Temperature"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._temperature

    async def async_update(self):
        """Fetch new state data for the sensor."""
        self._temperature = self._joule_api.get_current_temperature()
