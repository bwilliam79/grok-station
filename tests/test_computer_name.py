"""Add bot and Change computer ask for the computer name as the Grok Bot app shows it, never a hostname (0.12.8).

Regression: 0.12.6 (795d651) told bots to reply with the name they see ("its hostname is fine") and cut the
pasted value at the first dot, so a bot sent the station-side hostname instead of the Grok Bot app's label.
"""
import os
import re
import unittest

JS = open(os.path.join(os.path.dirname(__file__), "..", "admin", "web", "app.js"), encoding="utf-8").read()


def _between(a, b):
    return JS.split(a, 1)[1].split(b, 1)[0]


class ComputerNameInstructions(unittest.TestCase):
    def test_instructions_ask_for_grok_bot_app_name_not_hostname(self):
        for fn in (_between("function routineChangeComputer", "function openChangeComputer"),
                   _between("function routineForBot", "function addBot")):
            self.assertIn("exactly as it shows in the Grok Bot app", fn)
            self.assertIn("dots included. Do not shorten it.", fn)  # 0.12.9: the exact label, never trimmed
            self.assertNotIn("hostname is fine", fn)
            self.assertNotIn("as you see it", fn)
            self.assertIn("<computer name>", fn)

    def test_hint_and_fields_keep_the_typed_name(self):
        hint = re.search(r'const COMPUTER_HINT = "([^"]*)"', JS).group(1)
        self.assertIn("Grok Bot app", hint)
        self.assertIn("dots included", hint)
        self.assertNotIn("shortComputerName", JS)  # 0.12.6's stored trimming; 0.12.9 trims only on screen
        self.assertNotIn("bindComputerName", JS)
        self.assertNotIn("gethostname", JS)


if __name__ == "__main__":
    unittest.main()
