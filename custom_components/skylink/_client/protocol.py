"""Orbit Home wire protocol — pure functions.

Every function here is deterministic and IO-free. Signing, header
construction, payload building, response parsing.

Based on audit of Orbit Home Android APK 3.8.1. See
`~/dev/skylink-review/findings/FINDINGS.md` for the reverse-engineered
details each constant below is derived from.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .domain import Device, DeviceSnapshot, DeviceType, DoorState
from .errors import OrbitProtocolError

# ---------------------------------------------------------------------------
# Constants (all derived from the APK — see FINDINGS.md for file:line refs)
# ---------------------------------------------------------------------------

# REST API (BuildConfig.SERVER_URL + endpoint file)
REST_BASE_URL = "https://iot.skyhm.net:8444/skylinkhub_crm/skyhm_api_s.jsp"

# MD5 seed appended to every signed request (HeadInterceptor.java:37)
SIGNING_SECRET = "+8uHDSF77ueRmLlKkl67"

# MQTT broker (MainActivity.java:912 — hardcoded IPv4 + port in the APK)
MQTT_BROKER_HOST = "34.214.223.70"
MQTT_BROKER_PORT = 1899
MQTT_KEEPALIVE_SECONDS = 30

# MQTT topic suffixes (MainActivity.java:208-211)
_TOPIC_BASE = "skylink/things/client"
TOPIC_SUFFIX_GET = "get"
TOPIC_SUFFIX_GET_RESULT = "get/result"
TOPIC_SUFFIX_DESIRE = "desire"
TOPIC_SUFFIX_UPDATE_RESULT = "update/result"

# REST command names — just the ones we use. The APK exposes many more
# (cam_*, hub_*, act_*) that this client doesn't need.
CMD_LOGIN = "act_login"

# GDO toggle command value (MainActivity.java:1154 — always 0 for doors).
# Other cmd integers exist for lights (3/4), sensor-add (8), sensor-del (15),
# but they are not door commands.
GDO_TOGGLE_CMD = 0

# App identity — sent in login payload + User-Agent. Server does not appear
# to validate these strictly, but mimicking the APK avoids future surprises.
USER_AGENT = "Orbit/3.4 (iPhone; iOS 18.3; Scale/3.00)"
LOGIN_APP_SYS = "apns"
LOGIN_APP_BRAND = "00"


# ---------------------------------------------------------------------------
# Signing + headers
# ---------------------------------------------------------------------------


def make_timestamp(now: float | None = None) -> str:
    """Return a whole-second-precision epoch-milliseconds string.

    Matches the APK's SimpleDateFormat round-trip, which drops millis
    (HeadInterceptor.java:32-35). Always ends in "000".

    Takes an optional `now` for deterministic testing.
    """
    seconds = int(now if now is not None else time.time())
    return str(seconds * 1000)


def sign(timestamp: str, cmd: str, req_data: str) -> str:
    """Compute the REQ-SIGNATURE header value.

    Formula (from HeadInterceptor.java:37):
        MD5(timestamp + "+" + cmd + "+" + req_data + SIGNING_SECRET).lower()
    """
    raw = f"{timestamp}+{cmd}+{req_data}{SIGNING_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_request_headers(cmd: str, req_data: str, now: float | None = None) -> dict[str, str]:
    """Assemble the full set of headers for a signed REST request."""
    ts = make_timestamp(now)
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
        "REQ-CMD": cmd,
        "REQ-DATA": req_data,
        "REQ-TIMESTAMP": ts,
        "REQ-SIGNATURE": sign(ts, cmd, req_data),
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

# Server returns "result":00 as a bare number with a leading zero, which
# is invalid JSON. The app patches it client-side (RetrofitUtil.java:104).
_BARE_LEADING_ZERO_RE = re.compile(r':(0\d+)([,}\]\s])')


def parse_response_body(body: str, cmd: str) -> dict[str, Any]:
    """Parse a server response body into a dict.

    Applies the leading-zero fix and validates the top-level shape.
    Raises OrbitProtocolError for anything that isn't a JSON object.
    """
    cleaned = body.lstrip("\ufeff").strip()
    if not cleaned:
        raise OrbitProtocolError(f"Empty response body for {cmd}")

    cleaned = _BARE_LEADING_ZERO_RE.sub(r':"\1"\2', cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as err:
        raise OrbitProtocolError(
            f"Server returned non-JSON for {cmd} at position {err.pos}: {body[:120]!r}"
        ) from err

    if not isinstance(data, dict):
        raise OrbitProtocolError(
            f"Expected JSON object for {cmd}, got {type(data).__name__}"
        )
    return data


def check_success(data: dict[str, Any], cmd: str) -> None:
    """Raise OrbitProtocolError if the `result` field isn't a success code.

    Success is either "0" or "00" (the app accepts both after the bare-zero
    fixup). Missing result means the server returned something we don't
    understand.
    """
    result = data.get("result")
    if result is None:
        raise OrbitProtocolError(f"Missing 'result' field in {cmd} response")
    if str(result) not in ("0", "00"):
        msg = data.get("message", "")
        raise OrbitProtocolError(f"{cmd} failed: result={result} message={msg!r}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginResult:
    acc_no: str
    alias_name: str


def build_login_body(username: str, password: str) -> dict[str, Any]:
    """Login payload shape (MainActivity login call site)."""
    return {
        "app_sys": LOGIN_APP_SYS,
        "username": username,
        "password": password,
        "app_brand": LOGIN_APP_BRAND,
    }


def parse_login_response(data: dict[str, Any]) -> LoginResult:
    """Extract acc_no + alias_name from a successful login response."""
    acc_no = data.get("acc_no")
    if not acc_no:
        raise OrbitProtocolError("Login response missing 'acc_no'")
    return LoginResult(acc_no=str(acc_no), alias_name=str(data.get("alias_name", "")))


# ---------------------------------------------------------------------------
# MQTT topics
# ---------------------------------------------------------------------------


def mqtt_topic(acc_no: str, suffix: str) -> str:
    """Compose an MQTT topic for the given account number and suffix."""
    if not acc_no:
        raise OrbitProtocolError("Cannot build MQTT topic without acc_no")
    return f"{_TOPIC_BASE}/{acc_no}/{suffix}"


# ---------------------------------------------------------------------------
# Device discovery (publish `{}` to /get, receive list on /get/result)
# ---------------------------------------------------------------------------


def build_discover_payload() -> dict[str, Any]:
    """MainActivity.java:1137 — app publishes literally `{}` to request the list."""
    return {}


def parse_discover_response(payload: dict[str, Any]) -> list[DeviceSnapshot]:
    """Parse a /get/result payload into a list of DeviceSnapshots.

    Shape (inferred from APK's `Gson().fromJson(str, TestData.class)` +
    SkyLinkDevice field names):
        {"data": [
            {"hub_id": "...", "type": "GDO", "name": "...",
             "reported": {"mdev": {"door": <int>, ...}}},
            ...
        ]}

    Each entry's `reported.mdev.door` carries the device's current state
    — the APK uses this as the baseline before any /update/result pushes
    arrive. Missing or malformed state falls through to DoorState.UNKNOWN
    so an otherwise-valid discovery response still surfaces the device.

    Unknown device types are skipped — Skylink may add new types we
    don't support.
    """
    raw_list = payload.get("data")
    if not isinstance(raw_list, list):
        raise OrbitProtocolError(
            f"Discovery response missing 'data' list, got {type(raw_list).__name__}"
        )

    snapshots: list[DeviceSnapshot] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        hub_id = entry.get("hub_id")
        type_str = entry.get("type", "")
        if not hub_id or not isinstance(hub_id, str):
            continue
        try:
            device_type = DeviceType(type_str)
        except ValueError:
            continue  # unknown/unsupported device type
        name = entry.get("name") or f"Skylink {hub_id}"

        reported = entry.get("reported")
        mdev = reported.get("mdev") if isinstance(reported, dict) else None
        door_val = mdev.get("door") if isinstance(mdev, dict) else None
        state = (
            DoorState.from_wire(door_val)
            if isinstance(door_val, int)
            else DoorState.UNKNOWN
        )

        snapshots.append(
            DeviceSnapshot(
                device=Device(
                    hub_id=hub_id, name=str(name), device_type=device_type
                ),
                state=state,
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Door control — toggle via /desire
# ---------------------------------------------------------------------------


def build_toggle_payload(
    hub_id: str,
    device_type: DeviceType,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the MQTT /desire payload to toggle a door.

    Matches MainActivity.deviceContral (MainActivity.java:1179-1183) exactly.
    """
    ts = make_timestamp(now)
    ctrlgdo: dict[str, Any] = {"cmd": GDO_TOGGLE_CMD, "ts": ts}
    position = device_type.position
    if position is not None:
        ctrlgdo["position"] = position
    return {
        "data": {
            "hub_id": hub_id,
            "desired": {"mdev": {"ctrlgdo": ctrlgdo}},
        }
    }


# ---------------------------------------------------------------------------
# State updates — /update/result push
# ---------------------------------------------------------------------------


def parse_state_update(payload: dict[str, Any]) -> tuple[str, DoorState] | None:
    """Parse an /update/result payload.

    Returns (hub_id, DoorState) or None if the payload doesn't carry a
    door-state update (e.g. firmware-only update, sensor add/remove ack).
    Raises OrbitProtocolError only on structurally invalid input.
    """
    if not isinstance(payload, dict):
        raise OrbitProtocolError(
            f"State update is not a JSON object: got {type(payload).__name__}"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    hub_id = data.get("hub_id")
    if not isinstance(hub_id, str) or not hub_id:
        return None

    reported = data.get("reported")
    if not isinstance(reported, dict):
        return None

    mdev = reported.get("mdev")
    if not isinstance(mdev, dict):
        return None

    door = mdev.get("door")
    if not isinstance(door, int):
        return None

    return hub_id, DoorState.from_wire(door)
