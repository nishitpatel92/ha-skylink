"""HTTP transport adapter (aiohttp).

Uses system-trust-store TLS verification (matches the Orbit Home app's
OkHttp behaviour — the app does NOT disable verification for HTTPS).
"""
