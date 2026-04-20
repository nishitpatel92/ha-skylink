"""OrbitClient — the high-level orchestrator.

Ties HTTP (for auth) and MQTT (for everything else) together behind a
single async API. Exposes login, device discovery, door-state
subscription, and door toggle. Translates low-level protocol errors at
the authentication boundary (server `result` codes become
OrbitAuthError from the caller's point of view).

Usable standalone from a CLI or script, as well as from the HA
coordinator.
"""

from __future__ import annotations

from typing import Self

from . import protocol
from .domain import DeviceSnapshot, DeviceType
from .errors import OrbitAuthError, OrbitProtocolError
from .http import OrbitHttp
from .mqtt import DoorStateCallback, OrbitMqtt
from .protocol import LoginResult


class OrbitClient:
    """High-level async client for the Skylink / Orbit Home cloud.

    Lifecycle:
        client = OrbitClient()
        await client.authenticate(email, password)
        await client.connect()
        devices = await client.discover()
        client.on_door_state(handle_state)
        await client.toggle(hub_id, DeviceType.GDO)
        ...
        await client.disconnect()

    Transports are injectable for testing — pass in pre-configured
    `OrbitHttp` / `OrbitMqtt` instances to isolate from real IO.
    """

    def __init__(
        self,
        http: OrbitHttp | None = None,
        mqtt: OrbitMqtt | None = None,
    ) -> None:
        self._http = http if http is not None else OrbitHttp()
        self._mqtt = mqtt if mqtt is not None else OrbitMqtt()
        self._acc_no: str = ""
        self._password: str = ""
        self._authenticated = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> LoginResult:
        """Log in via REST and cache the account number.

        Raises:
            OrbitAuthError — bad credentials, or any server-side rejection
                of the login call.
            OrbitConnectionError — unreachable cloud.
        """
        body = protocol.build_login_body(username, password)
        try:
            data = await self._http.post(protocol.CMD_LOGIN, body, username)
        except OrbitProtocolError as err:
            # At the login boundary, "protocol error" means "login rejected"
            # from the caller's perspective. Translate.
            raise OrbitAuthError(f"Login rejected: {err}") from err

        result = protocol.parse_login_response(data)
        self._acc_no = result.acc_no
        self._password = password
        self._authenticated = True
        return result

    async def connect(self) -> None:
        """Open the MQTT session. Requires a prior successful authenticate().

        Raises:
            OrbitAuthError — authenticate() not yet called successfully, or
                MQTT broker rejected credentials.
            OrbitConnectionError — broker unreachable or connect timeout.
        """
        if not self._authenticated:
            raise OrbitAuthError("authenticate() must succeed before connect()")
        await self._mqtt.connect(self._acc_no, self._password)

    async def disconnect(self) -> None:
        """Close both transports. Safe to call without ever connecting."""
        await self._mqtt.disconnect()
        await self._http.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def on_door_state(self, callback: DoorStateCallback) -> None:
        """Register a callback fired for every door-state push."""
        self._mqtt.on_door_state(callback)

    async def discover(self, timeout: float = 5.0) -> list[DeviceSnapshot]:
        """Request the device list + current states via MQTT."""
        return await self._mqtt.discover(timeout)

    async def toggle(self, hub_id: str, device_type: DeviceType) -> None:
        """Send a toggle command to one door."""
        await self._mqtt.publish_toggle(hub_id, device_type)

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def acc_no(self) -> str:
        """The account number, populated after a successful authenticate()."""
        return self._acc_no

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def is_connected(self) -> bool:
        """MQTT session alive."""
        return self._mqtt.is_connected
