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

import os
import sys
from pathlib import Path

# Re-exec under the repo's venv Python if we weren't launched with it.
# aiohttp / aiomqtt live there, not in the system interpreter. We compare
# sys.prefix (which Python sets to the venv root when launched via the
# venv's python) rather than sys.executable — sys.executable resolves
# symlinks, so a venv python that links to the system python can't be
# distinguished that way.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENV_DIR = _REPO_ROOT / ".venv"
_VENV_PY = _VENV_DIR / "bin" / "python"
if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import argparse  # noqa: E402
import asyncio  # noqa: E402
import getpass  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402

# Make `_client` importable without pulling homeassistant into the chain.
_CLIENT_ROOT = _REPO_ROOT / "custom_components" / "skylink"
sys.path.insert(0, str(_CLIENT_ROOT))

from _client.client import OrbitClient  # noqa: E402
from _client.domain import Device, DeviceSnapshot, DoorState  # noqa: E402
from _client.errors import OrbitAuthError, OrbitConnectionError, OrbitError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live-test")

# Tunables — conservative defaults. A residential door opens in 10-15s;
# allowing 60s for full motion to complete gives margin for slow doors,
# pre-motion delays (lights, beeper), and variable hardware.
DISCOVER_TIMEOUT = 10.0
TOGGLE_RESPONSE_TIMEOUT = 15.0  # How long to wait for the door to start moving
STABILIZE_TIMEOUT = 60.0
STABILIZE_QUIET_SECONDS = 5.0  # Quiet window that counts as "settled"

# A door only counts as "stable" when it's in one of these states AND
# no pushes have arrived for STABILIZE_QUIET_SECONDS. OPENING / CLOSING
# / CLOSE_DELAY are transient: the door emits one push when motion
# begins, then is silent for 15-25s while physically moving, which is
# not the same as "stable".
_TERMINAL_STATES = frozenset(
    {DoorState.OPEN, DoorState.CLOSED, DoorState.OPEN_HALF, DoorState.OPEN_HALF_ALT}
)


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

    async def wait_for_change_from(
        self,
        hub_id: str,
        baseline: DoorState | None,
        timeout: float,
    ) -> DoorState | None:
        """Wait for the state to become something other than `baseline`.

        Returns the new state, or None if no different state arrives
        within `timeout`. Useful for "did the door respond to my toggle".
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            if not await self.wait_for_any_update(hub_id, remaining):
                return None
            current = self.current(hub_id)
            if current is not None and current != baseline:
                return current
        return None

    async def wait_for_stable(
        self,
        hub_id: str,
        quiet_for: float,
        overall_timeout: float,
    ) -> DoorState | None:
        """Wait for a terminal state held quiet for `quiet_for` seconds.

        Silence alone is not enough — residential doors go silent for
        15-25s during actual motion. We require the current state to be
        terminal (OPEN / CLOSED / half-open) before accepting.
        """
        deadline = asyncio.get_running_loop().time() + overall_timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            got_update = await self.wait_for_any_update(
                hub_id, min(quiet_for, remaining)
            )
            current = self.current(hub_id)
            if not got_update and current is not None and current in _TERMINAL_STATES:
                return current
            # Either an update arrived (reset quiet window) or we're still
            # mid-motion (current is transient). Keep waiting.
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


async def run_discover(client: OrbitClient) -> list[DeviceSnapshot]:
    log.info("step 3  discovering devices (timeout=%.1fs)", DISCOVER_TIMEOUT)
    snapshots = await client.discover(timeout=DISCOVER_TIMEOUT)
    if not snapshots:
        log.error("        -> no devices discovered")
        return []
    for s in snapshots:
        log.info(
            "        -> %s  type=%s  name=%r  state=%s",
            s.device.hub_id,
            s.device.device_type.value,
            s.device.name,
            s.state.name,
        )
    return snapshots


def seed_tracker(tracker: StateTracker, snapshots: list[DeviceSnapshot]) -> None:
    """Prime the tracker with each device's initial state from discovery.

    The /get/result response includes `reported.mdev.door` per device —
    same mechanism the APK uses to know the baseline. No need for a
    separate "wait for first push" step.
    """
    for s in snapshots:
        tracker.on_update(s.device.hub_id, s.state)


def confirm(prompt: str) -> bool:
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


async def _send_and_wait(
    client: OrbitClient,
    target: Device,
    tracker: StateTracker,
    label: str,
) -> DoorState | None:
    """Send one toggle; wait for the door to start moving then settle.

    Returns the terminal state reached, or None if the door never
    responded or never settled.
    """
    before = tracker.current(target.hub_id)
    before_label = before.name if before is not None else "?"
    log.info("        %s  sending toggle (pre-toggle state=%s)", label, before_label)

    t0 = time.monotonic()
    await client.toggle(target.hub_id, target.device_type)

    # Phase 1: wait for ANY change away from the pre-toggle state.
    # This catches "hardware ignored the toggle" cases early.
    transition = await tracker.wait_for_change_from(
        target.hub_id, before, TOGGLE_RESPONSE_TIMEOUT
    )
    if transition is None:
        log.warning(
            "        %s  no state change within %.0fs — hardware may not have "
            "responded to the toggle",
            label,
            TOGGLE_RESPONSE_TIMEOUT,
        )
        return None
    log.info(
        "        %s  transitioned to %s after %.1fs",
        label,
        transition.name,
        time.monotonic() - t0,
    )

    # Phase 2: wait for motion to finish (terminal state + quiet window).
    final = await tracker.wait_for_stable(
        target.hub_id, STABILIZE_QUIET_SECONDS, STABILIZE_TIMEOUT
    )
    if final is None:
        log.error(
            "        %s  door did not reach a terminal state within %.0fs",
            label,
            STABILIZE_TIMEOUT,
        )
        return None
    log.info(
        "        %s  settled at %s after %.1fs total",
        label,
        final.name,
        time.monotonic() - t0,
    )
    return final


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

    log.info("step 5a  toggle #1")
    first_final = await _send_and_wait(client, target, tracker, "#1")
    if first_final is None:
        return False

    log.warning("")
    log.warning("door is now %s.", first_final.name)
    if not confirm("Send second toggle to reverse?"):
        log.warning(
            "leaving door in state %s — will NOT attempt to reverse",
            first_final.name,
        )
        return False

    log.info("step 5b  toggle #2")
    second_final = await _send_and_wait(client, target, tracker, "#2")
    if second_final is None:
        return False

    log.info("")
    start_label = start_state.name if start_state is not None else "?"
    log.info(
        "cycle complete. start=%s  mid=%s  end=%s",
        start_label,
        first_final.name,
        second_final.name,
    )
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
        snapshots = await run_discover(client)
        if not snapshots:
            return 1
        seed_tracker(tracker, snapshots)

        if not args.toggle:
            log.info("")
            log.info("dry run complete. re-run with --toggle to actuate the door.")
            return 0

        if len(snapshots) > 1 and args.hub is None:
            log.error("multiple devices found — pick one with --hub <hub_id>:")
            for s in snapshots:
                log.error(
                    "  %s  %s  %r",
                    s.device.hub_id,
                    s.device.device_type.value,
                    s.device.name,
                )
            return 2

        target_snapshot = next(
            (s for s in snapshots if args.hub is None or s.device.hub_id == args.hub),
            None,
        )
        if target_snapshot is None:
            log.error("--hub %s not in discovered devices", args.hub)
            return 2

        success = await run_toggle_cycle(client, target_snapshot.device, tracker)
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
