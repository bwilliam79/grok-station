#!/usr/bin/env python3
"""A (Noor gs-voice-train rev1.3 copy deck, 'Where shown'): station vs admin strings.

On the station a silent stop or a mic stop always uses the spoken voice restart line, however the
session started. "Tap Record voice to try again." and the other Record voice strings are admin-dialog only.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import train_flow as tf  # noqa: E402

VOICE_STOP = "Stopped. Nothing was saved."  # 0.13.9 rev1.6 heading; the restart line is drawn under it
MIC_STATION = 'The microphone stopped while recording. Say "Hey Grok, train my wake word for Juniper" to try again.'
MIC_ADMIN = ("The microphone stopped while recording. "
             "Say 'Hey Grok, train my wake word for Juniper' or press Record voice to try again.")


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


class StationLines(unittest.TestCase):
    def test_silent_stop_station_line_is_voice_for_both_starts(self):
        for voice in (True, False):
            d = tf.silent_clip(2, "Juniper", voice=voice)
            self.assertEqual(d["station"], VOICE_STOP, voice)
            self.assertNotIn("Record voice", d["station"])

    def test_touch_admin_lines_unchanged(self):
        d = tf.silent_clip(2, "Juniper", voice=False)
        self.assertEqual(d["say"], "I didn't hear anything. Nothing new was saved.")
        self.assertEqual(d["note"], "Tap Record voice to try again.")

    def test_mic_stop_lines(self):
        self.assertEqual(tf.mic_stopped_station_line("Juniper"), MIC_STATION)
        self.assertEqual(tf.mic_stopped_line("Juniper"), MIC_ADMIN)

    def test_listener_posts_station_lines(self):
        wake = _src("wake/grok_wake.py")
        self.assertIn('ENROLL_STATE["silent_stop"] = (decision["say"], decision["station"])', wake)
        self.assertIn('show(i, "stopped", "topup" if prior else "")', wake)  # rev1.6: an enroll stage, 4 s
        self.assertIn('post_state("notice", text=train_flow.mic_stopped_station_line(station_bot_name(persona, label)))', wake)

    def test_station_renderer_never_says_record_voice(self):
        self.assertNotIn("Record voice", _src("app/grok_station.py"))
        for m in re.finditer(r'post_state\("notice", text=([^)\n]*)\)', _src("wake/grok_wake.py")):
            self.assertNotIn("Record voice", m.group(1))


class TableStrings(unittest.TestCase):
    """Every string in the table appears where the table says (exact copy)."""

    def test_station_strings(self):
        st = _src("app/grok_station.py")
        for s in ("Recording in {digit}…", "To stop, stay quiet for 3 seconds.", "I didn't hear it. Again after the chime.",
                  "Saved. Checking the recordings.", "I didn't hear it. Stopping in {digit}…", "Stopped. Nothing was saved.",
                  "Stopped. Nothing new was saved.", "Listening… {clip}"):
            self.assertIn(s, st)
        self.assertIn("Okay. I'll record you 5 times. Say %s after each chime. To stop, stay quiet.", _src("relay/train_flow.py"))
        self.assertIn("Okay. I'll add 5 more. Say %s after each chime. To stop, stay quiet.", _src("relay/train_flow.py"))
        self.assertIn("I don't have a bot called %s.", _src("relay/train_flow.py"))
        self.assertIn('ENROLL_START_FAILED = "I couldn\'t start recording. Try again in a moment."', _src("relay/relay.py"))

    def test_admin_strings(self):
        js = _src("admin/web/app.js")
        for s in ("I didn't hear anything. Nothing new was saved.", "Tap Record voice to try again.",
                  "Stop and discard", "Stopped. Nothing new was saved."):
            self.assertIn(s, js)


if __name__ == "__main__":
    unittest.main()
