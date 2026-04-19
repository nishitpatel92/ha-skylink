"""MQTT transport adapter (aiomqtt).

Async-native. Runs a single background task that connects, subscribes,
and fans incoming messages out to registered callbacks. Reconnects with
backoff on transient failures; auth failures surface to the caller and
do not retry.

TLS is enabled but certificate and hostname verification are disabled.
This matches the Orbit Home app's `getUnsafeSocketFactory` at
MainActivity.java:1011 — the broker's cert doesn't validate against the
system trust store (it's addressed by hardcoded IPv4). Disabling
verification is specifically required to talk to this broker, not a
general defensibility choice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from collections.abc import Callable
from typing import Any

import aiomqtt

from . import protocol
from .domain import Device, DeviceType, DoorState
from .errors import OrbitAuthError, OrbitConnectionError, OrbitProtocolError

_LOGGER = logging.getLogger(__name__)

# MQTT CONNACK reason codes that indicate the credentials are wrong
# rather than a transient network issue. Taken from MQTT 3.1.1 spec.
_AUTH_FAILURE_RCS = frozenset({4, 5})  # Bad credentials, Not authorised

_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_DISCOVER_TIMEOUT = 5.0
_RECONNECT_MIN_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0


DoorStateCallback = Callable[[str, DoorState], None]


def _make_tls_context() -> ssl.SSLContext:
    """TLS with certificate + hostname verification disabled.

    Matches `getUnsafeSocketFactory` and the pinned-IP HostnameVerifier in
    the Orbit Home APK. The broker at 34.214.223.70:1899 does not present
    a cert that validates against the public trust store.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class OrbitMqtt:
    """MQTT client for the Orbit broker.

    Usage:
        mqtt = OrbitMqtt()
        mqtt.on_door_state(coordinator.update_state)
        await mqtt.connect(acc_no, password)
        devices = await mqtt.discover()
        await mqtt.publish_toggle(hub_id, DeviceType.GDO)
        ...
        await mqtt.disconnect()
    """

    def __init__(
        self,
        host: str = protocol.MQTT_BROKER_HOST,
        port: int = protocol.MQTT_BROKER_PORT,
        keepalive: int = protocol.MQTT_KEEPALIVE_SECONDS,
        *,
        tls_context: ssl.SSLContext | None = None,
        enable_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._keepalive = keepalive
        if enable_tls:
            self._tls_context: ssl.SSLContext | None = (
                tls_context if tls_context is not None else _make_tls_context()
            )
        else:
            # Integration-test path: local mosquitto without TLS. The real
            # broker requires TLS — production callers should leave the
            # default.
            self._tls_context = None

        self._acc_no = ""
        self._password = ""
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected_event: asyncio.Event | None = None
        self._stopping = False
        self._auth_failed = False

        self._state_callback: DoorStateCallback | None = None
        self._discover_future: asyncio.Future[list[Device]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True if the broker session is currently alive."""
        return self._client is not None and not self._stopping

    def on_door_state(self, callback: DoorStateCallback) -> None:
        """Register the callback invoked for every /update/result push.

        Signature: callback(hub_id, door_state). Called in the asyncio
        event loop from the message-routing task.
        """
        self._state_callback = callback

    async def connect(
        self,
        acc_no: str,
        password: str,
        timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Open the MQTT session. Returns once the first CONNACK arrives.

        Raises:
            OrbitAuthError — broker rejected credentials (rc=4 or rc=5).
            OrbitConnectionError — timeout or non-auth connect failure.
        """
        if self._task is not None:
            raise OrbitProtocolError("Already connected or connecting")
        self._acc_no = acc_no
        self._password = password
        self._stopping = False
        self._auth_failed = False
        self._connected_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="orbit-mqtt")

        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except TimeoutError as err:
            self._stopping = True
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._task
            self._task = None
            raise OrbitConnectionError(f"MQTT connect timed out after {timeout}s") from err

        if self._auth_failed:
            # The run loop already stopped itself; clean up bookkeeping.
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._task
            self._task = None
            raise OrbitAuthError("MQTT broker rejected credentials")

    async def disconnect(self) -> None:
        """Tear down the session. Safe to call if never connected."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._task
            self._task = None
        self._client = None

    async def publish_toggle(self, hub_id: str, device_type: DeviceType) -> None:
        """Publish a toggle command to /desire for one hub."""
        if self._client is None:
            raise OrbitConnectionError("MQTT not connected")
        payload = protocol.build_toggle_payload(hub_id, device_type)
        topic = protocol.mqtt_topic(self._acc_no, protocol.TOPIC_SUFFIX_DESIRE)
        await self._client.publish(topic, json.dumps(payload, separators=(",", ":")))

    async def discover(self, timeout: float = _DEFAULT_DISCOVER_TIMEOUT) -> list[Device]:
        """Publish `{}` to /get and wait for the /get/result response.

        Only one discovery may be in flight at a time.
        """
        if self._client is None:
            raise OrbitConnectionError("MQTT not connected")
        if self._discover_future is not None and not self._discover_future.done():
            raise OrbitProtocolError("discover() already in flight")

        loop = asyncio.get_running_loop()
        self._discover_future = loop.create_future()
        try:
            topic = protocol.mqtt_topic(self._acc_no, protocol.TOPIC_SUFFIX_GET)
            await self._client.publish(topic, b"{}")
            return await asyncio.wait_for(self._discover_future, timeout)
        except TimeoutError as err:
            raise OrbitConnectionError(f"discover timed out after {timeout}s") from err
        finally:
            self._discover_future = None

    # ------------------------------------------------------------------
    # Incoming-message dispatch
    #
    # Exposed for direct unit testing. Production code reaches it via
    # the _run() task — nothing outside this class should call it.
    # ------------------------------------------------------------------

    def _handle_incoming(self, topic: str, payload_bytes: bytes) -> None:
        suffix = self._topic_suffix(topic)
        if suffix is None:
            return

        try:
            payload: Any = json.loads(payload_bytes)
        except json.JSONDecodeError:
            _LOGGER.warning("Dropping non-JSON MQTT message on %s", topic)
            return

        if suffix == protocol.TOPIC_SUFFIX_UPDATE_RESULT:
            self._dispatch_state_update(payload)
        elif suffix == protocol.TOPIC_SUFFIX_GET_RESULT:
            self._dispatch_discover_response(payload)

    def _topic_suffix(self, topic: str) -> str | None:
        """Return the trailing suffix if `topic` belongs to our acc_no, else None."""
        prefix = f"skylink/things/client/{self._acc_no}/"
        if not topic.startswith(prefix):
            return None
        return topic[len(prefix) :]

    def _dispatch_state_update(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            parsed = protocol.parse_state_update(payload)
        except OrbitProtocolError as err:
            _LOGGER.warning("Ignoring malformed state update: %s", err)
            return
        if parsed is None:
            return
        hub_id, state = parsed
        if self._state_callback is not None:
            try:
                self._state_callback(hub_id, state)
            except Exception:
                # Isolate callback bugs — a raise here would kill the
                # message-routing loop and silently stop all state updates.
                _LOGGER.exception("State callback raised for hub %s", hub_id)

    def _dispatch_discover_response(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        future = self._discover_future
        if future is None or future.done():
            return
        try:
            devices = protocol.parse_discover_response(payload)
        except OrbitProtocolError as err:
            future.set_exception(err)
            return
        future.set_result(devices)

    # ------------------------------------------------------------------
    # Background task: connect, subscribe, route, reconnect
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        delay = _RECONNECT_MIN_DELAY
        while not self._stopping:
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._acc_no,
                    password=self._password,
                    tls_context=self._tls_context,
                    keepalive=self._keepalive,
                ) as client:
                    self._client = client
                    await client.subscribe(
                        protocol.mqtt_topic(self._acc_no, protocol.TOPIC_SUFFIX_UPDATE_RESULT)
                    )
                    await client.subscribe(
                        protocol.mqtt_topic(self._acc_no, protocol.TOPIC_SUFFIX_GET_RESULT)
                    )
                    assert self._connected_event is not None
                    self._connected_event.set()
                    delay = _RECONNECT_MIN_DELAY  # reset backoff on a clean session

                    async for message in client.messages:
                        self._handle_incoming(str(message.topic), bytes(message.payload))

            except aiomqtt.MqttCodeError as err:
                if err.rc in _AUTH_FAILURE_RCS:
                    _LOGGER.error("MQTT auth rejected (rc=%s) — not retrying", err.rc)
                    self._auth_failed = True
                    if self._connected_event is not None:
                        self._connected_event.set()
                    return
                _LOGGER.warning("MQTT error rc=%s, reconnecting in %.1fs", err.rc, delay)
            except aiomqtt.MqttError as err:
                if self._stopping:
                    return
                _LOGGER.warning("MQTT disconnected: %s — reconnecting in %.1fs", err, delay)
            finally:
                self._client = None

            if self._stopping:
                return

            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)
