"""Orbit Home wire protocol — pure functions.

Signing, request-header construction, payload builders, response parsers.
Every function here should be unit-testable with fixtures. No IO.

Based on audit of Orbit Home Android APK 3.8.1 — see /tmp/skylink-review
for the decompiled source this was derived from.
"""
