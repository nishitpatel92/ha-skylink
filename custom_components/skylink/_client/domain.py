"""Pure domain types.

Stdlib only. No IO. No HA. No transport details.

Values for DoorState come from the Orbit Home APK's `setDoorStatus` switch
statement (DeviceDetailPageAdapter.java:172-197 in v3.8.1). The APK is
the authoritative source of the wire-level int → state mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class DoorState(IntEnum):
    """Maps 1:1 to the `door` integer field in Skylink's MQTT payload."""

    OPENING = 0
    OPEN = 1
    OPEN_HALF = 2
    CLOSING = 3
    CLOSED = 4
    CLOSE_DELAY = 5
    OPEN_HALF_ALT = 6
    UNKNOWN = 7

    @classmethod
    def from_wire(cls, value: int) -> DoorState:
        """Convert a raw `door` int from MQTT to a DoorState.

        Values outside 0-7 fall through to UNKNOWN rather than raising.
        We'd rather keep the entity available reporting "unknown" than
        crash if Skylink ever ships a new firmware with a new code.
        """
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class DeviceType(StrEnum):
    """Skylink device model.

    Values match the `type` field in the MQTT device payload.
    """

    GDO = "GDO"
    NOVA_A = "NOVA_A"
    NOVA_B = "NOVA_B"
    NV_MINI = "NVMini"

    @property
    def position(self) -> str | None:
        """NOVA/NVMini devices need an A/B position selector; GDO doesn't.

        Returned by control-payload builders. None → omit the `position`
        key from the payload (matches GDO behaviour in the APK).
        """
        return {
            DeviceType.NOVA_A: "A",
            DeviceType.NOVA_B: "B",
            DeviceType.NV_MINI: "A",
        }.get(self)


@dataclass(frozen=True, slots=True)
class Device:
    """A single controllable Skylink device.

    Identity + metadata only. Current state is carried separately in
    `DeviceSnapshot` so the Device itself can stay immutable and
    comparable.
    """

    hub_id: str
    name: str
    device_type: DeviceType


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """A device along with its most-recently-observed state.

    Returned from `OrbitClient.discover()` — the server includes the
    current `reported.mdev.door` state in each entry of the device
    list, so callers don't need to wait for a fresh MQTT push to know
    what the door is doing.
    """

    device: Device
    state: DoorState
