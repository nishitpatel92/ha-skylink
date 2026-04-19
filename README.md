# ha-skylink

Home Assistant integration for Skylink G2 garage door openers (Orbit Home cloud).

**Status:** early work in progress — not yet installable via HACS.

## Layout

Monorepo containing the HA custom component and an internal protocol client.
The client lives under `custom_components/skylink/_client/` and has zero
`homeassistant` imports, so it can be extracted to its own PyPI package
(`skylink-orbit-client`) later without touching the protocol logic.

```
custom_components/skylink/
  __init__.py            HA entry point — thin
  config_flow.py         HA config flow
  coordinator.py         HA DataUpdateCoordinator
  cover.py               HA cover entity
  binary_sensor.py       HA binary sensor entity
  const.py               HA-specific constants
  manifest.json
  strings.json
  translations/en.json
  _client/               Pure Python — no HA imports
    domain.py            Door, Hub, DoorState enum, Command
    protocol.py          Signing, payload builders, response parsers (pure)
    http.py              aiohttp transport
    mqtt.py              paho-mqtt transport
    client.py            OrbitClient orchestrator
    errors.py            Exception hierarchy

tests/
  unit/                  Pure tests — no network, no HA
  integration/           Against ephemeral mosquitto (testcontainers)
```

## Credits

- Protocol reverse-engineered from the Orbit Home Android APK.
  Full audit notes live in `~/dev/skylink-review/` (not committed).
- HA integration shape informed by `nicholsbw77/skylink-orbit-ha`
  (MIT), but written from scratch — no code was forked or copied.

## License

MIT — see `LICENSE`.
