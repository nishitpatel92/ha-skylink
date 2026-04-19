"""MQTT transport adapter (paho-mqtt).

TLS is enabled but certificate/hostname verification is disabled — matches
the app's behaviour for its hardcoded broker IP 34.214.223.70:1899.
Enables paho's automatic reconnect loop.
"""
