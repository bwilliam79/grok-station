#!/usr/bin/env python3
"""L3: the station persona id rule (widened in V6 52e295b from [a-z]{2,16} to admin/relay's
[a-z][a-z0-9-]{1,23}) still refuses traversal and odd ids, and a portrait path never leaves PORTRAIT_DIRS."""
import json
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import grok_station as gs  # noqa: E402

BAD = ["../etc", "..", "a/b", "a\\b", "a.png", "juniper.", "/abs", "Juniper", "j", "1abc", "-abc", "ju niper",
       "juniper\n", "juni\x00per", "a" * 25, "jüniper", "", "%2e%2e", "a..b", "~root"]
GOOD = ["juniper", "juniper-2", "kitchen-bot", "ab", "a" * 24]


class PersonaSlug(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_file, old = gs.PERSONAS_FILE, dict(gs._personas)

        def restore():
            gs.PERSONAS_FILE = old_file
            gs._personas.clear()
            gs._personas.update(old)
        self.addCleanup(restore)
        self.path = os.path.join(self.tmp.name, "personas.json")
        gs.PERSONAS_FILE = self.path

    def _load(self, slugs):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"personas": [{"slug": s, "name": "N%d" % k} for k, s in enumerate(slugs)]}, fh)
        gs._personas["mtime"] = None
        return {v[1] for v in gs.persona_names().values()} - {"grok"}

    def test_odd_ids_refused(self):
        self.assertEqual(self._load(BAD), set())

    def test_real_ids_kept(self):
        self.assertEqual(self._load(GOOD), set(GOOD))

    def test_same_rule_as_admin_and_relay(self):
        src = open(os.path.join(ROOT, "app", "grok_station.py"), encoding="utf-8").read()
        self.assertIn('re.fullmatch(r"[a-z][a-z0-9-]{1,23}", slug)', src)
        self.assertIn('r"^[a-z][a-z0-9-]{1,23}$"', open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read())
        self.assertIn('r"^[a-z][a-z0-9-]{1,23}$"', open(os.path.join(ROOT, "relay", "handoff.py"), encoding="utf-8").read())

    def test_portrait_path_stays_in_dirs(self):
        d = self.tmp.name
        for s in GOOD:
            open(os.path.join(d, s + ".png"), "wb").close()
        old = list(gs.PORTRAIT_DIRS)
        gs.PORTRAIT_DIRS[:] = [d] if isinstance(gs.PORTRAIT_DIRS, list) else gs.PORTRAIT_DIRS
        try:
            self._load(GOOD + BAD)
            for k in range(len(GOOD) + len(BAD)):
                p = gs.find_portrait("N%d" % k)
                if p is not None:
                    self.assertEqual(os.path.dirname(os.path.realpath(p)), os.path.realpath(d))
                    self.assertRegex(os.path.basename(p), r"^[a-z][a-z0-9-]{1,23}\.(png|jpg)$")
        finally:
            gs.PORTRAIT_DIRS[:] = old


if __name__ == "__main__":
    unittest.main()
