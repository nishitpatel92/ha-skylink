"""DataUpdateCoordinator for the Skylink integration.

Subscribes to MQTT state pushes from OrbitClient and fans them out to
entities. No polling — the `update_interval` tick is only used as a
belt-and-braces reconnect nudge.
"""
