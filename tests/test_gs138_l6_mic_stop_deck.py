#!/usr/bin/env python3
"""L6 (gs-voice-train SPEC rev1.4 copy deck): the approved station mic-stop line, with <bot> as the display name."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import train_flow as tf  # noqa: E402

DECK = 'The microphone stopped while recording. Say "Hey Grok, train my wake word for <bot>" to try again.'


class MicStopDeck(unittest.TestCase):
    def test_exact_deck_line(self):
        self.assertEqual(tf.mic_stopped_station_line("Juniper"), DECK.replace("<bot>", "Juniper"))

    def test_display_name_is_squeezed(self):
        self.assertEqual(tf.mic_stopped_station_line("  Kitchen   Bot "), DECK.replace("<bot>", "Kitchen Bot"))

    def test_listener_passes_the_display_label(self):
        src = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn("train_flow.mic_stopped_station_line(persona_name(persona))", src)


if __name__ == "__main__":
    unittest.main()
