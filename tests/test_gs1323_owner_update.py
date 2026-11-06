"""0.13.23: bots whose update command has no --owner, or --owner 'your owner', match after Update.

Those commands render the placeholder owner. The station renders its real owner, so the
signed hash was 'different' and every later update kept the placeholder. update_bot now
delivers the station owner; a placeholder-only hash stays updatable and the next run matches.
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_finish_update as fin  # noqa: E402

OWNER = "Owner Name"
STATION = "https://st.example"


def _ga():
    return fin._ga()


class LegacyOwnerUpdate(unittest.TestCase):
    """(1) no --owner  (2) --owner 'your owner'. Both must finish matching, not 'different'."""

    def setUp(self):
        self.ga = _ga()
        self.pins = {"commit": fin.U, "update_commit": fin.C,
                     "files": {"scripts/gs-update.sh": fin.sha(open(fin.SCRIPT, "rb").read())}}
        self._v = self.ga.version
        self.ga.version = lambda: fin.REL
        self.fin = {"sent": {}, "copied": {}, "updaters": {}, "failed": {}}
        self.ga.write_json(self.ga.path("auth.json"), {"name": OWNER})

    def tearDown(self):
        self.ga.version = self._v
        doc = self.ga.read_json(self.ga.path("auth.json"), {})
        if isinstance(doc, dict) and "name" in doc:
            doc.pop("name", None)
            self.ga.write_json(self.ga.path("auth.json"), doc)

    def _bot(self, update=None):
        return {"slug": "jules", "name": "Bot", "paired": True, "webhook": {"configured": True},
                "key_mode": "home", "reply_host": "pc", "helper": {"version": "0.11.7"},
                "update": update}

    def _rows(self, update=None):
        view = self.ga.finish_rows([self._bot(update)], STATION, fin.FP, fin.PUB, self.pins, self.fin)
        return view, view["computers"][0]["bots"][0]

    def _legacy_command(self, text, placeholder):
        flag = " --owner '%s'" % OWNER
        self.assertEqual(text.count(flag), 1)
        repl = " --owner 'your owner'" if placeholder else ""
        legacy = text.replace(flag, repl, 1)
        cmd = [ln for ln in legacy.split("\n") if ln.startswith("d=$(mktemp -d)")]
        self.assertEqual(len(cmd), 1)
        self.assertNotIn(" --owner '%s'" % OWNER, cmd[0])
        if placeholder:
            self.assertIn(" --owner 'your owner'", cmd[0])
        else:
            self.assertNotIn("--owner", cmd[0])
        return legacy, cmd[0]

    def _run(self, cmd, owner):
        fake = fin.Fake("home")
        fake.put(fin.RAW + fin.U + "/scripts/gs-update.sh", open(fin.SCRIPT, "rb").read())
        env = dict(fake.env(), RELEASE=fin.REL, COMMIT=fin.C, OWNER=owner)
        return subprocess.run(["sh", "-c", 'set -e; out=$(' + cmd + '); printf "%s" "$out"'],
                              env=env, capture_output=True, text=True, timeout=60)

    def _send_owner(self, want):
        calls, sent = [], []
        doc = {"sent": {}, "copied": {}, "updaters": {}, "failed": {}}

        class H:
            def send(self, code, body):
                sent.append((code, body))
                return code

            def _finish_inputs(self):
                return [self_bot], STATION, fin.FP, fin.PUB

        self_bot = self._bot()
        saved = (self.ga.read_finish, self.ga.write_json, self.ga.relay_call, self.ga.deploy_pins, self.ga.audit)
        self.ga.read_finish = lambda: json.loads(json.dumps(doc))
        self.ga.write_json = lambda path, d: doc.update(d)
        self.ga.relay_call = lambda *a, **k: (calls.append(a), (202, {"ok": True}))[1]
        self.ga.deploy_pins = lambda: self.pins
        self.ga.audit = lambda *a, **k: None
        try:
            code = self.ga.Handler.api_post_finish_update(
                H(), "/api/finish-update/send", {}, {"slug": "jules", "message_sha": want})
        finally:
            self.ga.read_finish, self.ga.write_json, self.ga.relay_call, self.ga.deploy_pins, self.ga.audit = saved
        self.assertEqual(code, 202, sent)
        self.assertEqual(len(calls), 1)
        return calls[0][2]

    def _check(self, placeholder):
        _, row = self._rows()
        text, want = row["message"], row["instr"]
        self.assertIn(" --owner '%s'" % OWNER, text)
        legacy, cmd = self._legacy_command(text, placeholder)
        have = fin.gs_routine.instr_hash(legacy)
        self.assertNotEqual(have, want)
        _, pre = self._rows({"release": fin.REL, "instr": have, "helper": "0.11.7", "computer": "f" * 16})
        _, odd = self._rows({"release": fin.REL, "instr": "0" * 16, "helper": "0.11.7", "computer": "f" * 16})
        self.assertEqual(odd["state"], "different")
        with self.subTest("placeholder is recoverable"):
            self.assertNotEqual(pre["state"], "different")
            self.assertEqual(pre["state"], "needs")
            self.assertNotEqual(pre["fail"], "The bot's instructions don't match this release.")
        with self.subTest("update_bot delivers the owner"):
            body = self._send_owner(want)
            self.assertEqual(body.get("owner"), OWNER)
            self.assertEqual(fin.gs_routine.owner_name(body["owner"]), OWNER)
            self.assertNotIn("'", body["owner"])
        ran = self._run(cmd, OWNER)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        adopted = ran.stdout.split("BEGIN INSTRUCTIONS\n", 1)[1].split("\nEND INSTRUCTIONS", 1)[0]
        self.assertIn(" --owner '%s'" % OWNER, adopted)
        self.assertNotIn(" --owner 'your owner'", adopted)
        line = fin.status(ran.stdout)
        rel, instr, helper, comp = re.fullmatch(
            r"(\S+) instr=(\S+) helper=(\S+) computer=(\S+)", line).groups()
        _, after = self._rows({"release": rel, "instr": instr, "helper": helper, "computer": comp})
        with self.subTest("after update"):
            self.assertEqual(adopted, text)
            self.assertEqual(instr, want)
            self.assertEqual(after["state"], "done")
            self.assertNotEqual(after["state"], "different")

    def test_command_without_owner_matches(self):
        self._check(placeholder=False)

    def test_placeholder_owner_flag_matches(self):
        self._check(placeholder=True)

    def test_unsafe_owner_is_rejected(self):
        """A delivered owner that fails owner_name stays the placeholder and is not spliced into the command."""
        _, row = self._rows()
        _, cmd = self._legacy_command(row["message"], True)
        bad = "Owner'; echo injected"
        self.assertEqual(fin.gs_routine.owner_name(bad), "your owner")
        ran = self._run(cmd, bad)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertNotIn("injected", ran.stdout)
        self.assertNotIn("injected", ran.stderr)
        adopted = ran.stdout.split("BEGIN INSTRUCTIONS\n", 1)[1].split("\nEND INSTRUCTIONS", 1)[0]
        self.assertIn(" --owner 'your owner'", adopted)
        self.assertNotIn(bad, adopted)


if __name__ == "__main__":
    unittest.main()
