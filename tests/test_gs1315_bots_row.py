"""0.13.15 Bots row (Noor bots-row-fix SPEC rev3) and sent state copy (bots-row-sent rev1). Source checks."""
import os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()


class Rev3(unittest.TestCase):
    def test_configure_is_last_in_row_dom(self):
        row = JS[JS.index('return `<div class="botrow${note'):]
        row = row[:row.index("}).join(\"\")")]
        self.assertLess(row.index('class="updslot"'), row.index(">Configure</button>"))
        self.assertLess(row.index('<div class="updcol">'), row.index('<div class="cfg">'))
        self.assertRegex(row, r'>Configure</button></div></div>')

    def test_no_updates_caption_at_rest(self):
        self.assertIn('rule.textContent = failed ? UPD_FAIL : "";', JS)
        self.assertNotIn("rule.textContent = failed ? UPD_FAIL : updatesRuleLine(fu);", JS)

    def test_shared_grid_on_row_class(self):
        self.assertIn("#blist .botrow{display:grid;grid-template-columns:[bot] minmax(240px,1fr) [wake] 200px [upd] 144px [act] 120px;", CSS)
        self.assertIn("#blist .botrow>.cfg{grid-column:act;grid-row:1;justify-self:end;", CSS)
        self.assertIn("#blist .botrow .ww .phrase{min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}", CSS)
        self.assertIn('.updcol .updsw{display:grid;grid-template-rows:32px 16px}', CSS)

    def test_truncated_wake_has_tooltip(self):
        self.assertIn('<span class="phrase${on ? "" : " muted"}" title="“${esc(label)}”">', JS)


class SentCopy(unittest.TestCase):
    def test_labels(self):
        self.assertIn('" \\u00b7 waiting for its reply"', JS)
        self.assertIn('"Setup request sent to " + bot', JS)
        self.assertIn(">Send request again</button>", JS)
        self.assertIn(">Enter details myself</button>", JS)
        self.assertIn("' aria-label=\"Send the setup request to ' + bot + ' again\"'", JS)
        self.assertNotIn(">Resend</button>", JS)
        self.assertNotIn(">Paste values</button>", JS)
        self.assertIn("they fill in here on their own. If it's taking a while, send the request again or enter the details yourself.", JS)

    def test_order_again_then_details(self):
        i = JS.index('<span class="setacts sent">')
        seg = JS[i:i + 700]
        self.assertLess(seg.index("Send request again"), seg.index("Enter details myself"))

    def test_throttle_shown(self):
        self.assertIn('" You can send it again in " + wait + " min."', JS)
        self.assertIn('"\\u26a0 Still waiting for " + (bot || "this bot") + ". You can send the request again in "', JS)
        self.assertRegex(JS, r"m\.sendBlocked \|\| wait \? ' aria-disabled=\"true\"'")


if __name__ == "__main__":
    unittest.main()
