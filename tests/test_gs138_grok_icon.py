#!/usr/bin/env python3
"""Grok status icon size and centring (gs138 / Noor rev1).

Rasterise the 24-unit SVGs (alpha > 0.5) and check the automatable rules:
sibling-mean height, bbox centre, ink centroid y, no transforms, glyph
stroke-width 2, ink inside 0-24. Badge states are measured on the glyph
group only (the halo is excluded).
"""
import io
import os
import re
import unittest

import numpy as np
from PIL import Image

try:
    import cairosvg
except ImportError:  # pragma: no cover
    cairosvg = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = os.path.join(ROOT, "assets", "status")
N = 480  # >= 240 px, same size the spec measured at
GROK = ("grok-online", "grok-no-internet", "grok-unreachable", "grok-checking", "grok-dim")
# Halo / badge lives outside the first <g>; measure that group only.
BADGE = {"grok-unreachable", "grok-checking"}


def _read(name):
    with open(os.path.join(STATUS, name + ".svg"), encoding="utf-8") as f:
        return f.read()


def _glyph_only(svg):
    """First <g>…</g> is the bubble + wave. Badges and the slash sit after it."""
    m = re.search(r"<g\b[^>]*>.*?</g>", svg, re.S)
    if not m:
        raise AssertionError("glyph group missing")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'width="24" height="24">' + m.group(0) + "</svg>")


def _raster(svg, n=N):
    svg = svg.replace('width="24"', f'width="{n}"', 1).replace('height="24"', f'height="{n}"', 1)
    png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=n, output_height=n)
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return np.asarray(im)[..., 3].astype(float) / 255.0


def measure(svg, n=N):
    """Ink box from alpha>0.5; centroid from the full alpha mass (spec build.py)."""
    a = _raster(svg, n)
    ys, xs = np.nonzero(a > 0.5)
    if xs.size == 0:
        raise AssertionError("no ink above alpha 0.5")
    u = 24.0 / n
    tot = float(a.sum())
    cx = float((a.sum(0) * np.arange(n)).sum() / tot * u)
    cy = float((a.sum(1) * np.arange(n)).sum() / tot * u)
    x0, x1 = float(xs.min() * u), float((xs.max() + 1) * u)
    y0, y1 = float(ys.min() * u), float((ys.max() + 1) * u)
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "w": x1 - x0, "h": y1 - y0,
            "bcx": (x0 + x1) / 2, "bcy": (y0 + y1) / 2,
            "cx": cx, "cy": cy}


@unittest.skipUnless(cairosvg is not None, "cairosvg not installed")
class GrokIconGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.online = measure(_read("grok-online"))
        cls.dim = measure(_read("grok-dim"))
        cls.mic = measure(_read("mic-ok"))
        cls.wifi = measure(_read("wifi-connected"))

    def test_height_matches_sibling_mean(self):
        mean = (self.mic["h"] + self.wifi["h"]) / 2.0
        for name, m in (("grok-online", self.online), ("grok-dim", self.dim)):
            self.assertLessEqual(abs(m["h"] - mean), 1.25, name)

    def test_bbox_centred_in_the_24_box(self):
        for name, m in (("grok-online", self.online), ("grok-dim", self.dim)):
            self.assertLessEqual(abs(m["bcx"] - 12), 0.5, name + " cx")
            self.assertLessEqual(abs(m["bcy"] - 12), 0.5, name + " cy")

    def test_centroid_y_inside_sibling_range(self):
        # Sibling centroid range is 10.9–11.8; the spec allows ±0.5 → 10.4–12.3.
        lo = min(self.mic["cy"], self.wifi["cy"]) - 0.5
        hi = max(self.mic["cy"], self.wifi["cy"]) + 0.5
        self.assertGreaterEqual(lo, 10.4 - 0.35)  # raster grain vs the spec's 10.4
        self.assertLessEqual(hi, 12.3 + 0.35)
        for name, m in (("grok-online", self.online), ("grok-dim", self.dim)):
            self.assertGreaterEqual(m["cy"], 10.4, name)
            self.assertLessEqual(m["cy"], 12.3, name)

    def test_every_grok_svg_has_no_transform_and_glyph_stroke_2(self):
        for name in GROK:
            svg = _read(name)
            self.assertNotIn("transform=", svg, name)
            glyph = re.search(r"<g\b([^>]*)>", svg)
            self.assertIsNotNone(glyph, name)
            attrs = glyph.group(1)
            self.assertRegex(attrs, r'stroke-width="2"', name)
            # Round caps and joins are part of the drop-in, not a separate state.
            self.assertIn('stroke-linecap="round"', attrs, name)
            self.assertIn('stroke-linejoin="round"', attrs, name)

    def test_ink_stays_inside_the_24_box(self):
        for name in GROK:
            svg = _read(name)
            src = _glyph_only(svg) if name in BADGE else svg
            m = measure(src)
            self.assertGreaterEqual(m["x0"], 0.0, name)
            self.assertGreaterEqual(m["y0"], 0.0, name)
            self.assertLessEqual(m["x1"], 24.0 + 0.05, name)  # last-pixel rounding
            self.assertLessEqual(m["y1"], 24.0 + 0.05, name)
            # Glyph itself (badge or not) is the thing that must be centred like online.
            if name in BADGE:
                g = measure(_glyph_only(svg))
                self.assertLessEqual(abs(g["bcx"] - 12), 0.5, name + " glyph cx")
                self.assertLessEqual(abs(g["h"] - self.online["h"]), 0.35, name + " glyph h")


if __name__ == "__main__":
    unittest.main()
