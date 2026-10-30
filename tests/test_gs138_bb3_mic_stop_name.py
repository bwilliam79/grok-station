#!/usr/bin/env python3
"""BB3 (Bugbot @a82107d): the station mic-stop line never names Grok for a non-Grok persona.
personas.json keeps the full 24-character slug, and a lookup miss falls back to the wake word label."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import grok_wake as gw  # noqa: E402

LONG = "kitchen-helper-number-24"  # 24 chars, valid per admin SLUG_RE


class MicStopName(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = gw.PORTRAITS
        gw.PORTRAITS = self.d
        self.addCleanup(lambda: setattr(gw, "PORTRAITS", self.old))

    def test_sync_keeps_full_slug(self):
        src = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn('"slug": str(p.get("slug", ""))[:24]', src)

    def test_long_slug_resolves(self):
        with open(os.path.join(self.d, "personas.json"), "w") as f:
            json.dump({"personas": [{"slug": LONG, "name": "Kitchen"}]}, f)
        self.assertEqual(gw.station_bot_name(LONG, "Kitchen Helper"), "Kitchen")

    def test_miss_falls_back_to_label_not_grok(self):
        with open(os.path.join(self.d, "personas.json"), "w") as f:
            json.dump({"personas": []}, f)
        self.assertEqual(gw.station_bot_name(LONG, "Kitchen Helper"), "Kitchen Helper")
        self.assertEqual(gw.station_bot_name("grok", "Grok"), "Grok")

    def test_mic_stop_uses_it(self):
        src = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn("train_flow.mic_stopped_station_line(station_bot_name(persona, label))", src)


if __name__ == "__main__":
    unittest.main()
