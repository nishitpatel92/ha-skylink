"""Binary sensor for a Skylink garage door (open/closed).

Grouped under the same HA device as the cover entity, so the UI shows
one device with two entities. Convention: is_on = True means open.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._client.domain import DoorState
from .const import DOMAIN
from .coordinator import SkylinkCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SkylinkCoordinator = data.coordinator
    async_add_entities(
        SkylinkDoorSensor(coordinator, hub_id) for hub_id in coordinator.data
    )


class SkylinkDoorSensor(CoordinatorEntity[SkylinkCoordinator], BinarySensorEntity):
    """Open/closed binary sensor for a Skylink door."""

    _attr_device_class = BinarySensorDeviceClass.GARAGE_DOOR
    _attr_has_entity_name = True
    _attr_name = "Door"

    def __init__(self, coordinator: SkylinkCoordinator, hub_id: str) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        self._attr_unique_id = f"{DOMAIN}_{hub_id}_door_sensor"

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

    @property
    def available(self) -> bool:
        return super().available and self._hub_id in (self.coordinator.data or {})

    @property
    def is_on(self) -> bool | None:
        view = self.coordinator.data.get(self._hub_id)
        if view is None:
            return None
        if view.state == DoorState.UNKNOWN:
            return None
        # True = open (any non-closed state). Consistent with HA's
        # GARAGE_DOOR device class.
        return view.state != DoorState.CLOSED
