#!/usr/bin/env python3
"""N3: "train my wake word for Grok" does not record.

The station speaks one line and the screen shows it for 4 seconds:
"Grok's wake word is built in and can't be trained. Say a bot's name to train its wake word."
A normal notice still holds 10 s. A finished reply still holds REPLY_HOLD.
"""
import importlib.util
import os
import sys
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("RELAY_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))

import grok_station as gs  # noqa: E402


def _relay():
    """Import relay at test time, not at collection time.

    relay reads its state paths from the environment once, on first import. A module-level
    import here ran before test_handoff set HANDOFF_PENDING, so relay kept /var/lib/grok-station.
    """
    import relay
    return relay

SAY = "Grok's wake word is built in and can't be trained. Say a bot's name to train its wake word."
GROK = {"slug": "grok", "name": "Grok", "phrase": None, "spoken": "Grok"}


def _train_flow():
    path = os.path.join(ROOT, "relay", "train_flow.py")
    spec = importlib.util.spec_from_file_location("gs_train_flow_n3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TF = _train_flow()


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class GrokBuiltin(unittest.TestCase):
    def test_grok_is_spoken_once_and_not_recorded(self):
        plan = TF.voice_train_plan(GROK, has_model=False)
        self.assertFalse(plan["begin"])
        self.assertIsNone(plan["enroll"])
        self.assertFalse(plan["tile"])
        self.assertEqual(plan["say"], SAY)
        self.assertEqual(plan["hold_s"], 4)
        again = TF.voice_train_plan({"slug": "grok", "name": "Grok", "phrase": "hey_grok", "spoken": "Grok"})
        self.assertEqual(again["say"], SAY)
        self.assertEqual(again["hold_s"], 4)
        self.assertIsNone(again["enroll"])
        missing = TF.voice_train_plan({"slug": "", "name": "", "phrase": None, "spoken": "Nova"})
        self.assertEqual(missing["say"], "I don't have a bot called Nova.")
        self.assertNotIn("hold_s", missing)
        quiet = TF.voice_train_plan({"slug": "juniper", "name": "Juniper", "phrase": None, "spoken": "Juniper"})
        self.assertEqual(quiet["say"], "")
        self.assertIsNone(quiet["enroll"])
        self.assertNotIn("hold_s", quiet)

        result = _relay().train_session_result(plan)
        self.assertEqual(result["say"], SAY)
        self.assertIsNone(result["enroll"])
        self.assertEqual(result["hold_s"], 4)
        self.assertEqual(result["done"]["type"], "done")
        self.assertTrue(result["done"]["local"])
        self.assertEqual(result["done"]["hold_s"], 4)
        self.assertEqual(result["done"]["text"], SAY)
        self.assertEqual(result["end"], "user ended")
        kept = _relay().train_session_result(TF.voice_train_plan(
            {"slug": "juniper", "name": "Juniper", "phrase": "hey_juniper", "spoken": "Juniper"}))
        self.assertEqual(kept["done"], {"type": "done", "local": True})
        self.assertNotIn("hold_s", kept)

    def test_the_screen_holds_this_line_for_four_seconds_only(self):
        self.assertEqual(gs.state_hold("notice", False, ""), gs.VOICE_HOLDS["notice"])
        self.assertEqual(gs.VOICE_HOLDS["notice"], 10)
        self.assertEqual(gs.state_hold("speaking", True, ""), gs.REPLY_HOLD)
        self.assertEqual(gs.state_hold("notice", False, "", 4), 4)
        self.assertEqual(gs.state_hold("speaking", True, "", 4), 4)
        ev = gs.state_event({"state": "notice", "text": SAY, "hold_s": 4})
        self.assertEqual(ev["hold_s"], 4)
        self.assertEqual(ev["text"], SAY)
        self.assertIsNone(gs.state_event({"state": "notice", "text": SAY, "hold_s": 10})["hold_s"])
        self.assertIsNone(gs.state_event({"state": "notice", "text": SAY, "hold_s": "4"})["hold_s"])
        station = _src("app/grok_station.py")
        self.assertIn(
            'state_hold(ev.state, bool(ev.done), getattr(ev, "heard", "") or "", getattr(ev, "hold_s", None))',
            station)

    def test_relay_and_listener_do_not_start_a_session(self):
        src = _src("relay/relay.py")
        i_train = src.find("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
        block = src[i_train:src.find('return "user ended"', i_train)]
        self.assertIn("play_fixed(plan", block)
        self.assertEqual(block.count("play_fixed(plan"), 1)  # the plan line once; a failed start adds its own line
        self.assertIn('if result["enroll"]', block)
        self.assertIn('result.get("hold_s") == 4', block)
        self.assertIn("TTS_REPLY_S", block)
        wake = _src("wake/grok_wake.py")
        i_done = wake.find('elif t in ("done", "timeout", "error"):')
        done = wake[i_done:wake.find("self.feeding = False", i_done)]
        self.assertIn('ev.get("hold_s") == 4', done)
        self.assertIn('self.state("notice", text=self.reply, hold_s=4)', done)
        self.assertIn("if not builtin_hold:", done)


class CollectionDoesNotImportRelay(unittest.TestCase):
    def test_loading_this_module_leaves_relay_unimported(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import test_gs138_n3_grok_builtin; "
                "print('relay' in sys.modules)") % os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "False", out.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
