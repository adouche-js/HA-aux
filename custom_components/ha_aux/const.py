"""Constants for the HA aux integration."""

DOMAIN = "ha_aux"
DOMAIN_NAME = "HA aux Audio Renderer"

# Default URL of the add-on API (localhost because add-on uses host_network)
ADDON_API_URL = "http://127.0.0.1:8292"
DEFAULT_API_URL = "http://127.0.0.1:8292"

# Config entry keys
CONF_API_URL = "api_url"
CONF_DEVICE_NAME = "device_name"

# Default config values
DEFAULT_DEVICE_NAME = "HA aux"

# Polling interval in seconds
UPDATE_INTERVAL_SECONDS = 5

# Custom attributes exposed on the entity
ATTR_AUDIO_DEVICE = "audio_device"
ATTR_KEEP_ALIVE_ENABLED = "keep_alive_enabled"
ATTR_VERSION = "version"
