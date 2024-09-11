import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "joule_sous_vide"

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Joule Sous Vide component."""
    _LOGGER.info("Setting up Joule Sous Vide component")
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Joule Sous Vide from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Example: Initialize connection with the Joule
    hass.data[DOMAIN][entry.entry_id] = await your_joule_library.connect_to_joule()
    return True
