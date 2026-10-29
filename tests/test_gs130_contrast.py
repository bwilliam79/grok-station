"""0.13.0: the two admin colors that failed WCAG AA (Noor's audit rev2).

--gs-faint was #6F655A (3.24:1 as text); it is now #998C7D (at least 4.5:1 on every admin background).
Input and select borders used --gs-rule-2 #3A3028 (1.53:1); they now use the new --gs-field-edge #806F60
(at least 3:1). --gs-rule-2 stays for dividers only."""
import os, re, unittest

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin", "web")
BACKGROUNDS = ("--gs-bg", "--gs-surface", "--gs-surface-2", "--gs-sidebar")


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b):
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


class Contrast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = open(os.path.join(WEB, "admin.css"), encoding="utf-8").read()
        cls.vars = {k: v.upper() for k, v in re.findall(r"(--gs-[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})", cls.css)}

    def test_exact_values(self):
        self.assertEqual(self.vars["--gs-faint"], "#998C7D")
        self.assertEqual(self.vars["--gs-field-edge"], "#806F60")
        self.assertEqual(self.vars["--gs-rule-2"], "#3A3028")  # unchanged: dividers

    def test_thresholds_on_every_background(self):
        for bg in BACKGROUNDS:
            self.assertGreaterEqual(contrast(self.vars["--gs-faint"], self.vars[bg]), 4.5, bg)
            self.assertGreaterEqual(contrast(self.vars["--gs-field-edge"], self.vars[bg]), 3.0, bg)

    def test_inputs_and_selects_use_the_field_edge(self):
        rule = re.search(r"\.sel,\.inp\{([^}]*)\}", self.css).group(1)
        self.assertIn("border:1px solid var(--gs-field-edge)", rule)
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", self.css):
            sel, body = m.group(1), m.group(2)
            if re.search(r"(^|[\s,])(input|select|textarea|\.inp|\.sel)\b", sel) and "border" in body:
                self.assertNotIn("--gs-rule-2", body, sel)

    def test_old_colors_gone(self):
        for name in ("admin.css", "app.js", "index.html"):
            self.assertNotIn("6F655A", open(os.path.join(WEB, name), encoding="utf-8").read().upper(), name)


if __name__ == "__main__":
    unittest.main()
