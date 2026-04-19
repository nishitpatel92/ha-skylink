"""Skylink integration for Home Assistant.

Thin HA adapter. All protocol work lives in `_client/`.

Lifecycle: authenticate → connect MQTT → discover devices → set up
cover + binary_sensor platforms. Unload reverses this cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from ._client.client import OrbitClient
from ._client.errors import OrbitAuthError, OrbitConnectionError
from .const import DOMAIN, PLATFORMS
from .coordinator import SkylinkCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class SkylinkData:
    """Runtime state stored in hass.data per config entry."""

    client: OrbitClient
    coordinator: SkylinkCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Skylink from a config entry."""
    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]

    client = OrbitClient()

    try:
        await client.authenticate(username, password)
    except OrbitAuthError as err:
        await client.disconnect()
        raise ConfigEntryAuthFailed("Invalid Skylink credentials") from err
    except OrbitConnectionError as err:
        await client.disconnect()
        raise ConfigEntryNotReady(f"Cannot reach Skylink cloud: {err}") from err

    try:
        await client.connect()
    except OrbitAuthError as err:
        await client.disconnect()
        raise ConfigEntryAuthFailed("MQTT broker rejected credentials") from err
    except OrbitConnectionError as err:
        await client.disconnect()
        raise ConfigEntryNotReady(f"Cannot reach MQTT broker: {err}") from err

    coordinator = SkylinkCoordinator(hass, client)
    # First refresh populates coordinator.data (the device list) before
    # platforms create their entities.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = SkylinkData(
        client=client, coordinator=coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and tear down the client."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data: SkylinkData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.client.disconnect()
    return unload_ok
