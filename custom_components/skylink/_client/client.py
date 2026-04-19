"""OrbitClient — the high-level orchestrator.

Depends on transport interfaces, not concrete transports. Exposes an
async API: authenticate(), discover(), subscribe(callback), toggle(hub_id).
HA coordinator is the only expected caller in the HA integration; the
client is usable standalone from a script or CLI.
"""
