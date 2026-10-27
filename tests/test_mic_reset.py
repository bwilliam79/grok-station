"""Capture reopen (stall, silence, exit) and speakerphone USB reset. No reset during a session."""
import datetime
import json
import os
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import grok_wake as gw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeRec:
    def __init__(self, mode, exit_after=None):
        self.mode = mode
        self.exit_after = exit_after
        self.killed = False
        self.stdout = self
        self.reads = 0

    def read(self, n):
        if self.mode == "stall":
            while not self.killed:
                time.sleep(0.01)
            return b""
        if self.killed:
            return b""
        self.reads += 1
        if self.exit_after is not None and self.reads > self.exit_after:
            return b""
        if self.mode == "zeros":
            return b"\x00" * n
        if self.mode == "flat":
            return np.full(n // 2, 7, np.int16).tobytes()
        if self.mode == "noise":
            return np.arange(n // 2, dtype=np.int16).tobytes()[:n]
        return b""

    def kill(self):
        self.killed = True


def _within(fn, timeout=3):
    box = {}

    def go():
        try:
            box["out"] = fn()
        except Exception as exc:
            box["err"] = exc

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise AssertionError("timed out")
    if "err" in box:
        raise box["err"]
    return box.get("out")


class CaptureReopen(unittest.TestCase):
    def _rounds(self, mode, *, rounds=2, session=False, stall_s=3.0, silence_s=10.0, exit_after=None, n_bytes=8):
        calls = {"find": 0, "spawn": 0}
        procs = []
        logs = []

        def find():
            calls["find"] += 1
            return "plughw:CARD=M0,DEV=0"

        def spawn(cap):
            calls["spawn"] += 1
            proc = FakeRec(mode, exit_after=exit_after)
            procs.append(proc)
            return proc

        try:
            reasons = _within(lambda: gw.run_capture_rounds(
                find, spawn, lambda buf, guard: None, lambda: session, rounds=rounds,
                stall_s=stall_s, silence_s=silence_s, chunk_s=0.08, sleep=lambda s: None,
                log=logs.append, gap=30.0, n_bytes=n_bytes))
        finally:
            for proc in procs:
                proc.kill()
        return reasons, calls, logs, procs

    def test_stall_silence_and_exit_each_reopen(self):
        reasons, calls, logs, _procs = self._rounds("stall", stall_s=0.15)
        self.assertEqual(reasons, ["stall", "stall"])
        self.assertEqual(calls["find"], 2)
        self.assertEqual(calls["spawn"], 2)
        self.assertIn("capture reopen: stall", logs)
        self.assertIn("capture ended; restarting", logs)

        reasons, calls, logs, _procs = self._rounds("zeros", silence_s=0.16)
        self.assertEqual(reasons, ["silence", "silence"])
        self.assertEqual(calls["find"], 2)
        self.assertIn("capture reopen: silence", logs)

        reasons, calls, _logs, _procs = self._rounds("flat", silence_s=0.16)
        self.assertEqual(reasons, ["silence", "silence"])

        reasons, calls, _logs, _procs = self._rounds("exit")
        self.assertEqual(reasons, ["eof", "eof"])
        self.assertEqual(calls["find"], 2)
        self.assertEqual(calls["spawn"], 2)

    def test_silence_does_not_reopen_during_a_session(self):
        reasons, _calls, logs, _procs = self._rounds("zeros", rounds=1, session=True, silence_s=0.16, exit_after=6)
        self.assertEqual(reasons, ["eof"])
        self.assertFalse(any("silence" in line for line in logs))

    def test_stall_still_reopens_during_a_session(self):
        reasons, _calls, _logs, _procs = self._rounds("stall", rounds=1, session=True, stall_s=0.15)
        self.assertEqual(reasons, ["stall"])

    def test_chunk_stuck_and_reopen_gap(self):
        self.assertTrue(gw.chunk_stuck(np.zeros(8, np.int16).tobytes()))
        self.assertTrue(gw.chunk_stuck(np.full(8, 7, np.int16).tobytes()))
        self.assertTrue(gw.chunk_stuck(np.array([0, 1, 0, 1], np.int16).tobytes()))
        self.assertFalse(gw.chunk_stuck(np.array([0, 2, 0, 3], np.int16).tobytes()))
        self.assertFalse(gw.chunk_stuck(b"\x00\x00"))
        self.assertEqual(gw.reopen_delay("eof", 10, 0), 2.0)
        self.assertEqual(gw.reopen_delay("reset", 10, 8), 2.0)
        self.assertEqual(gw.reopen_delay("stall", 10, 0), 30.0)
        self.assertEqual(gw.reopen_delay("silence", 10, 8, gap=30), 28.0)


def _recovery(**kw):
    calls = []
    logs = []
    kicked = []
    flags = kw.pop("flags", {"button": True, "night": True, "drought": True})
    busy = kw.pop("busy", lambda: False)
    clock = kw.pop("clock", {"t": 1000.0})
    when = kw.pop("when", {"dt": datetime.datetime(2026, 10, 27, 3, 40)})

    def reset(card):
        calls.append(card)
        return None

    rec = gw.MicRecovery(reset, now=lambda: clock["t"], local=lambda ts: when["dt"], flags=lambda: flags,
                         card=lambda: "M0", busy=busy, kick=lambda: kicked.append(1), log=logs.append,
                         need=kw.get("need", 20), window_s=kw.get("window_s", 7200.0),
                         score_max=kw.get("score_max", 0.1), speech_db=kw.get("speech_db", -45.0),
                         coalesce_s=kw.get("coalesce_s", 2.0))
    return rec, calls, logs, kicked, clock, when, flags


class MicReset(unittest.TestCase):
    def test_button_inside_the_gap_is_refused_kindly(self):
        rec, calls, _logs, _kicked, clock, _when, _flags = _recovery()
        self.assertEqual(rec.request("button"), "ok")
        clock["t"] += 44
        self.assertEqual(rec.request("button"), "too_soon")
        self.assertEqual(calls, ["M0"])
        clock["t"] += 1
        self.assertEqual(rec.request("button"), "ok")
        self.assertEqual(calls, ["M0", "M0"])
        src = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()
        self.assertIn('"too_soon"', src)
        self.assertIn("The microphone was just reset.", src)
        admin = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        self.assertIn('audit("-", "mic-reset ', admin)

    def test_no_usb_reset_during_a_session(self):
        busy = {"on": True}
        rec, calls, logs, kicked, clock, _when, _flags = _recovery(busy=lambda: busy["on"])
        self.assertEqual(rec.request("button"), "deferred")
        self.assertEqual(calls, [])
        self.assertEqual(kicked, [])
        self.assertIn("mic reset deferred (session): button", logs)
        busy["on"] = False
        clock["t"] += 2
        rec.poll()
        self.assertEqual(calls, ["M0"])
        self.assertEqual(kicked, [1])
        self.assertIn("mic reset: button", logs)

    def test_night_once_and_skipped_after_the_window(self):
        rec, calls, logs, _kicked, clock, when, _flags = _recovery()
        self.assertEqual(rec.request("night"), "ok")
        self.assertEqual(calls, ["M0"])
        clock["t"] += 5
        self.assertEqual(rec.request("night"), "off")
        self.assertEqual(calls, ["M0"])

        busy = {"on": True}
        rec, calls, logs, _kicked, clock, when, _flags = _recovery(busy=lambda: busy["on"])
        self.assertEqual(rec.request("night"), "deferred")
        when["dt"] = datetime.datetime(2026, 10, 27, 5, 0)
        clock["t"] += 2
        rec.poll()
        self.assertEqual(calls, [])
        self.assertIn("mic reset skipped: night window ended", logs)

        rec, calls, logs, _kicked, _clock, _when, _flags = _recovery()
        rec.poll()
        self.assertEqual(calls, ["M0"])
        self.assertIn("mic reset: night", logs)

    def test_drought_needs_twenty_over_two_hours_and_a_real_score_clears_it(self):
        rec, calls, _logs, _kicked, clock, _when, _flags = _recovery(need=20, window_s=7200, coalesce_s=2)
        for i in range(19):
            clock["t"] = i * (7200 / 19)
            rec.note_level(-30, 0.001, session=False)
        self.assertEqual(calls, [])
        clock["t"] = 7200
        rec.note_level(-30, 0.001, session=False)
        self.assertEqual(calls, ["M0"])
        self.assertEqual(rec.events, [])

        rec, calls, _logs, _kicked, clock, _when, _flags = _recovery()
        clock["t"] = 0
        rec.note_level(-30, 0.001, session=False)
        self.assertEqual(len(rec.events), 1)
        rec.note_level(-30, 0.5, session=False)
        self.assertEqual(rec.events, [])
        for i in range(30):
            clock["t"] = 1000 + i * 400
            rec.note_level(-30, 0.001, session=True)
        self.assertEqual(rec.events, [])
        self.assertEqual(calls, [])
        busy = {"on": True}
        rec.busy = lambda: busy["on"]
        rec.note_level(-30, 0.001, session=False)
        self.assertEqual(rec.events, [])

    def test_flags_file_beats_env_and_off_does_not_reset(self):
        old = {k: os.environ.get(k) for k in ("WAKE_MIC_RESET_BUTTON", "WAKE_MIC_RESET_NIGHT", "WAKE_MIC_RESET_DROUGHT")}
        try:
            os.environ["WAKE_MIC_RESET_BUTTON"] = "1"
            os.environ["WAKE_MIC_RESET_NIGHT"] = "1"
            os.environ["WAKE_MIC_RESET_DROUGHT"] = "0"
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "mic-reset.json")
                json.dump({"button": False, "night": True}, open(path, "w"))
                flags = gw.mic_reset_flags(path)
            self.assertEqual(flags, {"button": False, "night": True, "drought": False})
            os.environ.pop("WAKE_MIC_RESET_BUTTON", None)
            self.assertTrue(gw.mic_reset_flags(os.path.join(d, "missing.json"))["button"])
        finally:
            for key, val in old.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        rec, calls, logs, _kicked, _clock, _when, _flags = _recovery(flags={"button": False, "night": False, "drought": False})
        self.assertEqual(rec.request("button"), "off")
        self.assertEqual(calls, [])
        self.assertFalse(any(line.startswith("mic reset:") for line in logs))

    def test_button_endpoint_defers_and_listener_stays_unprivileged(self):
        import http.client
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        old_port, old_rec = gw.CONTROL_PORT, gw.RECOVERY
        gw.CONTROL_PORT = port
        calls = []
        gw.RECOVERY = gw.MicRecovery(lambda card: calls.append(card) or None, busy=lambda: True, card=lambda: "M0",
                                     flags=lambda: {"button": True, "night": True, "drought": True},
                                     log=lambda msg: None, kick=lambda: None,
                                     local=lambda ts: datetime.datetime(2026, 10, 27, 15, 0))
        th = threading.Thread(target=gw.control_server, daemon=True)
        th.start()

        def post():
            last = None
            for _ in range(50):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    c.request("POST", "/mic-reset", b"{}", {"Content-Type": "application/json"})
                    r = c.getresponse()
                    out = json.loads(r.read() or b"{}")
                    c.close()
                    return r.status, out
                except OSError as exc:
                    last = exc
                    time.sleep(0.02)
            raise last

        try:
            status, body = post()
            self.assertEqual(status, 202)
            self.assertTrue(body["deferred"])
            self.assertEqual(calls, [])
            gw.RECOVERY = None
            status, body = post()
            self.assertEqual(status, 503)
            self.assertIn("microphone", body["error"].lower())
        finally:
            if gw.CONTROL_HTTPD is not None:
                gw.CONTROL_HTTPD.shutdown()
                th.join(2)
                gw.CONTROL_HTTPD.server_close()
                gw.CONTROL_HTTPD = None
            gw.CONTROL_PORT, gw.RECOVERY = old_port, old_rec

    def test_sudo_command_and_admin_page(self):
        seen = []

        class Result:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        self.assertIsNone(gw.run_usb_reset("M0", run=lambda argv: seen.append(argv) or Result()))
        self.assertEqual(seen[0][:2], ["sudo", "-n"])
        self.assertTrue(seen[0][2].endswith("gs-usb-reset"))
        self.assertEqual(seen[0][3], "M0")

        class Bad:
            returncode = 1
            stdout = "refused: /tmp/secret\n"
            stderr = ""

        self.assertEqual(gw.run_usb_reset("M0", run=lambda argv: Bad()), "refused: reset failed")
        old = os.environ.get("WAKE_USB_RESET")
        os.environ["WAKE_USB_RESET"] = "/tmp/not a path"
        try:
            self.assertEqual(gw.run_usb_reset("M0", run=lambda argv: Result()), "refused: bad helper")
        finally:
            if old is None:
                os.environ.pop("WAKE_USB_RESET", None)
            else:
                os.environ["WAKE_USB_RESET"] = old
        self.assertEqual(gw.card_spec("plughw:CARD=M0,DEV=0"), "M0")
        self.assertIsNone(gw.card_spec("plughw:CARD=card0,DEV=0"))
        self.assertIsNone(gw.card_spec("hw:0,0"))

        admin = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        put = admin.split("def api_put_audio", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("/api/audio/mic-reset", put)
        self.assertIn("save_mic_reset", put)
        self.assertNotIn("restart", put)
        save = admin.split("def save_mic_reset", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("isinstance(val, bool)", save)
        self.assertIn("0o640", save)
        post = admin.split("def api_post_audio", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('"/mic-reset"', post)
        self.assertIn("timeout=8", post)
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        audio = js.split("async function audioPanel", 1)[1].split("\nconst ICO_WAVE", 1)[0]
        self.assertIn(">Reset microphone<", audio)
        self.assertIn("setTog", audio)
        self.assertIn('"/api/audio/mic-reset"', audio)
        self.assertIn(">Save<", audio)
        self.assertNotIn(".onblur", audio)
        self.assertNotIn("mailbox", audio.lower())
        self.assertNotIn("seal", audio.lower())
        self.assertNotIn("homehost", audio.lower())
        wake = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()
        main = wake.split("\ndef main():", 1)[1].split("\nif __name__", 1)[0]
        self.assertIn('find_card("arecord")', main)
        self.assertIn("run_capture_rounds", main)
        self.assertIn("User=grok", open(os.path.join(ROOT, "systemd", "grok-station-wake.service")).read())


if __name__ == "__main__":
    unittest.main()
