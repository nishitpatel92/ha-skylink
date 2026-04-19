"""Cover entity for a Skylink garage door.

Maps domain DoorState → HA cover state; dispatches open/close/stop to
OrbitClient.toggle() (hardware is toggle-only).
"""
