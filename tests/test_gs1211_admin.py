#!/usr/bin/env python3
"""0.12.11 (Nix review of 0.12.10): header normalization only on header lines; a table-only match is
"unconfirmed"; every doubtful case fails closed to needs update. Names and keys are made up."""
import os, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_gs1210 as t10  # noqa: E402

ga, gs_routine = t10.ga, t10.gs_routine
sys.path.insert(0, os.path.join(ROOT, "admin"))
import bot_content  # noqa: E402


class AnchoredHeader(unittest.TestCase):
    def test_header_looking_text_elsewhere_changes_the_fingerprint(self):
        a = b"Grok Station instructions, release 0.12.9.\nsay: Grok Station instructions, release 0.12.9. done\n"
        b = b"Grok Station instructions, release 0.12.10.\nsay: Grok Station instructions, release 0.12.10. done\n"
        self.assertNotEqual(bot_content.content_fp(a, b"s", b"h"), bot_content.content_fp(b, b"s", b"h"))
        self.assertEqual(bot_content.normalize(b"Grok Station instructions, release 0.12.9.\n"),
                         b"Grok Station instructions, release X.Y.Z.\n")

    def test_gs_update_template_headers_still_normalized(self):
        line = b'    "home": r\'\'\'Grok Station instructions, release 0.12.9.\n'
        self.assertEqual(bot_content.normalize(line), b'    "home": r\'\'\'Grok Station instructions, release X.Y.Z.\n')
        script = open(os.path.join(ROOT, "scripts", "gs-update.sh"), "rb").read()
        self.assertEqual(bot_content.normalize(script).count(b"release X.Y.Z."), 2)


class _Rows(t10._Rel):
    def setUp(self):
        super().setUp()
        self.rel = ga.version()
        maj, mi, pa = (int(x) for x in self.rel.split("."))
        self.prev = "%d.%d.%d" % (maj, mi, pa - 1)
        self.pins = {"commit": t10.C_NEW, "update_commit": "3" * 40, "files": {"scripts/gs-update.sh": t10.S_NEW}}
        self._saved = bot_content

    def tearDown(self):
        super().tearDown()
        if hasattr(self, "_orig"):
            bot_content.read_table = self._orig

    _table = t10.NoFalseBehind._table
    _old_instr = t10.NoFalseBehind._old_instr

    def _row(self, instr, setup=None, rel=None):
        bot = {"slug": "kai", "name": "Kai", "paired": True, "webhook": {"configured": True}, "key_mode": "home",
               "reply_host": "box.example", "helper": {"version": "0.11.8", "out_of_date": False},
               "update": {"release": rel or self.prev, "instr": instr, "at": 100} if instr else {}}
        fin = {"sent": {}, "copied": {}, "updaters": {}, "setup": setup or {}, "failed": {}}
        return ga.finish_rows([bot], "https://st.example", "", "", pins=self.pins, finish=fin, now=200)["computers"][0]["bots"][0]


class Unconfirmed(_Rows):
    def test_table_only_match_is_unconfirmed(self):
        self._table(True)
        row = self._row(None, setup={"kai": {"release": self.prev, "at": 50}})
        self.assertEqual(row["state"], "done")
        self.assertIs(row["unconfirmed"], True)

    def test_signed_status_confirms(self):
        self._table(True)
        row = self._row(self._old_instr("kai"))
        self.assertEqual((row["state"], row["unconfirmed"]), ("done", False))


class FailClosed(_Rows):
    def _set(self, rows):
        self._orig = bot_content.read_table
        bot_content.read_table = lambda path=None: rows

    def test_newer_release_reported_is_not_called_same(self):
        self._table(True)
        maj, mi, pa = (int(x) for x in self.rel.split("."))
        newer = "%d.%d.%d" % (maj, mi, pa + 1)
        self.assertFalse(ga.same_content({"slug": "kai", "key_mode": "home"}, newer, None, "", "", ""))
        self.assertNotEqual(self._row("e" * 64, rel=newer)["state"], "done")

    def test_unknown_release_needs(self):
        self._table(True)
        self.assertEqual(self._row(self._old_instr("kai"), rel="9.x")["state"], "needs")

    def test_missing_or_empty_table_needs(self):
        self._set([])
        self.assertEqual(self._row(self._old_instr("kai"))["state"], "needs")
        self.assertEqual(self._row(None, setup={"kai": {"release": self.prev, "at": 50}})["state"], "needs")

    def test_no_row_for_current_release_needs(self):
        fp = "c" * 64
        self._set([{"release": self.prev, "update_commit": t10.C_OLD, "update_sha": t10.S_OLD, "fp": {"home": fp, "cloud": fp}}])
        self.assertEqual(self._row(self._old_instr("kai"))["state"], "needs")

    def test_on_disk_routine_differs_needs(self):
        self._table(True)
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "routine-home.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(gs_routine.TEMPLATES["home"] + "\nextra line\n")
        saved = dict(ga.ROUTINES)
        ga.ROUTINES["home"] = p
        try:
            self.assertIn(self._row(self._old_instr("kai"))["state"], ("needs", "error"))  # never done
        finally:
            ga.ROUTINES.clear(); ga.ROUTINES.update(saved)

    def test_missing_table_file_reads_empty(self):
        self.assertEqual(bot_content.read_table(os.path.join(tempfile.mkdtemp(), "none.txt")), [])


if __name__ == "__main__":
    unittest.main()


class SealReplyAbsolutePath(unittest.TestCase):
    """A routine shell's PATH may not include ~/.local/bin (exit 127 on a second bot): call the helper by path."""

    def test_templates_never_rely_on_a_bare_gs_seal_reply(self):
        import re
        texts = [open(os.path.join(ROOT, "admin", "routine-home.txt"), encoding="utf-8").read(), gs_routine.TEMPLATES["home"],
                 open(os.path.join(ROOT, "scripts", "gs-update.sh"), encoding="utf-8").read()]
        for t in texts:
            self.assertIn('"$HOME/.local/share/grok-station/bin/gs-seal-reply" reply', t)
            for line in t.splitlines():
                self.assertIsNone(re.search(r'(^|\| )gs-seal-reply ', line), line)


class FreshHomeBotHelper(unittest.TestCase):
    """A second persona added on a computer whose helper is installed has sent nothing yet: no helper warning."""

    def _bot(self, **kw):
        b = {"slug": "ash", "name": "Ash", "key_mode": "home", "reply_host": "box.example", "paired": True,
             "webhook": {"configured": True, "last4": "abcd"}, "status": {"state": "connected", "last_answered_at": None},
             "helper_version": None, "update": None}
        b.update(kw)
        return b

    def test_freshly_added_second_home_bot_has_no_warning(self):
        self.assertIs(ga.helper_view(self._bot())["out_of_date"], False)
        self.assertIs(ga.safe_bot(self._bot())["helper"]["out_of_date"], False)

    def test_bot_that_posted_without_a_version_is_unknown_not_old(self):
        # 0.12.12: a post without a helper version is not evidence of an old helper.
        b = self._bot(status={"state": "connected", "last_answered_at": "2026-10-27T10:00:00Z"})
        self.assertIs(ga.helper_view(b)["out_of_date"], False)
        self.assertEqual(ga.helper_view(b)["state"], "unknown")
        self.assertIs(ga.helper_view(self._bot(helper_version="0.11.7"))["out_of_date"], True)
        self.assertIs(ga.helper_view(self._bot(helper_version=ga.HELPER_RELEASE))["out_of_date"], False)
