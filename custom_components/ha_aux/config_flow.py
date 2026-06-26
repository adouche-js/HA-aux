"""Config flow for HA aux integration.

Handles user setup, validation, and optional auto-discovery of
the HA aux add-on on the local machine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_URL,
    CONF_DEVICE_NAME,
    DEFAULT_API_URL,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
    DOMAIN_NAME,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_URL, default=DEFAULT_API_URL): str,
        vol.Required(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
    }
)


async def validate_api_connection(
    hass: HomeAssistant, url: str
) -> dict[str, Any] | None:
    """Try to reach the add-on health endpoint.

    Returns the JSON response body on success, or None on failure.
    """
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(5):
            async with session.get(f"{url}/health") as response:
                if response.status == 200:
                    return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("API connection test to %s failed: %s", url, err)
    return None


class HaAuxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA aux."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial (manual) setup step.

        Asks for the API URL and device name, then validates the connection.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            api_url = user_input.get(CONF_API_URL, DEFAULT_API_URL).rstrip("/")
            device_name = user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME)

            # Validate the connection
            health = await validate_api_connection(self.hass, api_url)
            if health is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"ha_aux_{api_url}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=device_name,
                    data={
                        CONF_API_URL: api_url,
                        CONF_DEVICE_NAME: device_name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=DEFAULT_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show a confirmation dialog when the add-on is auto-discovered."""
        if user_input is not None:
            return self.async_create_entry(
                title=self.context.get("title", DEFAULT_DEVICE_NAME),
                data=(
                    self.context.get("data")
                    or {
                        CONF_API_URL: DEFAULT_API_URL,
                        CONF_DEVICE_NAME: DEFAULT_DEVICE_NAME,
                    }
                ),
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={
                "device_name": self.context.get("title", DEFAULT_DEVICE_NAME),
            },
        )

    async def async_step_discovery(
        self, discovery_info: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle auto-discovery of the HA aux add-on.

        Checks whether the add-on is reachable at the default URL and,
        if so, guides the user through discovery confirmation.
        """
        # Abort if already configured
        await self.async_set_unique_id(f"ha_aux_{DEFAULT_API_URL}")
        self._abort_if_unique_id_configured()

        # Test the connection
        health = await validate_api_connection(self.hass, DEFAULT_API_URL)
        if health is None:
            _LOGGER.info("Auto-discovery: add-on not reachable at %s", DEFAULT_API_URL)
            return await self.async_step_user()

        _LOGGER.info("Auto-discovery: found HA aux add-on at %s", DEFAULT_API_URL)

        self.context["title"] = "HA aux"
        self.context["data"] = {
            CONF_API_URL: DEFAULT_API_URL,
            CONF_DEVICE_NAME: DEFAULT_DEVICE_NAME,
        }

        return await self.async_step_discovery_confirm()
