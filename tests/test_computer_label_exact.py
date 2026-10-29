"""0.12.9: the computer name is stored and matched exactly as the bot reports it; only the screen shortens it.

The Grok Bot app gives bots a full label (alpha.example.lan, or alpha.local on a fresh install) while its own
Computers list shows alpha. Bots use the full label to find their computer, so the station must keep it exactly.
Names here are made up.
"""
import json, os, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_readd_bot_e2e as e2e  # noqa: E402

JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def js_display(names):
    start = JS.index("const DNS_LABEL")
    end = JS.index("\n}\n", JS.index("function displayComputer")) + 3
    prog = JS[start:end] + "\nconsole.log(JSON.stringify(%s.map(displayComputer)));" % json.dumps(names)
    return json.loads(subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout)


class ExactLabel(e2e.AddBotEndToEnd):
    test_fresh_add_is_not_behind = test_readd_after_delete_is_not_behind = None
    test_rekey_without_delete_drops_the_old_status = None

    def test_labels_round_trip_exactly(self):
        for slug, host in (("alpha", "alpha.example.lan"), ("alphb", "alpha.local"), ("beta", "beta-desktop"),
                           ("gamma", "iPhone Air")):
            self.add_bot(slug, host)
            st, j = self.c.req("GET", "/api/bots")
            b = next(x for x in j["bots"] if x["slug"] == slug)
            self.assertEqual(b["reply_host"], host)
            r, v = self.row(slug)
            g = next(g for g in v["computers"] if any(x["slug"] == slug for x in g["bots"]))
            self.assertEqual(g["key"], "h:" + host)  # Finish update groups and matches on the exact name
            self.assertEqual(g["label"], host)
            st, _ = self.c.req("POST", "/api/finish-update/updater", {"computer": g["key"], "slug": slug})
            self.assertEqual(st, 200)
            st, _ = self.c.req("PATCH", "/api/bots/" + slug, {"reply_host": host})  # Change computer keeps it
            self.assertEqual(st, 200)
            st, j = self.c.req("GET", "/api/bots")
            self.assertEqual(next(x for x in j["bots"] if x["slug"] == slug)["reply_host"], host)


class DisplayOnly(unittest.TestCase):
    def test_display_shortens_dns_names_only(self):
        self.assertEqual(js_display(["alpha.example.lan", "alpha.local", "iPhone Air", "beta-desktop", "a b.c"]),
                         ["alpha", "alpha", "iPhone Air", "beta-desktop", "a b.c"])
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay
        self.assertEqual([relay.display_host(x) for x in ("alpha.local", "iPhone Air", "beta-desktop")],
                         ["alpha", "iPhone Air", "beta-desktop"])

    def test_screen_keeps_full_name_in_hover_and_edit_field(self):
        # 0.13.8: the row's second line is built by botRowL2HTML, which still puts the full name in the hover title.
        self.assertIn('<div class="help" title="${esc(title || "")}">', JS)
        self.assertIn('botRowL2HTML(repliesFrom(b), (b && b.reply_host) || "",', JS)
        self.assertIn('id="chname" maxlength="40" autocomplete="off" spellcheck="false" value="${esc(host)}"', JS)

    def test_instructions_ask_for_the_exact_label(self):
        for fn in ("function routineChangeComputer", "function routineForBot"):
            body = JS.split(fn, 1)[1][:6000]
            self.assertIn("exactly as it shows in the Grok Bot app", body)
            self.assertIn("dots included. Do not shorten it.", body)
            self.assertNotIn("hostname is fine", body)
            self.assertNotIn("Do not include the hostname", body)


if __name__ == "__main__":
    unittest.main()
