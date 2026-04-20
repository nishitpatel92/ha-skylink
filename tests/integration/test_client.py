"""End-to-end integration tests for OrbitClient.

Exercises the full stack against a live mosquitto (for MQTT) and an
aioresponses-mocked HTTPS endpoint (for the REST login). The existing
tests/integration/test_mqtt.py covers the MQTT layer alone; this
suite adds the HTTP + MQTT orchestration that the HA integration
actually uses at runtime.

What's covered here but not elsewhere:
  * authenticate() → discover() → toggle() → state push round-trip
  * discovery returning initial states as DeviceSnapshots
  * toggle publishing reaching /desire with the APK-correct payload
  * state update callbacks wiring through from /update/result
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import aiohttp
import aiomqtt
import pytest
from aioresponses import aioresponses

from custom_components.skylink._client import protocol
from custom_components.skylink._client.client import OrbitClient
from custom_components.skylink._client.domain import (
    Device,
    DeviceSnapshot,
    DeviceType,
    DoorState,
)
from custom_components.skylink._client.http import OrbitHttp
from custom_components.skylink._client.mqtt import OrbitMqtt

_EMAIL = "nishit@example.com"
_PASSWORD = "sekret"
_ACC_NO = "8003105701"
_HUB_ID = "rA8qM4QS"
_LOGIN_URL = f"{protocol.REST_BASE_URL}?cmd=act_login"


@pytest.fixture
async def client(mosquitto: tuple[str, int]) -> AsyncIterator[OrbitClient]:
    """OrbitClient wired to the test broker and default (network) HTTP.

    REST calls need to be mocked with aioresponses in each test.
    """
    host, port = mosquitto
    http = OrbitHttp()
    mqtt = OrbitMqtt(host=host, port=port, enable_tls=False)
    c = OrbitClient(http=http, mqtt=mqtt)
    try:
        yield c
    finally:
        await c.disconnect()


@pytest.fixture
async def fake_hub(mosquitto: tuple[str, int]) -> AsyncIterator[aiomqtt.Client]:
    """A second aiomqtt client that plays the Skylink hub role."""
    host, port = mosquitto
    async with aiomqtt.Client(
        hostname=host, port=port, identifier="test-fake-hub"
    ) as hub:
        yield hub


def _login_body(acc_no: str = _ACC_NO, alias: str = "Nishit") -> str:
    return json.dumps({"result": "00", "acc_no": acc_no, "alias_name": alias})


class TestFullFlow:
    async def test_authenticate_discover_toggle_state_push(
        self,
        client: OrbitClient,
        fake_hub: aiomqtt.Client,
        mosquitto: tuple[str, int],
    ) -> None:
        """The whole HA integration path in one test.

        We stand up a fake hub that responds to the /get request with a
        realistic payload (including reported.mdev.door) and echoes
        /desire publishes. Then we run the client through its full
        lifecycle: auth → connect → discover → register callback →
        toggle → state-change simulation.
        """
        received_state_updates: list[tuple[str, DoorState]] = []
        state_change_event = asyncio.Event()

        def on_state(hub_id: str, state: DoorState) -> None:
            received_state_updates.append((hub_id, state))
            state_change_event.set()

        client.on_door_state(on_state)

        # Auth is HTTPS — mock it with aioresponses. MQTT uses the live
        # broker directly.
        with aioresponses() as m:
            m.post(_LOGIN_URL, status=200, body=_login_body())

            # 1. authenticate
            result = await client.authenticate(_EMAIL, _PASSWORD)
            assert result.acc_no == _ACC_NO
            assert client.is_authenticated

            # 2. connect MQTT
            await client.connect()
            assert client.is_connected

            # Fake hub: subscribe to /get (so it can respond to discovery)
            # and /desire (so it can confirm our toggle reached the broker).
            get_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_GET)
            get_result_topic = protocol.mqtt_topic(
                _ACC_NO, protocol.TOPIC_SUFFIX_GET_RESULT
            )
            desire_topic = protocol.mqtt_topic(_ACC_NO, protocol.TOPIC_SUFFIX_DESIRE)
            update_topic = protocol.mqtt_topic(
                _ACC_NO, protocol.TOPIC_SUFFIX_UPDATE_RESULT
            )
            await fake_hub.subscribe(get_topic)
            await fake_hub.subscribe(desire_topic)

            desire_received = asyncio.Event()
            desire_payloads: list[bytes] = []

            async def hub_loop() -> None:
                async for msg in fake_hub.messages:
                    topic_str = str(msg.topic)
                    if topic_str == get_topic:
                        # Respond with device list including current state.
                        response = {
                            "data": [
                                {
                                    "hub_id": _HUB_ID,
                                    "type": "GDO",
                                    "name": "Main Garage",
                                    "reported": {"mdev": {"door": 4}},
                                }
                            ]
                        }
                        await fake_hub.publish(
                            get_result_topic, json.dumps(response)
                        )
                    elif topic_str == desire_topic:
                        desire_payloads.append(bytes(msg.payload))
                        desire_received.set()

            hub_task = asyncio.create_task(hub_loop())
            try:
                # Give subscriptions time to register
                await asyncio.sleep(0.2)

                # 3. discover — should return our fake device with CLOSED state
                snapshots = await client.discover(timeout=5.0)
                assert snapshots == [
                    DeviceSnapshot(
                        device=Device(
                            hub_id=_HUB_ID,
                            name="Main Garage",
                            device_type=DeviceType.GDO,
                        ),
                        state=DoorState.CLOSED,
                    )
                ]

                # 4. toggle — should publish on /desire
                await client.toggle(_HUB_ID, DeviceType.GDO)
                await asyncio.wait_for(desire_received.wait(), timeout=3.0)
                assert len(desire_payloads) == 1
                payload = json.loads(desire_payloads[0])
                ctrl = payload["data"]["desired"]["mdev"]["ctrlgdo"]
                assert payload["data"]["hub_id"] == _HUB_ID
                assert ctrl["cmd"] == 0
                assert "position" not in ctrl  # GDO has no A/B selector

                # 5. simulate hub reporting OPENING via /update/result
                push = {
                    "data": {
                        "hub_id": _HUB_ID,
                        "reported": {"mdev": {"door": 0}},  # OPENING
                    }
                }
                await fake_hub.publish(update_topic, json.dumps(push))
                await asyncio.wait_for(state_change_event.wait(), timeout=3.0)

                assert received_state_updates == [(_HUB_ID, DoorState.OPENING)]

            finally:
                hub_task.cancel()


class TestAuthFailure:
    async def test_rejected_credentials_translate(
        self, client: OrbitClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                _LOGIN_URL,
                status=200,
                body='{"result":"11","message":"bad password"}',
            )
            from custom_components.skylink._client.errors import OrbitAuthError

            with pytest.raises(OrbitAuthError, match="Login rejected"):
                await client.authenticate(_EMAIL, "wrong-password")
        assert not client.is_authenticated

    async def test_connect_without_auth_refused(
        self, client: OrbitClient
    ) -> None:
        from custom_components.skylink._client.errors import OrbitAuthError

        with pytest.raises(OrbitAuthError, match="authenticate"):
            await client.connect()


class TestNetworkFailure:
    async def test_rest_connection_error_propagates(
        self, client: OrbitClient
    ) -> None:
        from custom_components.skylink._client.errors import OrbitConnectionError

        with aioresponses() as m:
            m.post(_LOGIN_URL, exception=aiohttp.ClientConnectionError("boom"))
            with pytest.raises(OrbitConnectionError):
                await client.authenticate(_EMAIL, _PASSWORD)
