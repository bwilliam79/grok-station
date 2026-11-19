"""0.13.46: the Ready to talk panel does not list a Hey chip for every bot."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReadyPanel(unittest.TestCase):
    def test_the_home_panel_has_no_per_bot_chips(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        home = js.split("async function pageHome", 1)[1].split("async function pageXai", 1)[0]
        self.assertIn("Ready to talk", home)
        self.assertIn("Voice is off", home)
        self.assertNotIn('class="chip"', home)
        self.assertNotIn("WORD(", home)
        # Wake-word management still builds the Hey label. Only this panel dropped the chips.
        admin = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
        self.assertIn('row["label"] = "Hey " + names[slug]', admin)


if __name__ == "__main__":
    unittest.main()
