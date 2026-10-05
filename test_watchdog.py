"""The watchdog must restart a bot that is running but not connected.

This is the failure that actually happened: the socket died when the laptop
slept, the client reconnected in a loop forever, the process never exited, and
launchd's KeepAlive never fired. Everything looked healthy and Slack got nothing.

Run: python test_watchdog.py
"""

import logging
import os as _real_os
import time

import watchdog

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        failures.append(name)


# Run the real logic on a compressed clock so the test finishes in seconds.
watchdog.GRACE_SECONDS = 0
watchdog.CHECK_INTERVAL_SECONDS = 0.02
watchdog.FAILURES_BEFORE_RESTART = 3

exits = []


class _Exited(BaseException):
    """Stands in for os._exit: records the code and stops that thread dead."""


def _fake_exit(code):
    exits.append(code)
    raise _Exited


class _FakeOS:
    """Real os in every respect except _exit, which the test has to survive."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    _exit = staticmethod(_fake_exit)


watchdog.os = _FakeOS(_real_os)
logging.getLogger("watchdog").setLevel(logging.CRITICAL)


def run_until(states, timeout=3.0):
    """Drive the watchdog through a fixed sequence of connection readings."""
    exits.clear()
    remaining = list(states)
    seen = []

    def is_connected():
        value = remaining.pop(0) if remaining else states[-1]
        seen.append(value)
        return value

    watchdog.start(is_connected)
    deadline = time.time() + timeout
    while time.time() < deadline and not exits and remaining:
        time.sleep(0.01)
    time.sleep(0.15)
    return seen


# --- a healthy bot is left alone ----------------------------------------
run_until([True] * 30)
check("a connected bot is never restarted", not exits)

# --- a brief blip is tolerated ------------------------------------------
run_until([True, True, False, False, True, True, True, True, True, True])
check("a short disconnect does not trigger a restart", not exits,
      "reconnects take seconds; restarting on the first miss would thrash")

# --- a wedged socket is restarted ---------------------------------------
run_until([False] * 20)
check("a persistently disconnected bot exits", exits == [1], str(exits))
check("it exits non-zero so the service manager restarts it", exits[:1] == [1])

# --- the failure counter resets on recovery -----------------------------
# Two failures, a recovery, then two more must NOT add up to the threshold.
run_until([False, False, True, False, False, True, True, True, True, True])
check("recovery resets the failure count", not exits)

# --- a broken health check counts as disconnected ------------------------
exits.clear()


def raises():
    raise RuntimeError("cannot read socket state")


watchdog.start(raises)
time.sleep(0.3)
check("an unreadable connection state counts against the bot", exits == [1], str(exits))

# --- the shutdown hook runs before exiting -------------------------------
exits.clear()
hook_ran = []
watchdog.start(lambda: False, on_give_up=lambda: hook_ran.append(True))
time.sleep(0.3)
check("the shutdown hook runs", hook_ran == [True])
check("it still exits after the hook", exits == [1])

# --- a failing hook must not prevent the restart -------------------------
exits.clear()


def bad_hook():
    raise RuntimeError("shutdown failed")


watchdog.start(lambda: False, on_give_up=bad_hook)
time.sleep(0.3)
check("a failing shutdown hook still exits", exits == [1], str(exits))

# --- the heartbeat measures downtime on the way back up ------------------
# A stopped process cannot raise an alarm, so the gap is measured at startup.
import tempfile, time as _time  # noqa: E402
_os = _real_os

watchdog.HEARTBEAT_PATH = _os.path.join(tempfile.mkdtemp(), "heartbeat")

check("first ever run reports no downtime", watchdog.downtime_seconds() is None)

with open(watchdog.HEARTBEAT_PATH, "w") as f:
    f.write(str(_time.time() - 3600))
gap = watchdog.downtime_seconds()
check("an hour gap is measured", 3590 < gap < 3610, f"{gap:.0f}s")
check("an hour counts as an outage", gap > watchdog.OUTAGE_THRESHOLD_SECONDS)

with open(watchdog.HEARTBEAT_PATH, "w") as f:
    f.write(str(_time.time() - 5))
check("a quick restart is not an outage",
      watchdog.downtime_seconds() < watchdog.OUTAGE_THRESHOLD_SECONDS)

# A kill mid-write must not leave a timestamp that reads as decades of downtime.
with open(watchdog.HEARTBEAT_PATH, "w") as f:
    f.write("")
check("a truncated heartbeat is ignored", watchdog.downtime_seconds() is None)
with open(watchdog.HEARTBEAT_PATH, "w") as f:
    f.write("not-a-number")
check("a corrupt heartbeat is ignored", watchdog.downtime_seconds() is None)

watchdog.start_heartbeat(interval_seconds=0.02)
_time.sleep(0.2)
check("the heartbeat is written", watchdog.last_seen() is not None)
check("the heartbeat is current", _time.time() - watchdog.last_seen() < 2)
check("no temp file is left behind", not _os.path.exists(watchdog.HEARTBEAT_PATH + ".tmp"))

check("downtime reads in plain words", watchdog.describe(45) == "45 seconds")
check("minutes are rounded sensibly", watchdog.describe(3000) == "50 minutes")
check("long outages read as hours", watchdog.describe(7200) == "2.0 hours")

# --- the real defaults are sane -----------------------------------------
import importlib  # noqa: E402

fresh = importlib.reload(watchdog)
window = fresh.CHECK_INTERVAL_SECONDS * fresh.FAILURES_BEFORE_RESTART
check("waits long enough to rule out a normal reconnect", window >= 60, f"{window}s")
check("does not wait so long that an outage goes unnoticed", window <= 300, f"{window}s")
check("gives the first connection a grace period", fresh.GRACE_SECONDS >= 30)
print(f"      restarts after ~{window:.0f}s disconnected, "
      f"{fresh.GRACE_SECONDS}s grace at startup")

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
raise SystemExit(1 if failures else 0)
