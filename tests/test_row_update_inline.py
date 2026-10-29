"""0.12.9: the row Update button and Update all send in place, with no Finish update dialog.

Server: the send records a failure reason the row can show after a reload; a waiting row is the pending state;
a matching signed status is done. Page: the row shows a spinner while waiting, nothing on success, a failure line
and the Update button again on failure. Only a pre-0.11.8 (copy) bot still opens the dialog. No browser.
"""
import json, os, subprocess, sys, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_readd_bot_e2e as e2e  # noqa: E402

JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def page(view, sending=None):
    """Render the row pieces with the real functions from app.js against a server view."""
    start, end = JS.index("const ROW_SENDING"), JS.index("async function rowUpdateSend")
    notes = JS[JS.index("function finishNotes"):JS.index("function paintBots")]
    prog = """
const esc = s => String(s); const ICO_TRI = "";
const FINISH = {view: %s};
function finishFind(v, slug) { for (const g of v.computers) for (const b of g.bots) if (b.slug === slug) return b; return null; }
function finishNeedsUpdate(b) { const r = finishFind(FINISH.view, b.slug); return !!(r && r.state !== "done"); }
function helperBehind() { return false; }
%s
%s
Object.assign(ROW_SENDING, %s);
const b = {slug: "alpha"};
console.log(JSON.stringify({btn: updateRowBtnHTML(b), notes: finishNotes(b)}));
""" % (json.dumps(view), JS[start:end], notes, json.dumps(sending or {}))
    return json.loads(subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout)


def view(state, fail=""):
    return {"computers": [{"bots": [{"slug": "alpha", "state": state, "fail": fail, "instr": "a" * 16}]}]}


class RowPage(unittest.TestCase):
    def test_pending_shows_spinner_and_no_note(self):
        for v, s in ((view("waiting"), None), (view("needs"), {"alpha": True})):
            got = page(v, s)
            self.assertIn("Updating…", got["btn"]); self.assertIn('class="spin"', got["btn"])
            self.assertNotIn('data-upd="', got["btn"]); self.assertEqual(got["notes"], "")

    def test_success_clears_everything(self):
        self.assertEqual(page(view("done")), {"btn": "", "notes": ""})

    def test_failure_shows_reason_and_button(self):
        for st, why in (("refused", "The bot refused the update."), ("noreply", "No reply within 10 minutes."),
                        ("needs", "Asked a few minutes ago. Wait, then try again."), ("needs", "Couldn't reach the bot.")):
            got = page(view(st, why))
            self.assertIn("data-upd", got["btn"])
            self.assertIn("Update failed: " + why, got["notes"])
            self.assertNotIn("Update available", got["notes"])

    def test_row_and_update_all_do_not_open_the_dialog(self):
        h = JS.split('list.querySelectorAll("[data-upd]")', 1)[1].split("rowUpdatePoll();", 1)[0]
        self.assertIn("rowUpdateStart([btn.dataset.upd])", h)
        self.assertEqual(h.count("openFinishDialog"), 1)  # the row; Update all is the callout
        self.assertIn('row.state === "copy") return openFinishDialog', h)
        chrome = JS.split("function paintBotCallout", 1)[1].split("function bindBotMenu", 1)[0]
        self.assertIn('rows.some(r => r.state === "copy")) openFinishDialog({all: true})', chrome)
        self.assertIn("rowUpdateStart(send)", chrome)
        self.assertIn('api("POST", "/api/finish-update/send"', JS.split("async function rowUpdateSend", 1)[1][:600])


class RowServer(e2e.AddBotEndToEnd):
    test_fresh_add_is_not_behind = test_readd_after_delete_is_not_behind = None
    test_rekey_without_delete_drops_the_old_status = None

    def _older(self):
        self.add_bot("alpha", "alpha.local")
        doc = e2e.ga.read_finish(); doc["setup"]["alpha"]["release"] = "0.12.0"
        e2e.ga.write_json(e2e.ga.path(e2e.ga.FINISH), doc)
        r, _ = self.row("alpha"); self.assertEqual(r["state"], "needs")
        return r

    def test_send_failure_is_kept_for_the_row_then_cleared(self):
        r = self._older()
        real = e2e.ga.relay_call
        e2e.ga.relay_call = lambda m, p, *a, **k: (502, {"error": "x"}) if p.endswith("/update") else real(m, p, *a, **k)
        st, _ = self.c.req("POST", "/api/finish-update/send", {"slug": "alpha", "message_sha": r["instr"]})
        self.assertEqual(st, 502)
        r2, _ = self.row("alpha")  # a reload reads this from the server
        self.assertEqual((r2["state"], r2["fail"]), ("needs", "Couldn't reach the bot."))
        e2e.ga.relay_call = lambda m, p, *a, **k: (429, {}) if p.endswith("/update") else real(m, p, *a, **k)
        self.c.req("POST", "/api/finish-update/send", {"slug": "alpha", "message_sha": r["instr"]})
        self.assertEqual(self.row("alpha")[0]["fail"], "Asked a few minutes ago. Wait, then try again.")
        e2e.ga.relay_call = lambda m, p, *a, **k: (202, {"ok": True}) if p.endswith("/update") else real(m, p, *a, **k)
        st, _ = self.c.req("POST", "/api/finish-update/send", {"slug": "alpha", "message_sha": r["instr"]})
        self.assertEqual(st, 202)
        r3, _ = self.row("alpha")
        self.assertEqual((r3["state"], r3["fail"]), ("waiting", ""))  # pending survives a reload
        st, j = self.c.req("POST", "/api/finish-update/send", {"slug": "alpha", "message_sha": r["instr"]})
        self.assertEqual(st, 409)  # the 10-minute reply window still guards a second click

    def test_signed_status_is_success(self):
        r = self._older()
        json.dump({"alpha": {"release": open(os.path.join(e2e.ROOT, "VERSION")).read().strip(), "instr": r["instr"],
                             "helper": "-", "computer": "-", "at": int(time.time()) + 5}},
                  open(self.relay.r.UPDATES_FILE, "w"))
        r2, v = self.row("alpha")
        self.assertEqual((r2["state"], r2["fail"], v["behind"]), ("done", "", 0))


if __name__ == "__main__":
    unittest.main()
