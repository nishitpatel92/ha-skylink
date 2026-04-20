# Skylink / Orbit Home wire protocol

Everything the integration talks to the Skylink cloud over. All of it was reverse-engineered from the official Android app, cross-checked against live traffic.

## Source

- **APK:** `com.mtqqdemo.skylink` (Orbit Home), version **3.8.1**, build 65.
- **Method:** `jadx` decompilation + `strings` search for hardcoded literals, pinning each claim below to a specific line in the decompiled Java source.
- **Anchor files in the APK:**
  - `com.mtqqdemo.skylink.net.HeadInterceptor` — request-signing interceptor for OkHttp
  - `com.mtqqdemo.skylink.MainActivity` — MQTT broker init, topic subscriptions, control payload builders
  - `com.mtqqdemo.skylink.home.DeviceDetailPageAdapter` — door-state integer → UI icon switch (authoritative state mapping)
  - `com.mtqqdemo.skylink.BuildConfig` — REST base URL
  - `com.mtqqdemo.skylink.manager.SkyLinkDevice` — response data model (Gson-annotated)

## REST transport

Used only for authentication today; the APK calls many more commands that this integration doesn't need.

- **Base URL:** `https://iot.skyhm.net:8444/skylinkhub_crm/skyhm_api_s.jsp`
  - from `BuildConfig.SERVER_URL`
- **Method:** always `POST`, with `?cmd=<command>` query param
- **Commands used by this integration:** `act_login`
- **Commands the APK supports but this integration ignores:** `act_new`, `act_new_verify`, `act_resend`, `act_chg_pwd`, `act_reset_pwd`, `act_del`, `act_del_pn_token`, `act_hub_grant`, `get_app_version`, `get_last_firmware`, `hub_add`, `hub_del`, `hub_add_pntf`, `hub_del_pntf`, `hub_add_sup`, `hub_del_sup`, `hub_event_log`, `hub_import`, `cam_add`, `cam_del`, `cam_chg_name`, `cam_chg_pwd`, `set_alexa_pin`, `set_aog_pin`

### Custom headers (per `HeadInterceptor.java:37`)

| Header          | Value                                              |
| --------------- | -------------------------------------------------- |
| `REQ-CMD`       | the command name (e.g. `act_login`)                |
| `REQ-DATA`      | a context string — for login, the user's email     |
| `REQ-TIMESTAMP` | epoch milliseconds, whole-second precision (trailing `000`) |
| `REQ-SIGNATURE` | `md5(ts + "+" + cmd + "+" + req_data + SECRET).lower()` |

where `SECRET = "+8uHDSF77ueRmLlKkl67"` (literal string baked into the APK).

The APK rounds the timestamp to whole seconds via a `SimpleDateFormat` round-trip with GMT+08; we replicate this in `protocol.make_timestamp()` to keep timestamps byte-identical to the app's.

### Login request / response

- **Request body (`act_login`):**

  ```json
  {"app_sys": "apns", "username": "<email>", "password": "<password>", "app_brand": "00"}
  ```

- **Response (success):**

  ```json
  {"result": "00", "acc_no": "<numeric account id>", "alias_name": "<display name>"}
  ```

  The server emits `"result":00` as a **bare number with a leading zero**, which is technically invalid JSON. The APK patches it with `.replaceAll("\"result\":00", "\"result\":0")` in `RetrofitUtil.java`; we apply an equivalent regex fix-up in `protocol.parse_response_body()`.

### TLS

The APK's `RetrofitUtil.getOkHttpClient()` installs **no custom `SSLSocketFactory`**, so the REST client uses OkHttp's default system-trust verification. The server at `iot.skyhm.net` has a valid public certificate. We do the same — `OrbitHttp` uses the system trust store.

## MQTT transport

- **Broker:** `ssl://34.214.223.70:1899` — from `MainActivity.java:912`. Hardcoded IPv4 address; if Skylink migrates, the integration breaks the same day the official app does.
- **Auth:** username = `acc_no` (numeric ID from login response), password = the Orbit Home account password (`MainActivity.java:920-921`).
- **Keepalive:** 30 seconds; connect timeout 180 seconds.
- **Client library:** the APK uses Paho-Java; we use `aiomqtt` (async wrapper over Paho-Python), which gives us equivalent behaviour without the threading bridge.

### TLS posture

The APK installs a `TrustManager` with empty `checkClientTrusted` / `checkServerTrusted` methods and returns `new X509Certificate[0]` from `getAcceptedIssuers` (`MainActivity.java:1011-1028`), plus a `HostnameVerifier` that short-circuits to `true` for `34.214.223.70` (`MainActivity.java:1031-1041`). Net effect: **TLS is enabled but certificate / hostname verification is disabled**.

The broker does not present a cert that validates against the public trust store, so we do the same in `_client/mqtt.py`. This is the _only_ place in the integration where TLS verification is relaxed — REST continues to use the system trust store.

### Topic pattern

All topics follow `skylink/things/client/<acc_no>/<suffix>` (`MainActivity.java:208-211`):

| Suffix           | Direction   | Purpose                                 |
| ---------------- | ----------- | --------------------------------------- |
| `/get`           | publish     | Request the device list                 |
| `/get/result`    | subscribe   | Receive the device list (with states!)  |
| `/desire`        | publish     | Send a control command                  |
| `/update/result` | subscribe   | Receive asynchronous state pushes       |

On connect, the client subscribes to `/get/result` and `/update/result`, then publishes `{}` to `/get` to populate its device cache.

### Discovery

Request payload on `/get`:

```json
{}
```

Response payload on `/get/result`:

```json
{
  "data": [
    {
      "hub_id": "rA8qM4QS",
      "type": "GDO",
      "name": "Main Garage",
      "reported": {
        "mdev": {
          "door": 4,
          "rssi": -53,
          "autoclose_en": 0,
          "errno": 0,
          "ssid": "...",
          "firstdoor": {"door": 4},
          "fw": {"type": "1", "ver": "...", "ver_gdo": "..."}
        }
      }
    }
  ]
}
```

**Key insight:** the `reported.mdev.door` integer on each entry is the hub's current door state. The APK never queries door state separately — the discovery response _is_ the initial state. Our `parse_discover_response` returns a `DeviceSnapshot` (Device + DoorState) per entry, so the coordinator and live-test script have a baseline immediately without waiting for an `/update/result` push.

Supported `type` values: `GDO`, `NOVA_A`, `NOVA_B`, `NVMini`. Entries with any other `type` are skipped (not raised on) so a future device type doesn't break discovery.

### Control payload

Topic: `/desire`. Payload shape (`MainActivity.java:1179-1183`):

```json
{"data": {"hub_id": "...", "desired": {"mdev": {"ctrlgdo": {"cmd": 0, "ts": "<epoch-ms>"}}}}}
```

With an additional `"position": "A"` or `"position": "B"` field for NOVA / NVMini devices:

```json
{"data": {"hub_id": "...", "desired": {"mdev": {"ctrlgdo": {"cmd": 0, "ts": "<epoch-ms>", "position": "A"}}}}}
```

**`cmd` value semantics (`MainActivity.contralDoor`):**

| `cmd` | Meaning                                               |
| ----- | ----------------------------------------------------- |
| `0`   | **Toggle door** — the only command used by this integration |
| `3`   | Light on (not implemented)                            |
| `4`   | Light off (not implemented)                           |
| `8`   | Add external door sensor (not implemented)            |
| `15`  | Remove external door sensor (not implemented)         |

### State updates

Topic: `/update/result`. Payload:

```json
{"data": {"hub_id": "...", "reported": {"mdev": {"door": <int>, "rssi": -50, ...}}}}
```

Only the `hub_id` and `reported.mdev.door` fields are meaningful to this integration today. Entries without a `door` int (e.g. firmware upgrade progress pushes) are silently ignored by `parse_state_update`.

### Door state integer mapping

Derived from `DeviceDetailPageAdapter.setDoorStatus` (`DeviceDetailPageAdapter.java:172-197`), where a `switch` on the `door` int selects which icon to display:

| int | `DoorState` (our enum) | APK icon resource                     | Meaning                        |
| --- | ---------------------- | ------------------------------------- | ------------------------------ |
| `0` | `OPENING`              | `opener_opening` (animated GIF)       | Motion started upward          |
| `1` | `OPEN`                 | `garage_door_opener_opened`           | Fully open, idle               |
| `2` | `OPEN_HALF`            | `garage_door_opener_open_half`        | Partially open                 |
| `3` | `CLOSING`              | `garage_door_opener_closing` (GIF)    | Motion started downward        |
| `4` | `CLOSED`               | `garage_door_opener_closed`           | Fully closed, idle             |
| `5` | `CLOSE_DELAY`          | `garage_door_opener_closedelay`       | Warning period before closing  |
| `6` | `OPEN_HALF_ALT`        | `garage_door_opener_open_half`        | Partially open (alt encoding)  |
| `7` | `UNKNOWN`              | `garage_door_opener_unknow`           | APK explicitly labels "unknow" |

Field-tested against the author's GDO firmware, which uses only `0`/`1`/`3`/`4` in practice. The ~10-second cloud-close warning period appears as repeated `OPEN` (`1`) pushes rather than a `CLOSE_DELAY` (`5`); other firmwares may differ.

Values outside `0–7` are mapped to `DoorState.UNKNOWN` in `DoorState.from_wire()` rather than raising, so a future firmware that adds a new state code doesn't crash the integration.

## Notes on fragility

- **Hardcoded MQTT IP** (`34.214.223.70`) — if Skylink migrates their broker, the integration stops working the same day the app does.
- **Hardcoded signing secret** baked into every APK release — if Skylink rotates it, every old APK version and this integration break together.
- **No protocol versioning** in any response, so a silent wire-format change would land as mysterious parse errors.
- **Cloud dependency** — everything goes through Skylink's cloud. A Skylink outage or account suspension takes this integration offline. Same failure surface as the official app; inherent to the product, not this integration.
