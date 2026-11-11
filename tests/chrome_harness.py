"""One wall-clock limit for every headless Chrome --dump-dom / --virtual-time-budget run.

Chrome's virtual-time budget does not advance while a load stays open, so --dump-dom can sit forever.
run_chrome starts Chrome in its own session and kills that process group after 20 seconds. A timeout
raises SkipTest "headless Chrome hung after 20s". Empty stdout raises SkipTest "Chrome produced no
render". Neither result is a pass. GS_REQUIRE_CHROME=1 turns those skips into failures, which
tools/release-check.sh sets when Chrome is on PATH.
"""
import os
import shutil
import signal
import subprocess
import unittest

TIMEOUT_S = 20
HUNG = "headless Chrome hung after 20s"
EMPTY = "Chrome produced no render"


def find_chrome():
    """Path of a Chrome that exits after --dump-dom, or None.

    google-chrome-stable is preferred. A google-chrome earlier on PATH is sometimes a wrapper that
    holds a debugging port open and never exits, which is the hang this helper bounds.
    """
    for name in ("google-chrome-stable", "chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _give_up(reason):
    if os.environ.get("GS_REQUIRE_CHROME") == "1":
        raise AssertionError(reason)
    raise unittest.SkipTest(reason)


def _kill_group(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def run_chrome(argv, timeout=TIMEOUT_S):
    """Run argv and return stdout text. timeout is wall-clock seconds, default 20.

    The child is a session leader so the kill covers Chrome and the processes it spawned.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        _give_up(HUNG)
    if not (out or "").strip():
        _give_up(EMPTY)
    return out
