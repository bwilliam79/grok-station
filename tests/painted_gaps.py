"""Painted gap above the lower tile, the gap under it, and the tile's painted height.

The block bottom is the lowest painted clock, date, or weather pixel.
The tile is the non-background paint under that line.
"""
import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402
import grok_station as gs  # noqa: E402


def painted_gaps(surf):
    rects = [gs.LAST_CLOCK_RECT, gs.LAST_DATE_RECT, gs.LAST_WEATHER_RECT]
    block = 0
    for rect in rects:
        if not rect:
            continue
        sub = surf.subsurface(rect).copy()
        sub.set_colorkey(gs.BG)
        box = sub.get_bounding_rect()
        if box.w:
            block = max(block, rect.y + box.bottom)
    width, height = surf.get_size()
    band = surf.subsurface(pygame.Rect(0, block, width, height - block)).copy()
    band.set_colorkey(gs.BG)
    box = band.get_bounding_rect()
    if box.w == 0:
        raise AssertionError("the lower tile painted nothing")
    top = block + box.y
    bottom = block + box.bottom
    return top - block, height - bottom, bottom - top


def chip_ink_top(surf, after_y):
    """First painted row of the timer chips at or below after_y. Background rows do not count."""
    width, height = surf.get_size()
    for y in range(int(after_y), height):
        row = surf.subsurface(pygame.Rect(0, y, width, 1)).copy()
        row.set_colorkey(gs.BG)
        if row.get_bounding_rect().w:
            return y
    raise AssertionError("no chip ink")
