"""NIX-1313-10 (Noor copy): a failed wake-word delete says so with a text warning sign, in a polite live region
next to the row."""
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

MSG = "⚠ Couldn't delete that wake word. It's still saved. Try again in a minute."
NOT_ROOT = unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")


class DeleteCopy(unittest.TestCase):
    @NOT_ROOT
    def test_failed_delete_returns_exact_copy(self):
        import test_admin
        ga = test_admin.ga
        c = test_admin.signed_in()
        edir = os.path.dirname(ga.ENROLLED)
        os.makedirs(edir, exist_ok=True)
        ga.write_json(ga.ENROLLED, {"words": [{"name": "hey_dene", "persona": "dene", "kind": "fewshot"}]}, 0o640)
        os.chmod(edir, 0o555)
        try:
            with redirect_stdout(io.StringIO()):
                st, body = c.req("DELETE", "/api/wakewords/hey_dene", {})
        finally:
            os.chmod(edir, 0o755)
            ga.write_json(ga.ENROLLED, {"words": []}, 0o640)
        self.assertEqual(st, 500, body)
        self.assertEqual(body.get("error"), MSG)


class DeleteLiveRegion(unittest.TestCase):
    def setUp(self):
        src = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        start = src.index("function paintUnused(")
        self.fn = src[start:src.index("\nfunction ", start + 1)]

    def test_row_has_polite_status_region(self):
        self.assertRegex(self.fn, r'<div[^>]*role="status"[^>]*aria-live="polite"[^>]*data-umsg="\$\{esc\(w\.name\)\}"')

    def test_failure_text_goes_into_the_rows_region(self):
        self.assertRegex(self.fn, r'data-umsg[\s\S]*textContent\s*=\s*\(r && r\.data && r\.data\.error\)')


if __name__ == "__main__":
    unittest.main()
