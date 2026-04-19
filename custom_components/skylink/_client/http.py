"""HTTP transport adapter (aiohttp).

Single primitive: `OrbitHttp.post(cmd, body, req_data) -> dict`. Caller is
responsible for building the body and supplying `req_data` (which is what
gets signed). Response is parsed and the `result` code is validated.

TLS uses the system trust store. The Orbit Home APK verifies HTTPS
normally (RetrofitUtil.java:42 — no custom SSLSocketFactory installed);
we follow suit. Disabling verification would be a gratuitous downgrade.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from . import protocol
from .errors import OrbitConnectionError, OrbitProtocolError

_DEFAULT_TIMEOUT_SECONDS = 20


class OrbitHttp:
    """Thin async wrapper over aiohttp for signed Orbit REST calls."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        base_url: str = protocol.REST_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the HTTP session if we own it."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def post(
        self,
        cmd: str,
        body: dict[str, Any],
        req_data: str,
    ) -> dict[str, Any]:
        """Send a signed POST and return the parsed, validated response.

        Raises:
            OrbitConnectionError — network, DNS, TLS handshake, timeout.
            OrbitProtocolError — HTTP non-2xx, malformed body, non-success
                `result` code.
        """
        session = await self._get_session()
        url = f"{self._base_url}?cmd={cmd}"
        headers = protocol.build_request_headers(cmd, req_data)

        try:
            async with session.post(url, json=body, headers=headers, timeout=self._timeout) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise OrbitProtocolError(
                        f"HTTP {resp.status} for {cmd}: {text[:200]!r}"
                    )
                data = protocol.parse_response_body(text, cmd)
                protocol.check_success(data, cmd)
                return data
        except TimeoutError as err:
            raise OrbitConnectionError(f"Timeout calling {cmd}") from err
        except aiohttp.ClientError as err:
            raise OrbitConnectionError(f"Connection error calling {cmd}: {err}") from err
