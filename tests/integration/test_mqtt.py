"""MQTT integration tests against a live mosquitto broker.

Runs only when `MOSQUITTO_HOST` + `MOSQUITTO_PORT` are in the environment
(set by `scripts/test-integration.sh`).

Scenarios covered here are the ones that unit tests can't: actual
socket lifecycle, aiomqtt behaviour on real publishes, subscription
delivery, and the full discover() request/response rendezvous.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import aiomqtt
import pytest

from custom_components.skylink._client import protocol
from custom_components.skylink._client.domain import (
    Device,
    DeviceSnapshot,
    DeviceType,
    DoorState,
)
from custom_components.skylink._client.errors import OrbitConnectionError
from custom_components.skylink._client.mqtt import OrbitMqtt

# acc_no used in tests — arbitrary, but consistent so topic filters line up
_ACC_NO = "test8003105701"
_PASSWORD = "test-password"
_HUB_ID = "rA8qM4QS"


@pytest.fixture
async def hub(mosquitto: tuple[str, int]) -> AsyncIterator[aiomqtt.Client]:
    """A raw aiomqtt client playing the role of a Skylink hub.

    Used to publish state updates / discovery responses the test broker
    will fan back to our OrbitMqtt under test.
    """
    host, port = mosquitto
    async with aiomqtt.Client(hostname=host, port=port, identifier="fake-hub") as client:
        yield client


async def _wait_for(
    condition: asyncio.Event, timeout: float = 3.0, *, why: str
) -> None:
    try:
        await asyncio.wait_for(condition.wait(), timeout)
    except TimeoutError as err:
        raise AssertionError(f"timed out waiting for: {why}") from err


class TestConnect:
    async def test_connect_and_disconnect(self, orbit_mqtt: OrbitMqtt) -> None:
        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)
        assert orbit_mqtt.is_connected
        await orbit_mqtt.disconnect()
        assert not orbit_mqtt.is_connected

    async def test_connect_twice_raises(self, orbit_mqtt: OrbitMqtt) -> None:
        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)
        with pytest.raises(Exception, match="Already connected"):
            await orbit_mqtt.connect(_ACC_NO, _PASSWORD)


class TestStateUpdatePush:
    async def test_push_triggers_callback(
        self, orbit_mqtt: OrbitMqtt, hub: aiomqtt.Client
    ) -> None:
        received: list[tuple[str, DoorState]] = []
        got_update = asyncio.Event()

        def on_state(hub_id: str, state: DoorState) -> None:
            received.append((hub_id, state))
            got_update.set()

        orbit_mqtt.on_door_state(on_state)
        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)

        update_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_UPDATE_RESULT)
        payload = {"data": {"hub_id": _HUB_ID, "reported": {"mdev": {"door": 4}}}}
        await hub.publish(update_topic, json.dumps(payload))

        await _wait_for(got_update, why="state update callback")
        assert received == [(_HUB_ID, DoorState.CLOSED)]

    async def test_multiple_pushes_all_delivered(
        self, orbit_mqtt: OrbitMqtt, hub: aiomqtt.Client
    ) -> None:
        received: list[tuple[str, DoorState]] = []
        loop = asyncio.get_running_loop()
        got_three = loop.create_future()

        def on_state(hub_id: str, state: DoorState) -> None:
            received.append((hub_id, state))
            if len(received) == 3 and not got_three.done():
                got_three.set_result(None)

        orbit_mqtt.on_door_state(on_state)
        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)

        update_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_UPDATE_RESULT)
        for door_val in (0, 1, 4):  # opening, open, closed
            payload = {"data": {"hub_id": _HUB_ID, "reported": {"mdev": {"door": door_val}}}}
            await hub.publish(update_topic, json.dumps(payload))

        await asyncio.wait_for(got_three, timeout=3.0)
        assert [s for _, s in received] == [
            DoorState.OPENING,
            DoorState.OPEN,
            DoorState.CLOSED,
        ]


class TestPublishToggle:
    async def test_reaches_broker(
        self, orbit_mqtt: OrbitMqtt, mosquitto: tuple[str, int]
    ) -> None:
        """A separate subscriber on /desire should receive the toggle payload."""
        host, port = mosquitto
        desire_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_DESIRE)

        received: list[bytes] = []
        got_msg = asyncio.Event()

        async def listen() -> None:
            async with aiomqtt.Client(
                hostname=host, port=port, identifier="desire-listener"
            ) as listener:
                await listener.subscribe(desire_topic)
                async for message in listener.messages:
                    received.append(bytes(message.payload))
                    got_msg.set()
                    return

        listen_task = asyncio.create_task(listen())
        # Give the subscriber time to register
        await asyncio.sleep(0.2)

        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)
        await orbit_mqtt.publish_toggle(_HUB_ID, DeviceType.GDO)

        await _wait_for(got_msg, why="/desire publish")
        listen_task.cancel()

        payload = json.loads(received[0])
        assert payload["data"]["hub_id"] == _HUB_ID
        ctrl = payload["data"]["desired"]["mdev"]["ctrlgdo"]
        assert ctrl["cmd"] == 0
        assert "position" not in ctrl  # GDO — no position

    async def test_toggle_without_connection_raises(self, orbit_mqtt: OrbitMqtt) -> None:
        with pytest.raises(OrbitConnectionError, match="not connected"):
            await orbit_mqtt.publish_toggle(_HUB_ID, DeviceType.GDO)


class TestDiscover:
    async def test_roundtrip_returns_devices(
        self, orbit_mqtt: OrbitMqtt, hub: aiomqtt.Client, mosquitto: tuple[str, int]
    ) -> None:
        """Start discover() in one task; the fake hub publishes /get/result
        in response to the /get publish. The coroutine should resolve with
        the parsed device list.
        """
        host, port = mosquitto
        get_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_GET)
        get_result_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_GET_RESULT)

        # Fake hub: subscribe to /get, answer on /get/result with
        # realistic payload shapes (one with reported.mdev.door, one
        # without) so we confirm both paths produce the right snapshot.
        response = {
            "data": [
                {
                    "hub_id": "hubA",
                    "type": "GDO",
                    "name": "Main",
                    "reported": {"mdev": {"door": 4}},
                },
                {"hub_id": "hubB", "type": "NOVA_A"},
            ]
        }

        async def fake_hub() -> None:
            async with aiomqtt.Client(
                hostname=host, port=port, identifier="fake-hub-responder"
            ) as responder:
                await responder.subscribe(get_topic)
                async for _ in responder.messages:
                    await responder.publish(get_result_topic, json.dumps(response))
                    return

        hub_task = asyncio.create_task(fake_hub())
        await asyncio.sleep(0.2)  # let the fake hub subscribe

        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)
        snapshots = await orbit_mqtt.discover(timeout=3.0)
        hub_task.cancel()

        assert snapshots == [
            DeviceSnapshot(
                device=Device(hub_id="hubA", name="Main", device_type=DeviceType.GDO),
                state=DoorState.CLOSED,
            ),
            DeviceSnapshot(
                device=Device(
                    hub_id="hubB", name="Skylink hubB", device_type=DeviceType.NOVA_A
                ),
                state=DoorState.UNKNOWN,
            ),
        ]

    async def test_timeout_when_no_response(self, orbit_mqtt: OrbitMqtt) -> None:
        await orbit_mqtt.connect(_ACC_NO, _PASSWORD)
        with pytest.raises(OrbitConnectionError, match="timed out"):
            await orbit_mqtt.discover(timeout=0.5)
