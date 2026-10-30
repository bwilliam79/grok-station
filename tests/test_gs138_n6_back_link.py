"""Noor gs-voice-train rev1.1 call 6: the Integrations back link (a.iback) has a tap
target of at least 44 px at every width, and its phone text is at least 15 px."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "admin", "web", "admin.css")


def _px(value, tokens):
    value = value.strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        value = tokens[m.group(1)]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", value.strip())
    return float(m.group(1)) if m else None


class BackLinkTarget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CSS, encoding="utf-8") as fh:
            cls.css = fh.read()
        cls.tokens = dict(re.findall(r"(--[\w-]+):([^;}]+)", cls.css.split("}", 1)[0]))

    def _rules(self, phone):
        """Bodies of a.iback rules: top level (phone=False) or inside a max-width:767px @media."""
        css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        out = []
        for m in re.finditer(r"(?:^|[\s}])a\.iback\{([^}]*)\}", css):
            prefix = css[:m.start() + 1]
            depth = prefix.count("{") - prefix.count("}")
            media = prefix[prefix.rfind("@media"):] if depth > 0 else ""
            in_phone = "max-width:767px" in media.split("{", 1)[0]
            if in_phone == phone:
                out.append(m.group(1))
        return out

    def _decl(self, body, prop):
        m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*([^;]+)", body)
        return m.group(1) if m else None

    def test_desktop_target_is_32(self):
        # 0.13.9 fix round item 6: the ghost back button is 32 px on desktop (24 px minimum), 44 px on phone.
        base = self._rules(phone=False)
        self.assertTrue(base)
        heights = [_px(self._decl(b, "min-height"), self.tokens) for b in base if self._decl(b, "min-height")]
        self.assertEqual(heights, [32.0])

    def test_phone_target_44_and_text_15(self):
        ph = self._rules(phone=True)
        self.assertTrue(ph)
        body = ph[-1]
        self.assertGreaterEqual(_px(self._decl(body, "min-height"), self.tokens), 44)
        self.assertGreaterEqual(_px(self._decl(body, "font-size"), self.tokens), 15)


if __name__ == "__main__":
    unittest.main()
