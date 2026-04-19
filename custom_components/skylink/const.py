"""HA-specific constants for the Skylink integration.

Protocol constants live in `_client/protocol.py`. Everything here is
HA-facing: the domain name, the platforms we register, and the
fallback discovery cadence.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "skylink"

PLATFORMS: list[Platform] = [Platform.COVER, Platform.BINARY_SENSOR]

# The coordinator's update_interval is a fallback tick — MQTT push is the
# primary state-update mechanism. The tick re-runs device discovery, so
# new hubs get picked up without a config reload.
DEFAULT_DISCOVERY_INTERVAL = timedelta(hours=6)
