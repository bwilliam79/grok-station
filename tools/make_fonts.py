#!/usr/bin/env python3
"""Build the small static fonts in assets/fonts from the Inter / Inter Tight variable fonts (OFL).

SDL_ttf can't pick a variable-font weight or OpenType features, so we pin the weight,
freeze tabular numerals into the cmap, and subset to Latin-1 + common punctuation.
Usage: make_fonts.py <Inter-Variable.ttf> <Inter-Italic-Variable.ttf> <InterTight-Variable.ttf> <outdir>
"""
import sys
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools import subset

inter, inter_it, tight, out = sys.argv[1:5]
UNI = list(range(0x20, 0x7F)) + list(range(0xA0, 0x180)) + [0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2022, 0x2026, 0x00B0, 0x2032, 0x2033, 0x20AC]


def build(src, axes, name):
    f = TTFont(src)
    instantiateVariableFont(f, axes, inplace=True)
    names = set(f.getGlyphOrder())
    for t in f["cmap"].tables:  # tabular figures by default (clock digits don't jiggle)
        for cp in range(0x30, 0x3A):
            g = t.cmap.get(cp)
            if g and g + ".tf" in names:
                t.cmap[cp] = g + ".tf"
    opts = subset.Options()
    opts.layout_features = ["kern", "liga"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    s = subset.Subsetter(opts)
    s.populate(unicodes=UNI)
    s.subset(f)
    f.save(f"{out}/{name}")
    print(name)


build(tight, {"wght": 600}, "InterTight-SemiBold.ttf")
build(tight, {"wght": 500}, "InterTight-Medium.ttf")
build(inter, {"wght": 500, "opsz": 32}, "Inter-Medium.ttf")
build(inter_it, {"wght": 500, "opsz": 32}, "Inter-MediumItalic.ttf")
