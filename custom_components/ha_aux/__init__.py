"""HA aux integration for Home Assistant.

Exposes the HA aux audio renderer add-on as a media_player entity
within Home Assistant, compatible with Music Assistant, TTS, Assist,
and automations.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_API_URL,
    DEFAULT_API_URL,
    DOMAIN,
    DOMAIN_NAME,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA aux from a config entry.

    Creates the data coordinator and forwards setup to the media_player platform.
    """
    api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
    _LOGGER.info("Setting up HA aux integration (API: %s)", api_url)

    coordinator = HaAuxDataUpdateCoordinator(hass, api_url)

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("HA aux integration setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to a newer version if needed."""
    _LOGGER.debug("Migrating from version %s", entry.version)
    return True


# ---------------------------------------------------------------------------
# Data coordinator
# ---------------------------------------------------------------------------


class HaAuxDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls the HA aux add-on API for status updates.

    Used by the media_player entity to keep state in sync with MPV.
    """

    def __init__(self, hass: HomeAssistant, api_url: str) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            api_url: Base URL of the HA aux add-on API.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.api_url = api_url
        self._session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status from the add-on API.

        Returns the status dict on success.
        Raises UpdateFailed on any communication error.
        """
        try:
            async with asyncio.timeout(10):
                async with self._session.get(f"{self.api_url}/status") as response:
                    if response.status != 200:
                        text = await response.text()
                        raise UpdateFailed(
                            f"Add-on returned HTTP {response.status}: {text[:200]}"
                        )
                    return await response.json()

        except asyncio.TimeoutError as err:
            raise UpdateFailed(
                f"Timeout connecting to HA aux add-on at {self.api_url}"
            ) from err

        except aiohttp.ClientError as err:
            raise UpdateFailed(
                f"Cannot reach HA aux add-on at {self.api_url}: {err}"
            ) from err

    async def async_send_command(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a POST command to the add-on API.

        Args:
            endpoint: API endpoint path (e.g. "play", "pause").
            data: Optional JSON body.

        Returns:
            Response JSON dict.

        Raises:
            aiohttp.ClientError on connection failure.
        """
        url = f"{self.api_url}/{endpoint}"

        async with asyncio.timeout(10):
            if data is not None:
                async with self._session.post(url, json=data) as resp:
                    return await resp.json()
            else:
                async with self._session.post(url) as resp:
                    return await resp.json()
