"""Noor's design.md refresh (2026-10-22): no red in the admin, bots page copy, refresh clock sizes."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class NoRedInAdmin(unittest.TestCase):
    def test_admin_red_is_only_the_offline_status_ring(self):
        """0.12.6: --gs-red is the offline bot-avatar ring. Errors stay amber. Other reds stay out."""
        css = read("admin", "web", "admin.css")
        self.assertEqual(css.lower().count("#ff5a52"), 1)
        self.assertIn("--gs-red:#FF5A52", css.replace(" ", ""))
        uses = [ln.strip() for ln in css.splitlines() if "var(--gs-red)" in ln and not ln.strip().startswith("/*")]
        self.assertEqual(uses, [
            ".avwrap.st.on{--st-c:var(--gs-teal)}.avwrap.st.offl{--st-c:var(--gs-red)}.avwrap.st.chk{--st-c:var(--gs-faint)}",
        ])
        for red in ("#FF4D4D", "#3A1513", "#5A2420", "#2A0705"):
            self.assertNotIn(red.lower(), css.lower())
        for sel in (".err{", ".btn.d{", ".btn.dg{", ".bad{", ".need{"):
            rule = css[css.index(sel):css.index("}", css.index(sel))]
            self.assertNotIn("--gs-red", rule, sel)
            self.assertNotIn("#FF5A52", rule.upper(), sel)

    def test_errors_and_danger_are_amber(self):
        css = read("admin", "web", "admin.css")
        for sel in (".err{", ".btn.d{", ".btn.dg{", ".bad{"):
            rule = css[css.index(sel):css.index("}", css.index(sel))]
            self.assertIn("--gs-amber", rule, sel)

    def test_error_box_says_needs_attention(self):
        js = read("admin", "web", "app.js")
        m = re.search(r"function errBox\(el, msg\) \{(.*)\}", js)
        self.assertIn("withAttn(msg)", m.group(1))
        helper = re.search(r"function withAttn\(msg\) \{(.*)\}", js).group(1)
        self.assertIn('"Needs attention. "', helper)  # every error but a password failure


class BotsCopy(unittest.TestCase):
    def test_bots_page_intro(self):
        js = read("admin", "web", "app.js")
        self.assertNotIn("No other bots yet", js)
        self.assertNotIn("You can connect other bots from your Grok Bot account.", js)
        self.assertIn("Hey Grok", js)
        self.assertNotIn("Teammates the station can talk to.", js)


class RefreshSizes(unittest.TestCase):
    def test_idle_and_night_clock(self):
        src = read("app", "grok_station.py")
        self.assertIn("clock_px = L(300)", src)
        self.assertNotIn("clock_px = L(320)", src)
        self.assertIn('font("night", L(440))', src)
        self.assertNotIn('font("night", L(340))', src)


if __name__ == "__main__":
    unittest.main()
