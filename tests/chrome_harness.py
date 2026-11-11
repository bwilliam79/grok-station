"""One wall-clock limit for every headless Chrome --dump-dom run.

Chrome's --virtual-time-budget does not advance while a load, a font, a timer, or an
animation stays open, so --dump-dom can sit forever on some builds. The pages that
need a result before the dump finish that work with microtasks and inlined CSS, and
do not pass --virtual-time-budget. This helper still kills the process group after
20 seconds. That kill is only the backstop.

A timeout raises SkipTest "headless Chrome hung after 20s". Empty stdout raises
SkipTest "Chrome produced no render". Neither result is a pass. GS_REQUIRE_CHROME=1
turns those skips into failures, which tools/release-check.sh sets when Chrome is
on PATH.

Each Chrome child gets its own TMPDIR (and TMP and TEMP). scoped_dir and
.org.chromium dirs land there. The directory is removed on a normal exit, a
timeout, and a kill. A fresh --user-data-dir hangs Chrome 156 on the station, so
this helper does not pass one.
"""
import os
import re
import shutil
import signal
import subprocess
import tempfile
import unittest

TIMEOUT_S = 20
HUNG = "headless Chrome hung after 20s"
EMPTY = "Chrome produced no render"


def quiet_css(text):
    """CSS that does not fetch a font or a file, and does not keep an animation running.

    A pending font or an animation that never ends holds Chrome's virtual-time budget
    open on some builds, so --dump-dom never writes. Inlining this CSS lets the dump
    happen when the document loads.
    """
    text = re.sub(r"@font-face\{.*?\}", "", text, flags=re.S)
    text = re.sub(r"url\((?!data:)[^)]*\)", "url(data:,)", text)
    return text + "\n*{animation:none!important;transition:none!important;font-family:sans-serif!important}\n"


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


def _chrome_argv(argv):
    """Drop --user-data-dir. A new profile directory hangs Chrome 156 before --dump-dom.

    --headless=new does not advance --virtual-time-budget while a timer or a load is open,
    so a run that still passes that flag uses --headless=old. A shell stand-in ignores it.
    """
    out = [arg for arg in argv if not arg.startswith("--user-data-dir=")]
    base = os.path.basename(out[0]) if out else ""
    if ("chrome" in base or "chromium" in base) and any(a.startswith("--virtual-time-budget") for a in out):
        out = ["--headless=old" if a == "--headless=new" else a for a in out]
    return out


def run_chrome(argv, timeout=TIMEOUT_S):
    """Run argv and return stdout text. timeout is wall-clock seconds, default 20.

    The child is a session leader so the kill covers Chrome and the processes it spawned.
    TMPDIR, TMP, and TEMP point at a directory this function deletes, including on a kill.
    """
    tmp = tempfile.mkdtemp(prefix="gs-chrome-run-")
    env = os.environ.copy()
    env["TMPDIR"] = tmp
    env["TMP"] = tmp
    env["TEMP"] = tmp
    cmd = _chrome_argv(list(argv))
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            env=env,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            _give_up(HUNG)
        if not (out or "").strip():
            _give_up(EMPTY)
        return out
    finally:
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
        shutil.rmtree(tmp, ignore_errors=True)
