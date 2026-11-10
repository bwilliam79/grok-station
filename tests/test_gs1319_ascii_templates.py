"""0.13.19: bot templates must stay pure printable ASCII.

Bots still on 0.13.17 run a pinned gs-update.sh that opens the new
routine-*.txt with encoding="ascii". Any non-ASCII byte makes update_bot fail
closed, so every shipped template copy must decode as ASCII.
"""
import glob, os, re, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "admin"))


def _printable(text):
    return all(c == "\n" or " " <= c <= "~" for c in text)


class AsciiTemplates(unittest.TestCase):
    def test_every_routine_file_opens_as_ascii(self):
        files = sorted(glob.glob(os.path.join(ROOT, "admin", "routine-*.txt")))
        self.assertEqual([os.path.basename(f) for f in files], ["routine-home.txt"])  # 0.13.37: home only
        for path in files:
            with self.subTest(path=os.path.basename(path)):
                with open(path, encoding="ascii") as fh:  # the 0.13.17 opener
                    text = fh.read()
                self.assertTrue(_printable(text))

    def test_no_owner_token_for_017_updater(self):
        # 0.13.17 render() does not fill {owner}; keep the literal fallback.
        for path in glob.glob(os.path.join(ROOT, "admin", "routine-*.txt")):
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("{owner}", fh.read(), path)

    def test_embedded_templates_ascii(self):
        import gs_routine
        for mode, body in gs_routine.TEMPLATES.items():
            self.assertTrue(_printable(body), mode)
            self.assertNotIn("{owner}", body, mode)
        with open(os.path.join(ROOT, "scripts", "gs-update.sh"), encoding="utf-8") as fh:
            fh.read().encode("ascii")

    def test_printable_check_is_pure_ascii(self):
        import gs_routine
        t = gs_routine.TEMPLATES["home"]
        for ch in "\u2019\u201c\u201d":
            with self.assertRaises(gs_routine.Bad):
                gs_routine.render(t.replace("Station updates.", "Station" + ch + " updates.", 1),
                                  "home", "otto", "a" * 40, "b" * 64)


if __name__ == "__main__":
    unittest.main()
