"""Tests for the pure protocol functions."""

from __future__ import annotations

import pytest

from custom_components.skylink._client import protocol
from custom_components.skylink._client.domain import Device, DeviceType, DoorState
from custom_components.skylink._client.errors import OrbitProtocolError


class TestSign:
    def test_known_vector(self) -> None:
        # Precomputed: MD5("1700000000000+act_login+test@example.com+8uHDSF77ueRmLlKkl67")
        assert (
            protocol.sign("1700000000000", "act_login", "test@example.com")
            == "565434db744744435f29d1fa157e3cdf"
        )

    def test_is_lowercase_hex(self) -> None:
        sig = protocol.sign("1700000000000", "act_login", "someone@example.com")
        assert sig == sig.lower()
        assert len(sig) == 32


class TestTimestamp:
    def test_is_whole_second(self) -> None:
        # All APK-produced timestamps end in "000" (second-precision round-trip).
        ts = protocol.make_timestamp(now=1700000000.834)
        assert ts.endswith("000")
        assert ts == "1700000000000"

    def test_is_string(self) -> None:
        assert isinstance(protocol.make_timestamp(now=0), str)


class TestHeaders:
    def test_contains_all_required_keys(self) -> None:
        h = protocol.build_request_headers("act_login", "x@y.z", now=1700000000.0)
        for key in ("REQ-CMD", "REQ-DATA", "REQ-TIMESTAMP", "REQ-SIGNATURE", "User-Agent"):
            assert key in h

    def test_signature_matches_timestamp_and_cmd(self) -> None:
        h = protocol.build_request_headers("act_login", "test@example.com", now=1700000000.0)
        assert h["REQ-TIMESTAMP"] == "1700000000000"
        assert h["REQ-SIGNATURE"] == "565434db744744435f29d1fa157e3cdf"


class TestParseResponseBody:
    def test_fixes_bare_leading_zero_result(self) -> None:
        raw = '{"result":00,"message":"Success"}'
        data = protocol.parse_response_body(raw, "act_login")
        assert data["result"] == "00"
        assert data["message"] == "Success"

    def test_strips_bom(self) -> None:
        raw = '\ufeff{"result":"0"}'
        assert protocol.parse_response_body(raw, "x")["result"] == "0"

    def test_rejects_empty(self) -> None:
        with pytest.raises(OrbitProtocolError, match="Empty"):
            protocol.parse_response_body("", "x")

    def test_rejects_non_object(self) -> None:
        with pytest.raises(OrbitProtocolError, match="Expected JSON object"):
            protocol.parse_response_body("[1, 2, 3]", "x")

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(OrbitProtocolError, match="non-JSON"):
            protocol.parse_response_body("not json at all", "x")


class TestCheckSuccess:
    @pytest.mark.parametrize("result", ["0", "00", 0])
    def test_accepts_success(self, result: str | int) -> None:
        protocol.check_success({"result": result}, "x")

    def test_rejects_missing_result(self) -> None:
        with pytest.raises(OrbitProtocolError, match="Missing 'result'"):
            protocol.check_success({}, "x")

    def test_rejects_error_code(self) -> None:
        with pytest.raises(OrbitProtocolError, match="result=25"):
            protocol.check_success({"result": "25", "message": "bad sig"}, "act_login")


class TestLogin:
    def test_body_shape(self) -> None:
        body = protocol.build_login_body("user@example.com", "hunter2")
        assert body == {
            "app_sys": "apns",
            "username": "user@example.com",
            "password": "hunter2",
            "app_brand": "00",
        }

    def test_parse_response_extracts_acc_no_and_alias(self) -> None:
        result = protocol.parse_login_response(
            {"result": "00", "acc_no": "8003105701", "alias_name": "Brad"}
        )
        assert result.acc_no == "8003105701"
        assert result.alias_name == "Brad"

    def test_parse_response_missing_acc_no_raises(self) -> None:
        with pytest.raises(OrbitProtocolError, match="missing 'acc_no'"):
            protocol.parse_login_response({"result": "00"})


class TestMqttTopic:
    def test_composition(self) -> None:
        assert (
            protocol.mqtt_topic("8003105701", protocol.TOPIC_SUFFIX_DESIRE)
            == "skylink/things/client/8003105701/desire"
        )

    def test_empty_acc_no_raises(self) -> None:
        with pytest.raises(OrbitProtocolError):
            protocol.mqtt_topic("", "desire")


class TestDiscoverPayload:
    def test_build_is_empty_dict(self) -> None:
        assert protocol.build_discover_payload() == {}

    def test_parse_single_gdo(self) -> None:
        payload = {
            "data": [
                {"hub_id": "rA8qM4QS", "type": "GDO", "name": "Main Garage"},
            ]
        }
        devices = protocol.parse_discover_response(payload)
        assert devices == [
            Device(hub_id="rA8qM4QS", name="Main Garage", device_type=DeviceType.GDO),
        ]

    def test_parse_multiple_mixed_types(self) -> None:
        payload = {
            "data": [
                {"hub_id": "aaa", "type": "GDO"},
                {"hub_id": "bbb", "type": "NOVA_A"},
                {"hub_id": "ccc", "type": "NVMini", "name": "Barn"},
            ]
        }
        devices = protocol.parse_discover_response(payload)
        assert {d.hub_id for d in devices} == {"aaa", "bbb", "ccc"}
        assert {d.device_type for d in devices} == {
            DeviceType.GDO,
            DeviceType.NOVA_A,
            DeviceType.NV_MINI,
        }

    def test_parse_skips_unknown_types(self) -> None:
        payload = {"data": [{"hub_id": "x", "type": "FUTURE_TYPE"}]}
        assert protocol.parse_discover_response(payload) == []

    def test_parse_skips_entries_without_hub_id(self) -> None:
        payload = {"data": [{"type": "GDO"}, {"hub_id": "", "type": "GDO"}]}
        assert protocol.parse_discover_response(payload) == []

    def test_parse_uses_fallback_name_when_missing(self) -> None:
        devices = protocol.parse_discover_response({"data": [{"hub_id": "ABC", "type": "GDO"}]})
        assert devices[0].name == "Skylink ABC"

    def test_parse_rejects_non_list_data(self) -> None:
        with pytest.raises(OrbitProtocolError, match="missing 'data' list"):
            protocol.parse_discover_response({"data": "nope"})


class TestTogglePayload:
    def test_gdo_has_no_position(self) -> None:
        p = protocol.build_toggle_payload("rA8qM4QS", DeviceType.GDO, now=1700000000.0)
        assert p == {
            "data": {
                "hub_id": "rA8qM4QS",
                "desired": {"mdev": {"ctrlgdo": {"cmd": 0, "ts": "1700000000000"}}},
            }
        }

    def test_nova_a_includes_position(self) -> None:
        p = protocol.build_toggle_payload("hub", DeviceType.NOVA_A, now=1700000000.0)
        ctrl = p["data"]["desired"]["mdev"]["ctrlgdo"]
        assert ctrl["position"] == "A"
        assert ctrl["cmd"] == 0

    def test_nova_b_includes_position(self) -> None:
        p = protocol.build_toggle_payload("hub", DeviceType.NOVA_B, now=1700000000.0)
        assert p["data"]["desired"]["mdev"]["ctrlgdo"]["position"] == "B"


class TestParseStateUpdate:
    def test_parses_door_closed(self) -> None:
        # From FINDINGS.md: door=4 means closed in the APK's switch.
        payload = {
            "data": {
                "hub_id": "rA8qM4QS",
                "reported": {"mdev": {"door": 4, "rssi": -53}},
            }
        }
        assert protocol.parse_state_update(payload) == ("rA8qM4QS", DoorState.CLOSED)

    def test_parses_door_opening(self) -> None:
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 0}}}}
        assert protocol.parse_state_update(payload) == ("h", DoorState.OPENING)

    def test_unknown_door_value_maps_to_unknown(self) -> None:
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"door": 99}}}}
        assert protocol.parse_state_update(payload) == ("h", DoorState.UNKNOWN)

    def test_returns_none_when_no_door_field(self) -> None:
        # Firmware-only update, e.g. `{"mdev": {"fw": {...}}}` — ignore.
        payload = {"data": {"hub_id": "h", "reported": {"mdev": {"rssi": -50}}}}
        assert protocol.parse_state_update(payload) is None

    def test_returns_none_when_no_hub_id(self) -> None:
        payload = {"data": {"reported": {"mdev": {"door": 4}}}}
        assert protocol.parse_state_update(payload) is None

    def test_returns_none_for_malformed_nested_shape(self) -> None:
        # A "reported" that isn't a dict — don't blow up, just ignore.
        assert protocol.parse_state_update({"data": {"hub_id": "h", "reported": "nope"}}) is None

    def test_rejects_non_dict_top_level(self) -> None:
        with pytest.raises(OrbitProtocolError):
            protocol.parse_state_update("not a dict")  # type: ignore[arg-type]
