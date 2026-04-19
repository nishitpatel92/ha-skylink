"""Cover entity for a Skylink garage door.

Maps the `DoorState` enum to HA's is_closed / is_opening / is_closing
booleans. All three HA commands (open, close, stop) dispatch the same
toggle — the hardware has no separate open/close/stop primitives.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._client.domain import DoorState
from ._client.errors import OrbitError
from .const import DOMAIN
from .coordinator import SkylinkCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one SkylinkCover per discovered hub."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SkylinkCoordinator = data.coordinator
    async_add_entities(
        SkylinkCover(coordinator, hub_id) for hub_id in coordinator.data
    )


class SkylinkCover(CoordinatorEntity[SkylinkCoordinator], CoverEntity):
    """A Skylink garage door exposed as an HA cover.

    The hardware only supports a toggle command, so HA's open/close/stop
    semantics all collapse to the same call. HA's state model is
    derived from the MQTT push updates via DoorState mapping.
    """

    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    _attr_has_entity_name = True
    _attr_name = None  # inherit the device name

    def __init__(self, coordinator: SkylinkCoordinator, hub_id: str) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        self._attr_unique_id = f"{DOMAIN}_{hub_id}"

    # ------------------------------------------------------------------
    # Device info (groups the cover + binary_sensor under one HA device)
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        view = self.coordinator.data.get(self._hub_id)
        device_type = view.device.device_type.value if view else "Unknown"
        name = view.device.name if view else f"Skylink {self._hub_id}"
        return DeviceInfo(
            identifiers={(DOMAIN, self._hub_id)},
            manufacturer="Skylink",
            model=f"Skylink {device_type}",
            name=name,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def _state(self) -> DoorState:
        view = self.coordinator.data.get(self._hub_id)
        return view.state if view is not None else DoorState.UNKNOWN

    @property
    def available(self) -> bool:
        return super().available and self._hub_id in (self.coordinator.data or {})

    @property
    def is_closed(self) -> bool | None:
        state = self._state
        if state == DoorState.UNKNOWN:
            return None
        return state == DoorState.CLOSED

    @property
    def is_opening(self) -> bool:
        return self._state == DoorState.OPENING

    @property
    def is_closing(self) -> bool:
        # CLOSE_DELAY means "about to close" — report as closing so the UI
        # shows motion rather than an open-but-idle state.
        return self._state in (DoorState.CLOSING, DoorState.CLOSE_DELAY)

    # ------------------------------------------------------------------
    # Commands (all map to toggle — hardware limitation)
    # ------------------------------------------------------------------

    async def _toggle(self) -> None:
        view = self.coordinator.data.get(self._hub_id)
        if view is None:
            raise HomeAssistantError(f"Skylink device {self._hub_id} is not known")
        try:
            await self.coordinator.client.toggle(
                self._hub_id, view.device.device_type
            )
        except OrbitError as err:
            raise HomeAssistantError(f"Failed to toggle door: {err}") from err

    async def async_open_cover(self, **_: Any) -> None:
        await self._toggle()

    async def async_close_cover(self, **_: Any) -> None:
        await self._toggle()

    async def async_stop_cover(self, **_: Any) -> None:
        await self._toggle()
