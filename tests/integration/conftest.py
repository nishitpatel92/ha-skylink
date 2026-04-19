"""Fixtures for integration tests.

Integration tests require a running mosquitto broker. The expected flow
is to invoke the suite via `./scripts/test-integration.sh`, which
manages the container and exports `MOSQUITTO_HOST` / `MOSQUITTO_PORT`.
Running `pytest tests/integration/` without those env vars will
deliberately skip the whole suite with a helpful message.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from custom_components.skylink._client.mqtt import OrbitMqtt

_HOST_ENV = "MOSQUITTO_HOST"
_PORT_ENV = "MOSQUITTO_PORT"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip the integration suite when the broker env vars are missing."""
    if os.environ.get(_HOST_ENV) and os.environ.get(_PORT_ENV):
        return
    reason = (
        "Integration tests require a mosquitto broker. Run via "
        "`./scripts/test-integration.sh` or set MOSQUITTO_HOST/MOSQUITTO_PORT."
    )
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        item.add_marker(marker)


@pytest.fixture(scope="session")
def mosquitto() -> tuple[str, int]:
    """Return (host, port) of the running test broker."""
    return (os.environ[_HOST_ENV], int(os.environ[_PORT_ENV]))


@pytest.fixture
async def orbit_mqtt(mosquitto: tuple[str, int]) -> AsyncIterator[OrbitMqtt]:
    """OrbitMqtt wired to the local test broker (TLS disabled)."""
    host, port = mosquitto
    mqtt = OrbitMqtt(host=host, port=port, enable_tls=False)
    try:
        yield mqtt
    finally:
        await mqtt.disconnect()
