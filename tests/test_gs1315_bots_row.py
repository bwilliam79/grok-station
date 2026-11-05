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
    """Noor 0.13.15 QA: one helper line, countdown in the disabled button, 409 replaces the helper, fixed height."""
    def test_labels(self):
        self.assertIn('"Setup request sent to " + main', JS)
        self.assertIn('" \\u00b7 waiting for its reply"', JS)
        self.assertIn('bot + " is installing from the setup it already holds."', JS)
        self.assertNotIn("send the request again or enter the details yourself", JS)
        self.assertIn(">Enter details myself</button>", JS)
        self.assertNotIn(">Resend</button>", JS)

    def test_countdown_in_button(self):
        self.assertIn('(wait ? "Send again in " + wait + " min" : "Send request again")', JS)
        self.assertIn('" again, available in " + wait + " minutes"', JS)
        self.assertNotIn("You can send it again in", JS)

    def test_throttle_replaces_helper(self):
        self.assertIn('"\\u26a0 Still waiting for " + (bot || "this bot") + ". Try again in " + Math.max(1, mins | 0) + " min."', JS)
        self.assertIn("(thr ? '<div class=\"shelp sthrottle help\" role=\"status\">' + esc(thr) + \"</div>\"", JS)

    def test_fixed_sent_height_and_compact_phone_configure(self):
        self.assertIn("#blist .botrow.sentrow{height:100px;min-height:100px;max-height:100px;overflow:hidden}", CSS)
        # 0.13.16 Noor: helper wraps to 2 lines at desktop so the bot name is not ellipsized
        self.assertIn("#blist .botrow.sentrow .l2 .shelp{white-space:normal;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-height:16px}", CSS)
        self.assertNotIn("#blist .botrow.sentrow .l2 .shelp{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}", CSS)
        self.assertIn("#blist .botrow.sentrow>.cfg .btn{flex:0 0 auto;width:auto}", CSS)


if __name__ == "__main__":
    unittest.main()
