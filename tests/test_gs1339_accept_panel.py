"""0.13.39: the Accept panel shows the fingerprint for a pending teammate, and hides Accept without one.

A bot that is not on the station's Bots list (removed, then Set up again from the imported names) is built
by the pending branch of setupListRows. That branch omitted relay_fp, so the review panel rendered an empty
Fingerprint and still offered Accept. Fails on 0.13.38.
"""
import json, os, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys_path = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, sys_path)
from test_gs1311_bots_merge import FAKE_DOM, _fn, _icons  # noqa: E402

FP = "0123456789abcdef"
GROUPED = "0123 4567 89ab cdef"
NO_FP = "The station has no fingerprint for this key yet. Reload, or press Set up again."
def _names():
    # setupRelayFields is the shared helper added with the fix. The old list inlined the fields
    # (and skipped them on a pending teammate), so the before run must still load.
    with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    names = ["botIdFromName", "pendingTeammates", "setupRelayState", "setupComputerSuggestions",
             "setupMainComputer", "setupListRows", "formatKeyFp", "setupKeyReviewEl"]
    if "function setupRelayFields(" in js:
        names.insert(names.index("setupListRows"), "setupRelayFields")
    return names


def _run(expr):
    src = "\n".join(_fn(n) for n in _names())
    prog = (
        "const BOT_ID_RE = /^[a-z][a-z0-9-]{1,23}$/;\n"
        "const esc = s => String(s == null ? '' : s);\n"
        + _icons() + "\n" + FAKE_DOM + "\n" + src + "\n"
        "function walk(n, acc){ acc.push(n); n.children.forEach(c => walk(c, acc)); return acc; }\n"
        "process.stdout.write(JSON.stringify(" + expr + "));"
    )
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr or out.stdout)
    return json.loads(out.stdout)


def _panel(row):
    return _run(
        "(function(){ const n = setupKeyReviewEl(%s, ''); const nodes = walk(n, []);"
        " const acc = nodes.filter(x => x.attrs && x.attrs['data-pair']==='accept')[0];"
        " const rej = nodes.filter(x => x.attrs && x.attrs['data-pair']==='reject')[0];"
        " return {text: n.textContent, acceptDisabled: !!(acc && (acc.disabled || acc.attrs['aria-disabled']==='true')),"
        " rejectDisabled: !!(rej && (rej.disabled || rej.attrs['aria-disabled']==='true'))}; })()"
        % json.dumps(row)
    )


class PendingAcceptPanel(unittest.TestCase):
    def el(self, relay_fp):
        return {
            "_bots": [{"slug": "juno", "name": "Juno", "main": True, "reply_host": "den"}],
            "_imported": {"rows": [{"name": "Nix", "state": "new"}]},
            "_setup": {"owner": "Sam", "rows": {"nix": {
                "name": "Nix", "status": "sent", "sent_at": 10, "relay": "review",
                "relay_fp": relay_fp, "paired_computer": "Den iMac", "holds_setup": True,
            }}},
        }

    def test_pending_review_row_carries_the_fingerprint_into_the_panel(self):
        rows = _run("setupListRows(%s)" % json.dumps(self.el(FP)))
        nix = [r for r in rows if r.get("slug") == "nix"]
        self.assertEqual(len(nix), 1, rows)
        row = nix[0]
        self.assertTrue(row.get("pending"))
        self.assertEqual(row.get("relay"), "review")
        self.assertEqual(row.get("relay_fp"), FP)
        self.assertEqual(row.get("paired_computer"), "Den iMac")
        self.assertIs(row.get("holds_setup"), True)
        panel = _panel(row)
        self.assertIn(GROUPED, panel["text"])
        self.assertFalse(panel["acceptDisabled"])
        self.assertFalse(panel["rejectDisabled"])

    def test_empty_or_bad_fingerprint_disables_accept_and_keeps_reject(self):
        for raw in ("", "abcd", "0123456789abcdeG", "0123456789abcde"):
            panel = _panel({"slug": "nix", "name": "Nix", "relay_fp": raw})
            self.assertIn(NO_FP, panel["text"], raw)
            self.assertNotIn(GROUPED, panel["text"], raw)
            self.assertTrue(panel["acceptDisabled"], raw)
            self.assertFalse(panel["rejectDisabled"], raw)


if __name__ == "__main__":
    unittest.main()
