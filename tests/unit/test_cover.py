"""Unit tests for the SkylinkCover entity.

Two flavours:
  * State mapping — every DoorState maps to the right HA cover
    properties (is_closed / is_opening / is_closing). This is what
    flows through to HomeKit's CurrentDoorState characteristic.
  * Command guards — open/close/stop only fire the toggle when it
    will move the door toward the requested state. Critical because
    the hardware is toggle-only, so an unguarded open() on an
    already-open door would close it.

Uses a MagicMock coordinator to avoid needing a real HA test harness.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.skylink._client.domain import (
    Device,
    DeviceSnapshot,
    DeviceType,
    DoorState,
)
from custom_components.skylink._client.errors import OrbitConnectionError
from custom_components.skylink.coordinator import SkylinkCoordinator
from custom_components.skylink.cover import SkylinkCover

_HUB = "HUB1"


@pytest.fixture
def coordinator() -> MagicMock:
    coord = MagicMock(spec=SkylinkCoordinator)
    coord.client = MagicMock()
    coord.client.toggle = AsyncMock()
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


def _make_cover(coord: MagicMock, hub: str = _HUB) -> SkylinkCover:
    return SkylinkCover(coord, hub)


# ---------------------------------------------------------------------------
# HA property mapping — what HomeKit's CurrentDoorState will ultimately see
# ---------------------------------------------------------------------------


class TestIsClosed:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (DoorState.CLOSED, True),
            (DoorState.OPEN, False),
            (DoorState.OPEN_HALF, False),
            (DoorState.OPEN_HALF_ALT, False),
            (DoorState.OPENING, False),
            (DoorState.CLOSING, False),
            (DoorState.CLOSE_DELAY, False),
        ],
    )
    def test_known_states(
        self, coordinator: MagicMock, state: DoorState, expected: bool
    ) -> None:
        _set_state(coordinator, state)
        assert _make_cover(coordinator).is_closed is expected

    def test_unknown_returns_none(self, coordinator: MagicMock) -> None:
        # HA treats is_closed=None as "unknown" — HomeKit bridge will
        # skip the CurrentDoorState update until a real value arrives.
        _set_state(coordinator, DoorState.UNKNOWN)
        assert _make_cover(coordinator).is_closed is None

    def test_missing_device_returns_none(self, coordinator: MagicMock) -> None:
        coordinator.data = {}  # no snapshot for our hub
        assert _make_cover(coordinator).is_closed is None


class TestIsOpening:
    def test_opening(self, coordinator: MagicMock) -> None:
        _set_state(coordinator, DoorState.OPENING)
        assert _make_cover(coordinator).is_opening is True

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.OPEN,
            DoorState.CLOSED,
            DoorState.CLOSING,
            DoorState.CLOSE_DELAY,
            DoorState.OPEN_HALF,
            DoorState.UNKNOWN,
        ],
    )
    def test_other_states_return_false(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        assert _make_cover(coordinator).is_opening is False


class TestIsClosing:
    @pytest.mark.parametrize(
        "state", [DoorState.CLOSING, DoorState.CLOSE_DELAY]
    )
    def test_closing_and_close_delay(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        # CLOSE_DELAY is the pre-motion warning period — we surface it
        # as "closing" so the HomeKit tile animates toward closed.
        _set_state(coordinator, state)
        assert _make_cover(coordinator).is_closing is True

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.OPEN,
            DoorState.CLOSED,
            DoorState.OPENING,
            DoorState.OPEN_HALF,
            DoorState.UNKNOWN,
        ],
    )
    def test_other_states_return_false(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        assert _make_cover(coordinator).is_closing is False


# ---------------------------------------------------------------------------
# Command guards — toggle-only hardware, HomeKit-safe semantics
# ---------------------------------------------------------------------------


class TestOpenCommand:
    async def test_fires_toggle_when_closed(self, coordinator: MagicMock) -> None:
        _set_state(coordinator, DoorState.CLOSED)
        await _make_cover(coordinator).async_open_cover()
        coordinator.client.toggle.assert_awaited_once_with(_HUB, DeviceType.GDO)

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.OPEN,
            DoorState.OPEN_HALF,
            DoorState.OPEN_HALF_ALT,
            DoorState.OPENING,
            DoorState.CLOSING,
            DoorState.CLOSE_DELAY,
            DoorState.UNKNOWN,
        ],
    )
    async def test_noop_from_non_closed_states(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        # Guarantees "Hey Siri, open the garage" on an already-open door
        # doesn't close it, and that a mid-motion "open" doesn't stop
        # the door by sending a rogue toggle.
        _set_state(coordinator, state)
        await _make_cover(coordinator).async_open_cover()
        coordinator.client.toggle.assert_not_awaited()


class TestCloseCommand:
    @pytest.mark.parametrize(
        "state", [DoorState.OPEN, DoorState.OPEN_HALF, DoorState.OPEN_HALF_ALT]
    )
    async def test_fires_toggle_from_open_states(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        await _make_cover(coordinator).async_close_cover()
        coordinator.client.toggle.assert_awaited_once_with(_HUB, DeviceType.GDO)

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.CLOSED,
            DoorState.OPENING,
            DoorState.CLOSING,
            DoorState.CLOSE_DELAY,
            DoorState.UNKNOWN,
        ],
    )
    async def test_noop_from_non_open_states(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        await _make_cover(coordinator).async_close_cover()
        coordinator.client.toggle.assert_not_awaited()


class TestStopCommand:
    @pytest.mark.parametrize(
        "state",
        [DoorState.OPENING, DoorState.CLOSING, DoorState.CLOSE_DELAY],
    )
    async def test_fires_toggle_mid_motion(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        _set_state(coordinator, state)
        await _make_cover(coordinator).async_stop_cover()
        coordinator.client.toggle.assert_awaited_once_with(_HUB, DeviceType.GDO)

    @pytest.mark.parametrize(
        "state",
        [
            DoorState.OPEN,
            DoorState.CLOSED,
            DoorState.OPEN_HALF,
            DoorState.OPEN_HALF_ALT,
            DoorState.UNKNOWN,
        ],
    )
    async def test_noop_when_idle(
        self, coordinator: MagicMock, state: DoorState
    ) -> None:
        # Toggling an idle door would *start* motion — opposite of "stop".
        _set_state(coordinator, state)
        await _make_cover(coordinator).async_stop_cover()
        coordinator.client.toggle.assert_not_awaited()


class TestErrorHandling:
    async def test_unknown_device_raises(self, coordinator: MagicMock) -> None:
        # No snapshot for this hub — toggle should refuse.
        coordinator.data = {}
        with pytest.raises(HomeAssistantError, match="not known"):
            await _make_cover(coordinator)._toggle()

    async def test_client_error_wrapped(self, coordinator: MagicMock) -> None:
        _set_state(coordinator, DoorState.CLOSED)
        coordinator.client.toggle.side_effect = OrbitConnectionError("broker down")
        with pytest.raises(HomeAssistantError, match="Failed to toggle"):
            await _make_cover(coordinator).async_open_cover()


# ---------------------------------------------------------------------------
# HomeKit-bridge eligibility smoke check
# ---------------------------------------------------------------------------


class TestHomeKitEligibility:
    def test_device_class_is_garage(self, coordinator: MagicMock) -> None:
        # HA's HomeKit bridge inspects device_class — "garage" is what
        # triggers the GarageDoorOpener accessory mapping.
        from homeassistant.components.cover import CoverDeviceClass

        _set_state(coordinator, DoorState.CLOSED)
        cover = _make_cover(coordinator)
        assert cover.device_class == CoverDeviceClass.GARAGE

    def test_supported_features_within_bridge_whitelist(
        self, coordinator: MagicMock
    ) -> None:
        # type_covers.GarageDoorOpener only accepts open/close/stop.
        # Adding SET_POSITION etc. would demote us to a generic cover.
        from homeassistant.components.cover import CoverEntityFeature

        _set_state(coordinator, DoorState.CLOSED)
        cover = _make_cover(coordinator)
        allowed = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )
        assert cover.supported_features & ~allowed == 0
