"""DataUpdateCoordinator for the Skylink integration.

State updates are push-based via MQTT. The coordinator owns a
hub_id → DoorView map:
  * initial population + periodic refresh comes from client.discover()
  * real-time state changes come from client.on_door_state(...) callbacks

The update_interval tick exists only as a belt-and-braces device-list
refresh — it's how a newly-added hub shows up without reloading the
config entry.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._client.client import OrbitClient
from ._client.domain import DeviceSnapshot, DoorState
from ._client.errors import OrbitAuthError, OrbitConnectionError, OrbitProtocolError
from .const import DEFAULT_DISCOVERY_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


type _CoordinatorData = dict[str, DeviceSnapshot]


class SkylinkCoordinator(DataUpdateCoordinator[_CoordinatorData]):
    """Maintains a hub_id → DeviceSnapshot map.

    Seeded (including initial states) by MQTT discovery; updated in
    place by push state callbacks as `/update/result` arrives.
    """

    def __init__(self, hass: HomeAssistant, client: OrbitClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_DISCOVERY_INTERVAL,
        )
        self.client = client
        client.on_door_state(self._on_state_update)

    # ------------------------------------------------------------------
    # Push path: client fires this when /update/result arrives
    # ------------------------------------------------------------------

    def _on_state_update(self, hub_id: str, state: DoorState) -> None:
        current: _CoordinatorData = self.data or {}
        snapshot = current.get(hub_id)
        if snapshot is None:
            _LOGGER.debug("ignoring state for unknown hub_id %s", hub_id)
            return
        if snapshot.state == state:
            return  # no-op

        updated = dict(current)
        updated[hub_id] = replace(snapshot, state=state)
        self.async_set_updated_data(updated)

    # ------------------------------------------------------------------
    # Pull path: first_refresh + periodic tick → re-run discovery
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> _CoordinatorData:
        try:
            snapshots = await self.client.discover()
        except OrbitAuthError as err:
            raise ConfigEntryAuthFailed("Skylink authentication expired") from err
        except OrbitConnectionError as err:
            raise UpdateFailed(f"Cannot reach Skylink cloud: {err}") from err
        except OrbitProtocolError as err:
            raise UpdateFailed(f"Protocol error during discovery: {err}") from err

        # The discovery response carries each device's current state, so
        # we don't need to preserve state across refreshes — the server
        # is authoritative.
        return {s.device.hub_id: s for s in snapshots}
