"""Unit tests for OrbitMqtt's message-dispatch logic.

These tests do NOT open a broker connection. They exercise the pure
dispatch path (`_handle_incoming`) directly, which is the only
non-IO-bound logic in the transport. Full connect/subscribe/publish
is covered by integration tests against a testcontainers mosquitto.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.skylink._client import protocol
from custom_components.skylink._client.domain import Device, DeviceType, DoorState
from custom_components.skylink._client.errors import OrbitProtocolError
from custom_components.skylink._client.mqtt import OrbitMqtt

_ACC_NO = "8003105701"


def _update_topic() -> str:
    return protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_UPDATE_RESULT)


def _get_result_topic() -> str:
    return protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_GET_RESULT)


@pytest.fixture
def mqtt() -> OrbitMqtt:
    """An OrbitMqtt wired for our fake acc_no.

    We bypass connect() since these tests exercise _handle_incoming directly.
    """
    m = OrbitMqtt()
    m._acc_no = _ACC_NO
    return m


class TestStateUpdateDispatch:
    def test_invokes_state_callback(self, mqtt: OrbitMqtt) -> None:
        received: list[tuple[str, DoorState]] = []
        mqtt.on_door_state(lambda hub, state: received.append((hub, state)))

        payload = {"data": {"hub_id": "rA8qM4QS", "reported": {"mdev": {"door": 4}}}}
        mqtt._handle_incoming(_update_topic(), json.dumps(payload).encode())

        assert received == [("rA8qM4QS", DoorState.CLOSED)]

    def test_no_callback_does_not_crash(self, mqtt: OrbitMqtt) -> None:
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 1}}}}
        mqtt._handle_incoming(_update_topic(), json.dumps(payload).encode())

    def test_silently_ignores_non_door_update(self, mqtt: OrbitMqtt) -> None:
        received: list[tuple[str, DoorState]] = []
        mqtt.on_door_state(lambda hub, state: received.append((hub, state)))

        # Firmware-only update — no "door" key. Should not fire callback.
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"rssi": -40}}}}
        mqtt._handle_incoming(_update_topic(), json.dumps(payload).encode())
        assert received == []

    def test_ignores_non_json_payload(self, mqtt: OrbitMqtt) -> None:
        received: list[tuple[str, DoorState]] = []
        mqtt.on_door_state(lambda hub, state: received.append((hub, state)))

        mqtt._handle_incoming(_update_topic(), b"not json at all")
        assert received == []

    def test_callback_exception_is_logged_and_swallowed(
        self, mqtt: OrbitMqtt, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken_cb(hub: str, state: DoorState) -> None:
            raise RuntimeError("intentional")

        mqtt.on_door_state(broken_cb)
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 4}}}}
        # Must not propagate — the message loop would otherwise die.
        mqtt._handle_incoming(_update_topic(), json.dumps(payload).encode())
        assert "State callback raised" in caplog.text

    def test_messages_for_wrong_acc_no_are_ignored(self, mqtt: OrbitMqtt) -> None:
        received: list[tuple[str, DoorState]] = []
        mqtt.on_door_state(lambda hub, state: received.append((hub, state)))

        wrong_topic = "skylink/things/client/9999999999/update/result"
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 4}}}}
        mqtt._handle_incoming(wrong_topic, json.dumps(payload).encode())
        assert received == []


class TestDiscoverDispatch:
    async def test_resolves_future_on_valid_response(self, mqtt: OrbitMqtt) -> None:
        loop = asyncio.get_running_loop()
        mqtt._discover_future = loop.create_future()

        payload = {
            "data": [
                {"hub_id": "aaa", "type": "GDO", "name": "Main"},
                {"hub_id": "bbb", "type": "NOVA_A"},
            ]
        }
        mqtt._handle_incoming(_get_result_topic(), json.dumps(payload).encode())

        assert mqtt._discover_future.done()
        devices = mqtt._discover_future.result()
        assert devices == [
            Device(hub_id="aaa", name="Main", device_type=DeviceType.GDO),
            Device(hub_id="bbb", name="Skylink bbb", device_type=DeviceType.NOVA_A),
        ]

    async def test_sets_future_exception_on_malformed_payload(self, mqtt: OrbitMqtt) -> None:
        loop = asyncio.get_running_loop()
        mqtt._discover_future = loop.create_future()

        # Missing "data" list — parse_discover_response raises.
        payload = {"not_data": "nope"}
        mqtt._handle_incoming(_get_result_topic(), json.dumps(payload).encode())

        assert mqtt._discover_future.done()
        with pytest.raises(OrbitProtocolError, match="missing 'data' list"):
            mqtt._discover_future.result()

    async def test_no_future_in_flight_is_noop(self, mqtt: OrbitMqtt) -> None:
        # get/result arriving unsolicited (shouldn't happen but should not crash)
        payload = {"data": [{"hub_id": "a", "type": "GDO"}]}
        mqtt._handle_incoming(_get_result_topic(), json.dumps(payload).encode())
        assert mqtt._discover_future is None

    async def test_done_future_does_not_double_resolve(self, mqtt: OrbitMqtt) -> None:
        # A stale future from a prior discover that already resolved.
        loop = asyncio.get_running_loop()
        mqtt._discover_future = loop.create_future()
        mqtt._discover_future.set_result([])

        payload = {"data": [{"hub_id": "x", "type": "GDO"}]}
        # Must not raise InvalidStateError.
        mqtt._handle_incoming(_get_result_topic(), json.dumps(payload).encode())
        assert mqtt._discover_future.result() == []  # unchanged


class TestTopicRouting:
    def test_unrecognised_topic_suffix_is_ignored(self, mqtt: OrbitMqtt) -> None:
        received: list[tuple[str, DoorState]] = []
        mqtt.on_door_state(lambda hub, state: received.append((hub, state)))

        # /desire — we publish there, don't subscribe. If the broker echoed
        # it back to us, we should ignore rather than crash.
        desire_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_DESIRE)
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 4}}}}
        mqtt._handle_incoming(desire_topic, json.dumps(payload).encode())
        assert received == []
