"""Unit tests for OrbitClient orchestrator.

HTTP calls are exercised against aioresponses (real OrbitHttp talking
to mocked endpoints). MQTT is replaced with an AsyncMock so we assert
call semantics without needing a broker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

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
from custom_components.skylink._client.errors import (
    OrbitAuthError,
    OrbitConnectionError,
)
from custom_components.skylink._client.http import OrbitHttp
from custom_components.skylink._client.mqtt import OrbitMqtt

_LOGIN_URL = f"{protocol.REST_BASE_URL}?cmd=act_login"


@pytest.fixture
def fake_mqtt() -> MagicMock:
    mock = MagicMock(spec=OrbitMqtt)
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.discover = AsyncMock()
    mock.publish_toggle = AsyncMock()
    mock.is_connected = False
    return mock


@pytest.fixture
async def client(fake_mqtt: MagicMock) -> AsyncIterator[OrbitClient]:
    http = OrbitHttp()
    c = OrbitClient(http=http, mqtt=fake_mqtt)
    try:
        yield c
    finally:
        await c.disconnect()


class TestAuthenticate:
    async def test_success_returns_login_result_and_stores_acc_no(
        self, client: OrbitClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                _LOGIN_URL,
                status=200,
                body='{"result":"00","acc_no":"8003105701","alias_name":"Brad"}',
            )
            result = await client.authenticate("user@example.com", "hunter2")

        assert result.acc_no == "8003105701"
        assert result.alias_name == "Brad"
        assert client.acc_no == "8003105701"
        assert client.is_authenticated

    async def test_bad_credentials_translate_to_auth_error(
        self, client: OrbitClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                _LOGIN_URL,
                status=200,
                body='{"result":"11","message":"bad password"}',
            )
            with pytest.raises(OrbitAuthError, match="Login rejected"):
                await client.authenticate("user@example.com", "wrong")

        assert not client.is_authenticated
        assert client.acc_no == ""

    async def test_bare_zero_result_handled(self, client: OrbitClient) -> None:
        # Server quirk: result:00 as bare number. http layer's JSON fix-up
        # should propagate through authenticate() transparently.
        with aioresponses() as m:
            m.post(
                _LOGIN_URL,
                status=200,
                body='{"result":00,"acc_no":"800","alias_name":""}',
            )
            result = await client.authenticate("u@example.com", "p")
        assert result.acc_no == "800"

    async def test_network_error_propagates_as_connection_error(
        self, client: OrbitClient
    ) -> None:
        import aiohttp

        with aioresponses() as m:
            m.post(_LOGIN_URL, exception=aiohttp.ClientConnectionError("boom"))
            with pytest.raises(OrbitConnectionError):
                await client.authenticate("u@example.com", "p")

    async def test_malformed_response_becomes_auth_error(
        self, client: OrbitClient
    ) -> None:
        # Structurally broken JSON — at the login boundary we treat this
        # as "login failed" rather than leaking a generic protocol error.
        with aioresponses() as m:
            m.post(_LOGIN_URL, status=200, body="not json")
            with pytest.raises(OrbitAuthError, match="Login rejected"):
                await client.authenticate("u@example.com", "p")


class TestConnect:
    async def test_without_auth_raises(self, client: OrbitClient) -> None:
        with pytest.raises(OrbitAuthError, match="authenticate"):
            await client.connect()

    async def test_passes_acc_no_and_password_to_mqtt(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        with aioresponses() as m:
            m.post(
                _LOGIN_URL,
                status=200,
                body='{"result":"00","acc_no":"800","alias_name":""}',
            )
            await client.authenticate("u@example.com", "sekret")

        await client.connect()

        fake_mqtt.connect.assert_awaited_once_with("800", "sekret")


class TestOperations:
    async def test_toggle_delegates_to_mqtt(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        await client.toggle("hub_x", DeviceType.GDO)
        fake_mqtt.publish_toggle.assert_awaited_once_with("hub_x", DeviceType.GDO)

    async def test_discover_delegates_to_mqtt(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        expected = [
            DeviceSnapshot(
                device=Device(hub_id="a", name="A", device_type=DeviceType.GDO),
                state=DoorState.CLOSED,
            )
        ]
        fake_mqtt.discover.return_value = expected

        result = await client.discover(timeout=2.5)

        fake_mqtt.discover.assert_awaited_once_with(2.5)
        assert result == expected

    async def test_on_door_state_forwards_to_mqtt(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        def handler(hub: str, state: DoorState) -> None:
            pass

        client.on_door_state(handler)
        fake_mqtt.on_door_state.assert_called_once_with(handler)

    async def test_disconnect_closes_both_transports(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        await client.disconnect()
        fake_mqtt.disconnect.assert_awaited_once()


class TestAsyncContextManager:
    async def test_aexit_disconnects(self, fake_mqtt: MagicMock) -> None:
        http = OrbitHttp()
        async with OrbitClient(http=http, mqtt=fake_mqtt) as c:
            assert c is not None

        fake_mqtt.disconnect.assert_awaited_once()


class TestIsConnected:
    async def test_reflects_mqtt_state(
        self, client: OrbitClient, fake_mqtt: MagicMock
    ) -> None:
        fake_mqtt.is_connected = False
        assert not client.is_connected
        fake_mqtt.is_connected = True
        assert client.is_connected
