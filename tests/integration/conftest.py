"""Integration-test fixtures.

`mosquitto_broker` spins up an `eclipse-mosquitto:2` container via
testcontainers-python for the test session, yields (host, port), tears
it down after.
"""
