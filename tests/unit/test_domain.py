"""Tests for domain types."""

from __future__ import annotations

import pytest

from custom_components.skylink._client.domain import Device, DeviceType, DoorState


class TestDoorState:
    @pytest.mark.parametrize(
        ("wire_value", "expected"),
        [
            (0, DoorState.OPENING),
            (1, DoorState.OPEN),
            (2, DoorState.OPEN_HALF),
            (3, DoorState.CLOSING),
            (4, DoorState.CLOSED),
            (5, DoorState.CLOSE_DELAY),
            (6, DoorState.OPEN_HALF_ALT),
            (7, DoorState.UNKNOWN),
        ],
    )
    def test_from_wire_maps_known_values(self, wire_value: int, expected: DoorState) -> None:
        assert DoorState.from_wire(wire_value) is expected

    @pytest.mark.parametrize("wire_value", [-1, 8, 99, 1000])
    def test_from_wire_falls_back_to_unknown(self, wire_value: int) -> None:
        assert DoorState.from_wire(wire_value) is DoorState.UNKNOWN


class TestDeviceType:
    def test_gdo_has_no_position(self) -> None:
        assert DeviceType.GDO.position is None

    def test_nova_positions(self) -> None:
        assert DeviceType.NOVA_A.position == "A"
        assert DeviceType.NOVA_B.position == "B"

    def test_nvmini_uses_position_a(self) -> None:
        assert DeviceType.NV_MINI.position == "A"


class TestDevice:
    def test_device_is_frozen(self) -> None:
        dev = Device(hub_id="ABC", name="Garage", device_type=DeviceType.GDO)
        with pytest.raises(Exception):  # FrozenInstanceError is a dataclass exception
            dev.name = "Other"  # type: ignore[misc]

    def test_device_is_hashable(self) -> None:
        dev = Device(hub_id="ABC", name="Garage", device_type=DeviceType.GDO)
        assert hash(dev) == hash(dev)
