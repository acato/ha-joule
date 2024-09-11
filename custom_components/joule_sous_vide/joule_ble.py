import pygatt
import logging

_LOGGER = logging.getLogger(__name__)

JOULE_SERVICE_UUID = "YOUR_JOULE_SERVICE_UUID"
TEMPERATURE_CHAR_UUID = "YOUR_TEMPERATURE_CHAR_UUID"
TIME_CHAR_UUID = "YOUR_TIME_CHAR_UUID"
START_STOP_CHAR_UUID = "YOUR_START_STOP_CHAR_UUID"
CURRENT_TEMP_CHAR_UUID = "YOUR_CURRENT_TEMP_CHAR_UUID"

class JouleBLEAPI:
    def __init__(self, mac_address):
        self.mac_address = mac_address
        self.adapter = pygatt.GATTToolBackend()
        self.device = None

    def connect(self):
        try:
            self.adapter.start()
            self.device = self.adapter.connect(self.mac_address)
            _LOGGER.info(f"Connected to Joule at {self.mac_address}")
        except pygatt.exceptions.BLEError as e:
            _LOGGER.error(f"Failed to connect to Joule: {str(e)}")
    
    def disconnect(self):
        try:
            if self.device:
                self.device.disconnect()
            self.adapter.stop()
        except pygatt.exceptions.BLEError as e:
            _LOGGER.error(f"Failed to disconnect from Joule: {str(e)}")
    
    def set_temperature(self, temperature):
        """Set the target temperature."""
        temp_value = int(temperature * 100)  # Example conversion if needed
        self.device.char_write(TEMPERATURE_CHAR_UUID, temp_value.to_bytes(2, 'little'))
        _LOGGER.info(f"Set temperature to {temperature}°C")
    
    def set_cook_time(self, time_minutes):
        """Set the cooking time."""
        time_value = int(time_minutes * 60)  # Convert minutes to seconds
        self.device.char_write(TIME_CHAR_UUID, time_value.to_bytes(4, 'little'))
        _LOGGER.info(f"Set cook time to {time_minutes} minutes")
    
    def start_cooking(self):
        """Start the cooking process."""
        self.device.char_write(START_STOP_CHAR_UUID, bytearray([0x01]))
        _LOGGER.info("Cooking started")
    
    def stop_cooking(self):
        """Stop the cooking process."""
        self.device.char_write(START_STOP_CHAR_UUID, bytearray([0x00]))
        _LOGGER.info("Cooking stopped")
    
    def get_current_temperature(self):
        """Get the current temperature."""
        temp_bytes = self.device.char_read(CURRENT_TEMP_CHAR_UUID)
        current_temp = int.from_bytes(temp_bytes, 'little') / 100  # Convert from centidegrees
        _LOGGER.info(f"Current temperature is {current_temp}°C")
        return current_temp
