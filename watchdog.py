"""Detect a bot that is running but no longer reaching Slack.

Socket Mode can fail in a way that leaves the process perfectly healthy from the
outside: the websocket dies — a laptop sleeping is enough — and the client
reconnects forever, hitting the same broken pipe each time. The process never
exits, so launchd's KeepAlive never fires, and every check anyone would think to
run says the bot is fine. Meanwhile Slack gets nothing and `/new-project`
answers "the app did not respond".

This watches the one thing that actually matters — whether the socket is
connected — and, once it has been down long enough to rule out an ordinary
reconnect, exits the process so launchd starts a clean one.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# A reconnect after a dropped connection normally takes seconds. Waiting for
# several consecutive failures avoids restarting the bot over ordinary network
# blips, at the cost of a slightly longer outage in the case that is genuinely
# stuck.
CHECK_INTERVAL_SECONDS = 20
FAILURES_BEFORE_RESTART = 6  # ~2 minutes disconnected

# Give the handler time to make its first connection before judging it.
GRACE_SECONDS = 60


HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "heartbeat"
)

# Restarts take seconds. Only a gap longer than this is an outage worth telling
# anyone about.
OUTAGE_THRESHOLD_SECONDS = 300


def last_seen() -> float | None:
    """When the bot last knew it was alive, as a unix timestamp."""
    try:
        with open(HEARTBEAT_PATH) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def downtime_seconds() -> float | None:
    """How long the bot was gone, or None if this is its first run.

    A process that has stopped cannot report anything, so the gap is measured on
    the way back up: the heartbeat file says when it was last alive, and the
    difference is how long Slack had nobody answering.
    """
    previous = last_seen()
    if previous is None:
        return None
    return max(0.0, time.time() - previous)


def start_heartbeat(interval_seconds: int = 60) -> threading.Thread:
    """Record that the bot is alive, so the next start can measure the gap."""

    def run() -> None:
        while True:
            try:
                os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
                # Written via a temporary file so a kill mid-write cannot leave
                # a truncated timestamp that reads as a decades-long outage.
                temporary = HEARTBEAT_PATH + ".tmp"
                with open(temporary, "w") as f:
                    f.write(str(time.time()))
                os.replace(temporary, HEARTBEAT_PATH)
            except OSError:
                logger.warning("Could not write the heartbeat", exc_info=True)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=run, name="heartbeat", daemon=True)
    thread.start()
    return thread


def describe(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def start(is_connected: Callable[[], bool], on_give_up: Callable[[], None] | None = None) -> threading.Thread:
    """Run the check in the background. Returns the thread, already started."""

    def run() -> None:
        logger.info(
            "Watchdog active — restarting if the socket is down for ~%ds",
            CHECK_INTERVAL_SECONDS * FAILURES_BEFORE_RESTART,
        )
        time.sleep(GRACE_SECONDS)
        consecutive = 0
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            try:
                connected = bool(is_connected())
            except Exception:
                logger.warning("Could not read the connection state", exc_info=True)
                connected = False

            if connected:
                if consecutive:
                    logger.info("Socket reconnected after %d failed check(s)", consecutive)
                consecutive = 0
                continue

            consecutive += 1
            logger.warning(
                "Socket not connected (%d/%d checks)", consecutive, FAILURES_BEFORE_RESTART
            )
            if consecutive >= FAILURES_BEFORE_RESTART:
                logger.error(
                    "Socket has been down for ~%ds and is not recovering. Exiting so "
                    "the service manager starts a clean process.",
                    consecutive * CHECK_INTERVAL_SECONDS,
                )
                if on_give_up:
                    try:
                        on_give_up()
                    except Exception:
                        logger.exception("Shutdown hook failed; exiting anyway")
                # os._exit skips atexit and any thread still holding a lock — the
                # point is to die now and be restarted, not to unwind politely
                # from a state we already know is broken.
                os._exit(1)

    thread = threading.Thread(target=run, name="watchdog", daemon=True)
    thread.start()
    return thread
