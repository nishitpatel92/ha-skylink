# ha-skylink

Home Assistant integration for **Skylink G2** garage door openers via the Orbit Home cloud service. Exposes each door as a native HA cover entity and, through HA's built-in HomeKit bridge, as an Apple Home **Garage Door Opener** accessory.

## Features

- Works in Apple Home as a proper Garage Door Opener (via HA's HomeKit bridge — no extra config)
- Open / Close / Stop with HomeKit-safe command guards — no accidental "Hey Siri, open the garage" → door closes on an already-open door
- Real-time state pushed over MQTT — no polling, no hub-ID sniffing with mitmproxy
- Auto-discovery of every hub on your account, with current door state included in the discovery response
- Correct handling of toggle-only hardware (one MQTT publish per command)
- Reauth flow when the stored password stops working
- Supported device types: `GDO`, `NOVA_A`, `NOVA_B`, `NVMini`

## Status

Early, but fully functional against the author's real hardware. Verified end-to-end: authentication → MQTT discovery → toggle → state push. The integration's door-state mapping is derived from the Orbit Home Android APK and has been field-tested against a live GDO (firmware emits states `0` / `1` / `3` / `4`).

Not yet in HACS's default repository — install as a HACS custom repository for now (see below). No obstruction detection yet (the APK has an `errno` field we haven't wired up).

## Requirements

- Home Assistant **2024.1** or later
- A Skylink G2 opener with WiFi, provisioned via the Orbit Home app
- Your Orbit Home account email + password

## Installation

### Via HACS (recommended)

1. HACS → Integrations → ⋮ (top-right) → **Custom repositories**
2. Add `https://github.com/nishitpatel92/ha-skylink`, category **Integration**
3. Install **Skylink** and restart Home Assistant
4. **Settings** → **Devices & Services** → **Add Integration** → **Skylink**

### Manual

Copy or symlink `custom_components/skylink/` into your HA config directory's `custom_components/` folder, restart HA, then add the integration via the UI.

```sh
# From a clone of this repo
cp -r custom_components/skylink /path/to/your/ha-config/custom_components/
# …or symlink for live-edit dev:
ln -s "$PWD/custom_components/skylink" /path/to/your/ha-config/custom_components/skylink
```

## Configuration

Single step: your Orbit Home email and password. The integration authenticates, connects to the Skylink cloud MQTT broker, and auto-discovers every hub on your account. One HA device per hub is created, carrying:

- a **cover** entity (garage door — enabled by default)
- a **binary_sensor** entity (open/closed — **disabled by default**; see below)

If your password changes, HA will prompt a reauth flow automatically.

## Apple Home (HomeKit bridge)

No extra config — HA's built-in HomeKit bridge sees the cover's `device_class: garage` and `supported_features ≤ OPEN|CLOSE|STOP` and emits a proper `GarageDoorOpener` accessory (HomeKit Category 4).

| Skylink DoorState     | HA cover state | HomeKit `CurrentDoorState` |
| --------------------- | -------------- | -------------------------- |
| `OPEN` / `OPEN_HALF`  | `open`         | `0` Open                   |
| `CLOSED`              | `closed`       | `1` Closed                 |
| `OPENING`             | `opening`      | `2` Opening                |
| `CLOSING` / `CLOSE_DELAY` | `closing`  | `3` Closing                |
| `UNKNOWN`             | `unknown`      | _(no update)_              |

**Why is the binary_sensor disabled by default?** HA's HomeKit bridge maps `BinarySensorDeviceClass.GARAGE_DOOR` to a HomeKit `ContactSensor` service. If both entities were enabled, Apple Home would show a duplicate "contact sensor" accessory right next to the Garage Door Opener. Default-disabled means the bridge skips it; users who want it for HA automations can enable it manually from the entity registry.

## Architecture

Monorepo with a clean split between protocol and HA glue:

- **`custom_components/skylink/_client/`** — pure Python protocol client. Zero `homeassistant.*` imports. Usable standalone from a script or CLI (see `scripts/live-test.py`). Designed to be extractable as its own PyPI package (`skylink-orbit-client`) later without touching any protocol logic.
- **`custom_components/skylink/`** — thin HA adapters (cover, binary_sensor, config_flow, coordinator) that translate between HA primitives and the client's types.

```
custom_components/skylink/
  __init__.py        HA entry point
  manifest.json      HACS / HA metadata
  const.py           HA-side constants only
  config_flow.py     User + reauth flows
  coordinator.py     DataUpdateCoordinator wiring MQTT pushes
  cover.py           GarageDoor cover entity
  binary_sensor.py   Open/closed sensor (default-disabled)
  strings.json / translations/en.json
  _client/
    domain.py        DoorState enum, Device, DeviceSnapshot
    protocol.py      Signing, payload builders, response parsers
    http.py          aiohttp transport
    mqtt.py          aiomqtt transport
    client.py        OrbitClient orchestrator
    errors.py        Exception hierarchy
```

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the wire-level details of the Orbit Home protocol.

## Development

```sh
uv venv && uv pip install -e ".[dev]"

# Fast feedback — no docker, no network
.venv/bin/pytest tests/unit

# Full integration suite — spins up a throwaway mosquitto
./scripts/test-integration.sh                    # tears down broker after
KEEP_BROKER=1 ./scripts/test-integration.sh      # reuse broker across runs

# Against your real hardware (safe-by-default dry-run)
./scripts/live-test.py your@email.com            # auth + discover + state
./scripts/live-test.py your@email.com --toggle   # full open / close cycle

# Lint + types
.venv/bin/ruff check . && .venv/bin/mypy
```

The **live-test** script is your fastest way to verify the client works against your hardware before touching your HA instance. It's safe by default — a plain invocation only reads state; `--toggle` is opt-in and prompts for `y/N` before each motion.

## Credits

- Protocol reverse-engineered from the Orbit Home Android APK v3.8.1. See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the audit trail and wire-format details.
- HA integration shape informed by [`nicholsbw77/skylink-orbit-ha`](https://github.com/nicholsbw77/skylink-orbit-ha) (MIT), but written from scratch — no code was forked or copied.

## License

MIT — see [`LICENSE`](LICENSE).
