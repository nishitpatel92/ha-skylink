"""Config flow for the Skylink integration.

Will implement: user step (email, password) → auth check via OrbitClient →
MQTT auto-discovery of hubs → create entry. Plus reauth and options flows.
"""
