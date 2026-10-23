"""Mic gain defaults: the listener and the admin page share wake/gain.py.

A save that does not mention WAKE_GAIN or VOICE_GAIN must leave those env lines absent or unchanged.
No network, no audio device.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import gain


def helper():
    g = {"__name__": "helper", "__file__": os.path.join(ROOT, "admin", "grok-station-helper")}
    exec(compile(open(os.path.join(ROOT, "admin", "grok-station-helper")).read(), "helper", "exec"), g)
    return g


def call(g, fn, *args, stdin=""):
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            try:
                g[fn](*args)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdin = old
    line = buf.getvalue().strip().splitlines()
    return code, json.loads(line[-1] if line else "{}")


class GainDefaults(unittest.TestCase):
    def test_default_gain_per_device(self):
        for name in ("EMEET", "emeet", "Emeet Speaker", "usb emeet"):
            self.assertEqual(gain.default_gain(name), "2", name)
        for name in ("Razer", "USB Audio", "x"):
            self.assertEqual(gain.default_gain(name), "4", name)
        self.assertEqual(gain.gain_by_device(), {"emeet": "2", "other": "4"})

    def test_voice_follows_wake_when_unset(self):
        self.assertEqual(gain.effective_gains("EMEET", None, None), {"WAKE_GAIN": "2", "VOICE_GAIN": "2"})
        self.assertEqual(gain.effective_gains("", None, None), {"WAKE_GAIN": "2", "VOICE_GAIN": "2"})
        self.assertEqual(gain.effective_gains("USB Audio", None, None), {"WAKE_GAIN": "4", "VOICE_GAIN": "4"})
        # An explicit wake gain is the voice default. The device default is not.
        self.assertEqual(gain.effective_gains("USB Audio", "3", None), {"WAKE_GAIN": "3", "VOICE_GAIN": "3"})
        self.assertEqual(gain.effective_gains("EMEET", "3.5", "5"), {"WAKE_GAIN": "3.5", "VOICE_GAIN": "5"})
        self.assertEqual(gain.effective_gains("EMEET", "  ", ""), {"WAKE_GAIN": "2", "VOICE_GAIN": "2"})

    def test_listener_uses_the_same_helper(self):
        import grok_wake as gw
        self.assertEqual(gw.default_gain("EMEET"), "2")
        self.assertEqual(gw.default_gain("Razer"), gain.default_gain("Razer"))
        src = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()
        self.assertIn("gain.py", src)
        self.assertNotIn('return "2" if "emeet"', src)
        install = open(os.path.join(ROOT, "install.sh")).read()
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn("wake/gain.py", install)
        self.assertIn('"$S/wake/gain.py"', files)


class SettingsGain(unittest.TestCase):
    def setUp(self):
        self.g = helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.wake = os.path.join(self.tmp.name, "wake.env")
        self.g["ENV"]["wake"] = self.wake

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.wake, "w") as fh:
            fh.write(text)

    def test_defaults_returned_per_device(self):
        cases = [
            ("", {"WAKE_GAIN": "2", "VOICE_GAIN": "2"}),
            ("WAKE_DEVICE_NAME=EMEET\n", {"WAKE_GAIN": "2", "VOICE_GAIN": "2"}),
            ("WAKE_DEVICE_NAME=EMEET Speaker\n", {"WAKE_GAIN": "2", "VOICE_GAIN": "2"}),
            ("WAKE_DEVICE_NAME=USB Audio\n", {"WAKE_GAIN": "4", "VOICE_GAIN": "4"}),
            ("WAKE_DEVICE_NAME=USB Audio\nWAKE_GAIN=3\n", {"WAKE_GAIN": "3", "VOICE_GAIN": "3"}),
            ("WAKE_GAIN=2\nVOICE_GAIN=5\n", {"WAKE_GAIN": "2", "VOICE_GAIN": "5"}),
        ]
        for text, expect in cases:
            self._write(text)
            code, body = call(self.g, "settings_get", "audio")
            self.assertEqual(code, 0, body)
            self.assertEqual(body["defaults"], expect, text)
            self.assertEqual(body["gain_by_device"], {"emeet": "2", "other": "4"})
        self._write("WAKE_DEVICE_NAME=USB Audio\nWAKE_GAIN=3\n")
        code, body = call(self.g, "settings_get", "audio")
        self.assertIsNone(body["values"]["VOICE_GAIN"])
        self.assertEqual(body["values"]["WAKE_GAIN"], "3")
        self.assertNotEqual(body["defaults"]["VOICE_GAIN"], body["gain_by_device"]["other"])

    def test_put_without_gains_leaves_lines_absent_or_unchanged(self):
        self._write("WAKE_CAPTURE_RATE=16000\n")
        before = open(self.wake).read()
        code, body = call(self.g, "settings_set", "audio", stdin="{}")
        self.assertEqual(code, 0, body)
        self.assertEqual(open(self.wake).read(), before)
        code, body = call(self.g, "settings_set", "audio", stdin=json.dumps({"WAKE_DEVICE_NAME": "USB Audio"}))
        self.assertEqual(code, 0, body)
        text = open(self.wake).read()
        self.assertNotIn("WAKE_GAIN", text)
        self.assertNotIn("VOICE_GAIN", text)
        self.assertIn('WAKE_DEVICE_NAME="USB Audio"\n', text)
        code, body = call(self.g, "settings_get", "audio")
        self.assertEqual(body["defaults"], {"WAKE_GAIN": "4", "VOICE_GAIN": "4"})
        self.assertIsNone(body["values"]["WAKE_GAIN"])
        self.assertIsNone(body["values"]["VOICE_GAIN"])

        self._write("WAKE_GAIN=2.0\nVOICE_GAIN=3.5\n")
        code, body = call(self.g, "settings_set", "audio", stdin=json.dumps({"WAKE_CAPTURE_RATE": "48000"}))
        self.assertEqual(code, 0, body)
        text = open(self.wake).read()
        self.assertIn("WAKE_GAIN=2.0\n", text)
        self.assertIn("VOICE_GAIN=3.5\n", text)
        self.assertEqual(text.count("WAKE_GAIN"), 1)
        self.assertEqual(text.count("VOICE_GAIN"), 1)
        self.assertIn("WAKE_CAPTURE_RATE=48000\n", text)

    def test_put_with_one_gain_writes_only_that_line(self):
        self._write("")
        code, body = call(self.g, "settings_set", "audio", stdin=json.dumps({"WAKE_GAIN": "2.5"}))
        self.assertEqual(code, 0, body)
        text = open(self.wake).read()
        self.assertIn("WAKE_GAIN=2.5\n", text)
        self.assertNotIn("VOICE_GAIN", text)
        code, body = call(self.g, "settings_set", "audio", stdin=json.dumps({"VOICE_GAIN": "4.0"}))
        self.assertEqual(code, 0, body)
        text = open(self.wake).read()
        self.assertIn("WAKE_GAIN=2.5\n", text)
        self.assertIn("VOICE_GAIN=4.0\n", text)


if __name__ == "__main__":
    unittest.main()
