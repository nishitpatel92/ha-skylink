"""Tests for the HTTP transport adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.skylink._client import protocol
from custom_components.skylink._client.errors import OrbitConnectionError, OrbitProtocolError
from custom_components.skylink._client.http import OrbitHttp

_URL = f"{protocol.REST_BASE_URL}?cmd=act_login"


@pytest.fixture
async def http() -> AsyncIterator[OrbitHttp]:
    client = OrbitHttp()
    try:
        yield client
    finally:
        await client.close()


class TestPostHappyPath:
    async def test_returns_parsed_body(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, status=200, body='{"result":"00","acc_no":"8003105701"}')
            data = await http.post("act_login", {"username": "x"}, "x")
        assert data == {"result": "00", "acc_no": "8003105701"}

    async def test_fixes_bare_zero_result(self, http: OrbitHttp) -> None:
        # Real server quirk: result:00 as a bare number.
        with aioresponses() as m:
            m.post(_URL, status=200, body='{"result":00,"acc_no":"800"}')
            data = await http.post("act_login", {}, "x")
        assert data["result"] == "00"

    async def test_attaches_signed_headers(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, status=200, body='{"result":"0"}')
            await http.post("act_login", {"x": 1}, "user@example.com")
            # aioresponses records every call in m.requests keyed by (method, URL).
            calls = next(iter(m.requests.values()))
            headers = calls[0].kwargs["headers"]

        for key in ("REQ-CMD", "REQ-DATA", "REQ-TIMESTAMP", "REQ-SIGNATURE", "User-Agent"):
            assert key in headers, f"missing header: {key}"
        assert headers["REQ-CMD"] == "act_login"
        assert headers["REQ-DATA"] == "user@example.com"
        # Signature hex is lowercase and 32 chars
        assert len(headers["REQ-SIGNATURE"]) == 32
        assert headers["REQ-SIGNATURE"].islower()


class TestPostErrors:
    async def test_connection_error_is_translated(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, exception=aiohttp.ClientConnectionError("boom"))
            with pytest.raises(OrbitConnectionError, match="Connection error"):
                await http.post("act_login", {}, "x")

    async def test_timeout_is_translated(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, exception=TimeoutError())
            with pytest.raises(OrbitConnectionError, match="Timeout"):
                await http.post("act_login", {}, "x")

    async def test_http_500_raises_protocol_error(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, status=500, body="oops")
            with pytest.raises(OrbitProtocolError, match="HTTP 500"):
                await http.post("act_login", {}, "x")

    async def test_http_401_raises_protocol_error(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, status=401, body="no")
            with pytest.raises(OrbitProtocolError, match="HTTP 401"):
                await http.post("act_login", {}, "x")

    async def test_malformed_body_raises_protocol_error(self, http: OrbitHttp) -> None:
        with aioresponses() as m:
            m.post(_URL, status=200, body="this is not json")
            with pytest.raises(OrbitProtocolError, match="non-JSON"):
                await http.post("act_login", {}, "x")

    async def test_server_result_code_raises_protocol_error(self, http: OrbitHttp) -> None:
        # result=25 is the server's "invalid signature" code — a protocol bug
        # from our side. client.py turns login-time protocol errors into
        # OrbitAuthError; http.py stays agnostic.
        with aioresponses() as m:
            m.post(_URL, status=200, body='{"result":"25","message":"bad sig"}')
            with pytest.raises(OrbitProtocolError, match="result=25"):
                await http.post("act_login", {}, "x")


class TestSessionOwnership:
    async def test_reuses_external_session(self) -> None:
        async with aiohttp.ClientSession() as external:
            http = OrbitHttp(session=external)
            try:
                with aioresponses() as m:
                    m.post(_URL, status=200, body='{"result":"0"}')
                    await http.post("act_login", {}, "x")
            finally:
                await http.close()
            # close() on an external session should be a no-op.
            assert not external.closed

    async def test_closes_owned_session(self) -> None:
        http = OrbitHttp()
        with aioresponses() as m:
            m.post(_URL, status=200, body='{"result":"0"}')
            await http.post("act_login", {}, "x")
        assert http._session is not None
        session = http._session
        await http.close()
        assert session.closed
