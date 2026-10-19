#!/usr/bin/env python3
"""Write PNGs of the station tool cards and the timer strip.

Usage: python3 tools/render_cards.py OUTDIR

Fake data only. Sizes: 800x480, 1920x1080, 3840x2160. Headless (SDL dummy driver).
"""
import os, sys, time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame

SIZES = ((800, 480), (1920, 1080), (3840, 2160))
EPOCH = 1_700_000_000

CARDS = {
    "weather": {"kind": "weather", "icon": "partly", "now_temp": 64, "units": "°F", "conditions": "partly cloudy",
                "today": {"high": 71, "low": 52}, "tomorrow": {"high": 68, "low": 49}},
    "time": {"kind": "time", "zone_label": "London", "hhmm": "11:05 PM", "day": "Mon", "offset": "+6 h"},
    "convert": {"kind": "convert", "value": "350", "from_unit": "°F", "result": "176.67", "to_unit": "°C"},
}


def _chip(n, left, ringing=False, label="timer"):
    return {"id": f"t{n:05d}", "label": label, "ends_at_epoch": EPOCH + left, "ringing": ringing}


def main(out):
    pygame.display.init()
    pygame.font.init()
    os.makedirs(out, exist_ok=True)
    gs.WX_CACHE = "/nonexistent"
    gs.TIMER_EPOCH = EPOCH
    now = time.strptime("2026-10-19 17:05", "%Y-%m-%d %H:%M")
    wx = gs.Weather()
    wx.data, wx.ok_at = {"temp": 64, "code": 2, "day": True, "hi": 71, "lo": 52}, EPOCH
    running = [_chip(1, 125, label="pasta"), _chip(2, 3700, label="tea")]
    ringing = [_chip(1, -5, ringing=True, label="eggs"), _chip(2, 125, label="pasta")]
    for w, h in SIZES:
        disp = gs.Display(fb=None, size=(w, h))
        tag = f"{w}x{h}"
        for name, card in CARDS.items():
            gs.TIMER_CHIPS[:] = []
            msg = {"from": "Grok", "voice": True, "heard": "a short question", "text": "Here you go.",
                   "state": "speaking", "card": card}
            disp.screen.fill(gs.BG)
            gs.draw_session(disp.screen, disp.L, now, msg)
            path = os.path.join(out, f"{name}-{tag}.png")
            pygame.image.save(disp.screen, path)
            print(path)
        for name, chips in (("timers", running), ("ringing", ringing)):
            gs.TIMER_CHIPS[:] = [dict(c) for c in chips]
            disp.screen.fill(gs.BG)
            gs.draw_idle(disp.screen, disp.L, now, wx, True)
            path = os.path.join(out, f"{name}-{tag}.png")
            pygame.image.save(disp.screen, path)
            print(path)
    gs.TIMER_CHIPS[:] = []
    gs.TIMER_EPOCH = None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 tools/render_cards.py OUTDIR")
    main(sys.argv[1])
