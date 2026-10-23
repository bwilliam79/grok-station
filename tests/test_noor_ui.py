"""Noor's design.md refresh (2026-10-22): no red in the admin, bots page copy, refresh clock sizes."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class NoRedInAdmin(unittest.TestCase):
    def test_admin_css_has_no_red(self):
        css = read("admin", "web", "admin.css")
        self.assertNotIn("--gs-red", css)
        for red in ("#FF5A52", "#FF4D4D", "#3A1513", "#5A2420", "#2A0705"):
            self.assertNotIn(red.lower(), css.lower())

    def test_errors_and_danger_are_amber(self):
        css = read("admin", "web", "admin.css")
        for sel in (".err{", ".btn.d{", ".btn.dg{", ".bad{"):
            rule = css[css.index(sel):css.index("}", css.index(sel))]
            self.assertIn("--gs-amber", rule, sel)

    def test_error_box_says_needs_attention(self):
        js = read("admin", "web", "app.js")
        m = re.search(r"function errBox\(el, msg\) \{(.*)\}", js)
        self.assertIn("Needs attention.", m.group(1))


class BotsCopy(unittest.TestCase):
    def test_bots_page_intro(self):
        js = read("admin", "web", "app.js")
        self.assertIn("You can connect other bots from your Grok Bot account.", js)
        self.assertIn("No other bots yet", js)
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
