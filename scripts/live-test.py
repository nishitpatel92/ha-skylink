#!/usr/bin/env python3
"""End-to-end live test against the real Skylink cloud + your hardware.

Safe by default: runs auth + discover + initial-state read without
actuating the door. Pass --toggle to run a full open/close cycle
(confirmation prompt before each toggle).

Usage:
    ./scripts/live-test.py EMAIL [--toggle] [-v]

Credentials: reads $SKYLINK_PASSWORD from the environment; prompts
interactively if not set. Never accepts the password on argv.

Requires the repo's dev venv to be active (or accessible at .venv/)
because the client imports from custom_components/skylink/_client/.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from pathlib import Path

# Make `_client` importable without pulling homeassistant into the chain.
_CLIENT_ROOT = Path(__file__).parent.parent / "custom_components" / "skylink"
sys.path.insert(0, str(_CLIENT_ROOT))

from _client.client import OrbitClient  # noqa: E402
from _client.domain import Device, DoorState  # noqa: E402
from _client.errors import OrbitAuthError, OrbitConnectionError, OrbitError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live-test")

# Tunables — conservative defaults. A residential door opens in 10-15s;
# allowing 45s for the full motion to complete gives margin for slow
# doors, MQTT delays, and variable hardware.
INITIAL_PUSH_TIMEOUT = 15.0
DISCOVER_TIMEOUT = 10.0
STABILIZE_TIMEOUT = 45.0
STABILIZE_QUIET_SECONDS = 5.0  # "stable" == no state change for this long


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------


class StateTracker:
    """Records per-hub state history with timestamps.

    Also exposes asyncio Events so tests can await "any update" or
    "stabilized" without polling.
    """

    def __init__(self) -> None:
        self.history: dict[str, list[tuple[float, DoorState]]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._start = time.monotonic()

    def on_update(self, hub_id: str, state: DoorState) -> None:
        elapsed = time.monotonic() - self._start
        self.history.setdefault(hub_id, []).append((elapsed, state))
        log.info("  push  %s +%5.2fs  state=%s", hub_id, elapsed, state.name)
        evt = self._wakeups.get(hub_id)
        if evt is not None and not evt.is_set():
            evt.set()

    def current(self, hub_id: str) -> DoorState | None:
        h = self.history.get(hub_id)
        return h[-1][1] if h else None

    async def wait_for_any_update(self, hub_id: str, timeout: float) -> bool:
        """Return True when a new update lands within `timeout`."""
        evt = self._wakeups.setdefault(hub_id, asyncio.Event())
        evt.clear()
        try:
            await asyncio.wait_for(evt.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def wait_for_stable(
        self,
        hub_id: str,
        quiet_for: float,
        overall_timeout: float,
    ) -> DoorState | None:
        """Wait until `quiet_for` seconds pass without a new update.

        Returns the final state, or None on overall timeout.
        """
        deadline = asyncio.get_running_loop().time() + overall_timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            if not await self.wait_for_any_update(hub_id, min(quiet_for, remaining)):
                # No update in the quiet window — we're stable.
                return self.current(hub_id)
        return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def run_auth(client: OrbitClient, email: str, password: str) -> None:
    log.info("step 1  authenticating as %s", email)
    result = await client.authenticate(email, password)
    log.info("        -> acc_no=%s alias=%r", result.acc_no, result.alias_name)


async def run_connect(client: OrbitClient) -> None:
    log.info("step 2  connecting MQTT")
    await client.connect()
    log.info("        -> connected")


async def run_discover(client: OrbitClient) -> list[Device]:
    log.info("step 3  discovering devices (timeout=%.1fs)", DISCOVER_TIMEOUT)
    devices = await client.discover(timeout=DISCOVER_TIMEOUT)
    if not devices:
        log.error("        -> no devices discovered")
        return []
    for d in devices:
        log.info(
            "        -> %s  type=%s  name=%r",
            d.hub_id,
            d.device_type.value,
            d.name,
        )
    return devices


async def run_read_initial_state(tracker: StateTracker, devices: list[Device]) -> None:
    log.info("step 4  reading initial state (up to %.0fs)", INITIAL_PUSH_TIMEOUT)
    # Wait for at least one push per device, best-effort.
    tasks = [
        asyncio.create_task(tracker.wait_for_any_update(d.hub_id, INITIAL_PUSH_TIMEOUT))
        for d in devices
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    for d in devices:
        cur = tracker.current(d.hub_id)
        label = cur.name if cur is not None else "<no push received — door may be idle>"
        log.info("        %s  current=%s", d.hub_id, label)


def confirm(prompt: str) -> bool:
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


async def run_toggle_cycle(
    client: OrbitClient, target: Device, tracker: StateTracker
) -> bool:
    """Open→wait→close→wait cycle. Returns True if end state == start state."""
    log.warning("")
    log.warning("⚠  --toggle: this will actuate your garage door.")
    log.warning("   Make sure the path is clear and you can see the door.")
    log.warning("")

    start_state = tracker.current(target.hub_id)
    if start_state is None:
        log.warning("no initial state received — continuing without baseline")

    if not confirm(f"Send first toggle to {target.hub_id} ({target.name})?"):
        log.info("aborted by user before any toggle")
        return True  # nothing happened

    log.info("step 5a  toggle #1 -> waiting for state to stabilize")
    t0 = time.monotonic()
    await client.toggle(target.hub_id, target.device_type)
    first_final = await tracker.wait_for_stable(
        target.hub_id, STABILIZE_QUIET_SECONDS, STABILIZE_TIMEOUT
    )
    log.info("        stabilized at %s after %.1fs",
             first_final.name if first_final else "<timeout>", time.monotonic() - t0)

    if first_final is None:
        log.error("door did not reach a stable state — aborting")
        return False

    log.warning("")
    log.warning("door is now %s.", first_final.name)
    if not confirm("Send second toggle to reverse?"):
        log.warning("leaving door in state %s — will NOT attempt to reverse",
                    first_final.name)
        return False

    log.info("step 5b  toggle #2 -> waiting for state to stabilize")
    t1 = time.monotonic()
    await client.toggle(target.hub_id, target.device_type)
    second_final = await tracker.wait_for_stable(
        target.hub_id, STABILIZE_QUIET_SECONDS, STABILIZE_TIMEOUT
    )
    log.info("        stabilized at %s after %.1fs",
             second_final.name if second_final else "<timeout>", time.monotonic() - t1)

    if second_final is None:
        log.error("door did not reach a stable state after second toggle")
        return False

    log.info("")
    log.info("cycle complete. start=%s  mid=%s  end=%s",
             start_state.name if start_state else "?",
             first_final.name, second_final.name)
    if start_state is not None and second_final == start_state:
        log.info("✓ door returned to original state")
        return True
    log.warning("✗ door did NOT return to original state")
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    password = os.environ.get("SKYLINK_PASSWORD")
    if not password:
        password = getpass.getpass("Skylink password (no echo): ")

    client = OrbitClient()
    tracker = StateTracker()
    client.on_door_state(tracker.on_update)

    try:
        await run_auth(client, args.email, password)
        await run_connect(client)
        devices = await run_discover(client)
        if not devices:
            return 1
        await run_read_initial_state(tracker, devices)

        if not args.toggle:
            log.info("")
            log.info("dry run complete. re-run with --toggle to actuate the door.")
            return 0

        if len(devices) > 1 and args.hub is None:
            log.error("multiple devices found — pick one with --hub <hub_id>:")
            for d in devices:
                log.error("  %s  %s  %r", d.hub_id, d.device_type.value, d.name)
            return 2

        target = next(
            (d for d in devices if args.hub is None or d.hub_id == args.hub),
            None,
        )
        if target is None:
            log.error("--hub %s not in discovered devices", args.hub)
            return 2

        success = await run_toggle_cycle(client, target, tracker)
        return 0 if success else 1

    except OrbitAuthError as err:
        log.error("authentication failed: %s", err)
        return 3
    except OrbitConnectionError as err:
        log.error("connection failed: %s", err)
        return 4
    except OrbitError as err:
        log.exception("orbit client error: %s", err)
        return 5
    finally:
        log.info("disconnecting...")
        try:
            await client.disconnect()
        except Exception:
            log.exception("error during disconnect (non-fatal)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("email", help="Orbit Home account email")
    p.add_argument(
        "--toggle",
        action="store_true",
        help="Actually actuate the door (opt-in). Default is dry-run.",
    )
    p.add_argument(
        "--hub",
        default=None,
        help="Required when multiple hubs are discovered and --toggle is set.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Enable DEBUG logging"
    )
    return p.parse_args()


if __name__ == "__main__":
    a = parse_args()
    if a.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    try:
        sys.exit(asyncio.run(main(a)))
    except KeyboardInterrupt:
        log.warning("")
        log.warning("interrupted. if a toggle was in progress, verify the")
        log.warning("physical state of your door before re-running.")
        sys.exit(130)
