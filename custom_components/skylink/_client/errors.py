"""Exception hierarchy for the Orbit client.

Protocol-level errors only. HA adapters translate these into HA-specific
exceptions (ConfigEntryAuthFailed, UpdateFailed, HomeAssistantError).
"""

from __future__ import annotations


class OrbitError(Exception):
    """Base class for all client errors."""


class OrbitAuthError(OrbitError):
    """Login rejected by the server, or an in-flight signature was invalid."""


class OrbitConnectionError(OrbitError):
    """Could not reach the Orbit cloud (network, DNS, timeout, TLS)."""


class OrbitProtocolError(OrbitError):
    """Server returned a response that doesn't match what we expect.

    Covers malformed JSON, missing fields, unexpected result codes, and
    anything else that suggests the protocol has drifted.
    """
