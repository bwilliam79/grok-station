"""0.13.24: the updater still pinned at 0.13.22 accepts these templates.

That updater (3d1fe24) requires each _REQUIRED_LINES entry as the start of a
line. 0.13.23 put "except owner" inside the safety line, so Update stopped
with "template safety line" and applied nothing.
"""
import ast
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = "3d1fe24"


def required_lines(rev=OLD):
    """_REQUIRED_LINES from admin/gs_routine.py at rev, the same tuple the updater checks."""
    src = subprocess.check_output(
        ["git", "-C", ROOT, "show", "%s:admin/gs_routine.py" % rev], text=True)
    start = src.index("_REQUIRED_LINES = (")
    snippet = src[start:]
    end = snippet.index(")\n") + 1
    value = ast.parse(snippet[:end]).body[0].value
    return tuple(elt.value for elt in value.elts)


class OldUpdaterAcceptsTemplates(unittest.TestCase):
    def test_01322_required_lines_start_a_line(self):
        sys.path.insert(0, os.path.join(ROOT, "admin"))
        import gs_routine
        lines = required_lines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(gs_routine._REQUIRED_LINES, lines)
        def read(name):
            with open(os.path.join(ROOT, "admin", name), encoding="utf-8") as fh:
                return fh.read()
        texts = {
            "routine-home.txt": read("routine-home.txt"),
            "template home": gs_routine.TEMPLATES["home"],
        }
        for name, text in texts.items():
            for line in lines:
                self.assertIn("\n" + line, text, "%s missing %r" % (name, line[:48]))


if __name__ == "__main__":
    unittest.main()
