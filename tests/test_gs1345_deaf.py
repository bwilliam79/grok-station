"""0.13.45: an open capture that is stalled or exact silence asks for one USB reset.

A varying noise floor is not dead. Speaking and playback skip the window.
A second reset waits out the gap, then a longer backoff. The reset follows the
same auto-reset switch as a stuck microphone.
"""
import os
import struct
import sys
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))

import grok_wake as gw  # noqa: E402
import grok_station as gs  # noqa: E402

CARDS = " 0 [Device]: USB Audio - EMEET\n"


def _pcm(samples):
    return struct.pack("<%dh" % len(samples), *samples)


def _zeros(n=16):
    return _pcm([0] * n)


def _constant(n=16, value=7):
    return _pcm([value] * n)


def _noise(n=16):
    """A low varying floor. Not a threshold: the samples are just not constant."""
    pattern = (2, -3, 4, -2, 5, -4, 3, -5)
    return _pcm([pattern[i % len(pattern)] for i in range(n)])


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class DeafCapture(unittest.TestCase):
    def _watch(self):
        clock = _Clock()
        return gw.DeafWatch(now=clock, window_s=60.0, gap_s=600.0), clock

    def test_stalled_frames_trigger_one_reset(self):
        watch, clock = self._watch()
        fired = []
        for step in range(0, 70):
            clock.t = float(step)
            kind = watch.observe(None, stalled=True)
            if kind:
                fired.append((clock.t, kind))
        self.assertEqual(fired, [(60.0, "stall")])
        self.assertTrue(watch.degraded)

    def test_exact_zero_and_constant_frames_trigger_one_reset(self):
        for frame in (_zeros(), _constant()):
            watch, clock = self._watch()
            fired = []
            for step in range(0, 70):
                clock.t = float(step)
                kind = watch.observe(frame)
                if kind:
                    fired.append(kind)
            self.assertEqual(fired, ["silence"], frame[:8])
            self.assertEqual(watch.reason, "silence")

    def test_a_low_noise_floor_does_not_trigger(self):
        watch, clock = self._watch()
        fired = []
        frame = _noise()
        for step in range(0, 600):
            clock.t = float(step)
            kind = watch.observe(frame)
            if kind:
                fired.append(kind)
        self.assertEqual(fired, [])
        self.assertFalse(watch.degraded)
        self.assertFalse(gw.chunk_stuck(frame))
        self.assertTrue(gw.chunk_stuck(_zeros()))
        self.assertTrue(gw.chunk_stuck(_constant()))

    def test_the_rate_limit_holds_and_then_backs_off(self):
        watch, clock = self._watch()
        fired = []
        for step in (0, 60, 120, 659, 660, 660 + 1200):
            clock.t = float(step)
            kind = watch.observe(_zeros(), now=clock.t)
            if kind:
                fired.append(clock.t)
        # Window opens at 0. First reset at 60. Next is 60+600. The one after waits the doubled gap.
        self.assertEqual(fired, [60.0, 660.0, 660.0 + 1200.0])

    def test_no_detection_while_speaking_or_playing(self):
        for flag in ("speaking", "playing"):
            watch, clock = self._watch()
            fired = []
            for step in range(0, 180):
                clock.t = float(step)
                kind = watch.observe(_zeros(), **{flag: True})
                if kind:
                    fired.append(kind)
            self.assertEqual(fired, [], flag)
            self.assertFalse(watch.degraded, flag)
        watch, clock = self._watch()
        for step in range(0, 50):
            clock.t = float(step)
            self.assertIsNone(watch.observe(_zeros(), speaking=True))
        fired = []
        for step in range(50, 100):
            clock.t = float(step)
            kind = watch.observe(_zeros())
            if kind:
                fired.append(clock.t)
        self.assertEqual(fired, [])

    def test_deaf_reset_follows_the_auto_reset_switch(self):
        calls = []
        logs = []

        def rec(drought):
            return gw.MicRecovery(
                lambda card: calls.append(card) or None,
                now=lambda: 1000.0,
                flags=lambda: {"button": True, "night": False, "drought": drought},
                card=lambda: "Plus",
                busy=lambda: False,
                kick=lambda: None,
                log=logs.append,
            )

        self.assertEqual(rec(False).request("deaf"), "off")
        self.assertEqual(calls, [])
        self.assertEqual(logs, [])
        self.assertEqual(rec(True).request("deaf"), "ok")
        self.assertEqual(calls, ["Plus"])
        self.assertIn("mic reset: deaf", logs)
        self.assertTrue(all("/" not in line and "http" not in line for line in logs))

    def test_serve_capture_reports_silence_and_skips_while_speaking(self):
        class Clock:
            t = 0.0

        class Guard:
            def __init__(self, frames, clock):
                self.frames = list(frames)
                self.clock = clock
                self.reason = None

            def read(self, _n):
                self.clock.t += 1.0
                if not self.frames:
                    return b""
                return self.frames.pop(0)

            def observe(self, _buf, _busy):
                return False

        buf = _zeros()
        clock = Clock()
        watch = gw.DeafWatch(now=lambda: clock.t, window_s=60, gap_s=600)
        kinds = []

        def on_deaf(_deaf, _was, kind):
            if kind:
                kinds.append(kind)

        reason = gw.serve_capture(
            None, Guard([buf] * 70, clock), lambda _b, _g: None, lambda: False,
            n=len(buf), deaf=watch, held=lambda: False, on_deaf=on_deaf)
        self.assertEqual(reason, "eof")
        self.assertEqual(kinds, ["silence"])

        clock2 = Clock()
        watch2 = gw.DeafWatch(now=lambda: clock2.t, window_s=60, gap_s=600)
        quiet = []
        reason = gw.serve_capture(
            None, Guard([buf] * 70, clock2), lambda _b, _g: None, lambda: False,
            n=len(buf), deaf=watch2, held=lambda: True,
            on_deaf=lambda _d, _w, kind: quiet.append(kind) if kind else None)
        self.assertEqual(reason, "eof")
        self.assertEqual(quiet, [])
        self.assertFalse(watch2.degraded)

    def test_the_mic_icon_shows_the_problem_while_frames_still_arrive(self):
        now = 2000.0
        fresh = {"audio_at": now, "capture": "plughw:CARD=Plus,DEV=0", "deaf": "silence"}
        got = gs.check_mic(now, lambda: fresh, CARDS)
        self.assertFalse(got["ok"])
        self.assertEqual(got["detail"], "capture deaf")
        healthy = {"audio_at": now, "capture": "plughw:CARD=Plus,DEV=0", "deaf": None}
        self.assertTrue(gs.check_mic(now, lambda: healthy, CARDS)["ok"])


if __name__ == "__main__":
    unittest.main()
