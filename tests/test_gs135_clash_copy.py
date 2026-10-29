#!/usr/bin/env python3
"""Noor's device-name clash copy (0.13.5): names the other device and its source, offers a safe example."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    start = JS.index("function " + name + "(")
    depth, i = 0, JS.index("{", start)
    while True:
        c = JS[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return JS[start:i]


def _run(expr):
    src = "\n".join(_fn(n) for n in ("spokenKey", "roomExample", "sourceLabel", "deviceClashMsg", "batchClashMsg", "clashPhrase"))
    out = subprocess.run(["node", "-e", src + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClashCopy(unittest.TestCase):
    def test_bar_light_vs_wled_bar_lights_names_device_and_source(self):
        msg = _run('deviceClashMsg("Bar Light", "Bar lights", "wled", ex => false)')
        self.assertEqual(msg, "Sounds the same as \u201cBar lights\u201d (WLED) when you say it. "
                              "Pick a name that sounds different, like \u201cBar room light\u201d.")
        hb = _run('deviceClashMsg("Bar Light", "Bar lights", "homebridge", ex => false)')
        self.assertIn("\u201cBar lights\u201d (Homebridge)", hb)

    def test_example_that_would_clash_is_dropped(self):
        msg = _run('deviceClashMsg("Bar Light", "Bar lights", "wled", ex => ex === "Bar room light")')
        self.assertEqual(msg, "Sounds the same as \u201cBar lights\u201d (WLED) when you say it. Pick a name that sounds different.")
        self.assertNotIn("like", msg)

    def test_no_light_word_drops_example_and_source_is_optional(self):
        msg = _run('deviceClashMsg("Kitchen", "kitchen", "", ex => false)')
        self.assertEqual(msg, "Sounds the same as \u201ckitchen\u201d when you say it. Pick a name that sounds different.")

    def test_same_batch_wording(self):
        self.assertEqual(_run('batchClashMsg("Bar lights")'), "Sounds the same as \u201cBar lights\u201d above")

    def test_old_copy_is_gone_everywhere(self):
        self.assertNotIn("Used by another device", JS)
        for path in ("rowClash", "serverClashMsg"):
            self.assertIn("function " + path, JS)
        self.assertIn("serverClashMsg(serverClash, values[i], others)", JS)
        # 0.13.7: Homebridge names moved to Voice names; its device clash names the source the same way.
        self.assertIn("function voiceClashBody(hit)", JS)
        self.assertIn("sourceLabel(hit.src)", JS)
        self.assertIn("batchClashMsg(a)", JS)


if __name__ == "__main__":
    unittest.main()
