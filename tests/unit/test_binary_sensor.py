"""Unit tests for SkylinkDoorSensor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.skylink._client.domain import (
    Device,
    DeviceSnapshot,
    DeviceType,
    DoorState,
)
from custom_components.skylink.binary_sensor import SkylinkDoorSensor
from custom_components.skylink.coordinator import SkylinkCoordinator

_HUB = "HUB1"


@pytest.fixture
def coordinator() -> MagicMock:
    coord = MagicMock(spec=SkylinkCoordinator)
    coord.data = {}
    coord.last_update_success = True
    return coord


def _set_state(coord: MagicMock, state: DoorState, hub: str = _HUB) -> None:
    coord.data = {
        hub: DeviceSnapshot(
            device=Device(hub_id=hub, name="Test", device_type=DeviceType.GDO),
            state=state,
        )
    }


def _make_sensor(coord: MagicMock, hub: str = _HUB) -> SkylinkDoorSensor:
    return SkylinkDoorSensor(coord, hub)


class TestIsOn:
    def test_closed_is_off(self, coordinator: MagicMock) -> None:
        _set_state(coordinator, DoorState.CLOSED)
        assert _make_sensor(coordinator).is_on is False

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.OPEN,
            DoorState.OPEN_HALF,
            DoorState.OPEN_HALF_ALT,
            DoorState.OPENING,
            DoorState.CLOSING,
            DoorState.CLOSE_DELAY,
        ],
    )
    def test_any_non_closed_known_state_is_on(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        assert _make_sensor(coordinator).is_on is True

    def test_unknown_returns_none(self, coordinator: MagicMock) -> None:
        _set_state(coordinator, DoorState.UNKNOWN)
        assert _make_sensor(coordinator).is_on is None

    def test_missing_device_returns_none(self, coordinator: MagicMock) -> None:
        coordinator.data = {}
        assert _make_sensor(coordinator).is_on is None


class TestHomeKitExposureGuard:
    """The sensor must not appear in Apple Home alongside the cover.

    BinarySensorDeviceClass.GARAGE_DOOR is mapped to a ContactSensor
    in HA's HomeKit bridge (type_sensors.py:90), which would produce
    a duplicate accessory. The bridge's filter skips entities whose
    registry-enabled-default is False — that's the mechanism we rely
    on to keep the sensor out of HomeKit while still offering it as
    an opt-in for HA automations.
    """

    def test_default_disabled(self, coordinator: MagicMock) -> None:
        # HA converts `_attr_*` into cached_property descriptors on the
        # class, so we read through an instance. The property HA actually
        # consults at registration time is `entity_registry_enabled_default`.
        _set_state(coordinator, DoorState.CLOSED)
        sensor = _make_sensor(coordinator)
        assert sensor.entity_registry_enabled_default is False

    def test_device_class_is_garage_door(self, coordinator: MagicMock) -> None:
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        _set_state(coordinator, DoorState.CLOSED)
        sensor = _make_sensor(coordinator)
        assert sensor.device_class == BinarySensorDeviceClass.GARAGE_DOOR
