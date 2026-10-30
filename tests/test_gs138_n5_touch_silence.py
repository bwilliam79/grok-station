#!/usr/bin/env python3
"""A Record voice tap stops after two silent clips and saves nothing (gs138 N5).

Voice-started sessions keep their stop line. The touch screen uses the silent
error: "I didn't hear anything. Nothing new was saved." /
"Tap Record voice to try again." The Try again and Cancel buttons stay put.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wake"))
import grok_wake as gw  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TOUCH_SAY = "I didn't hear anything. Nothing new was saved."
TOUCH_NOTE = "Tap Record voice to try again."
VOICE_STOP = "Stopped. Say 'Hey Grok, train my wake word for Juniper' to try again."


class SilentClipCopy(unittest.TestCase):
    def test_voice_stop_copy_unchanged(self):
        first = gw.silent_clip(1, "Juniper")
        self.assertEqual(first["action"], "retry")
        self.assertEqual(first["say"], "I didn't hear it. Again after the chime.")
        second = gw.silent_clip(2, "Juniper")
        self.assertEqual(second["action"], "stop")
        self.assertEqual(second["say"], VOICE_STOP)
        again = gw.silent_clip(2, "Juniper", voice=True)
        self.assertEqual(again["action"], "stop")
        self.assertEqual(again["say"], VOICE_STOP)
        self.assertNotIn("note", again)

    def test_touch_stop_copy(self):
        self.assertEqual(gw.train_flow.SILENT_TOUCH_SAY, TOUCH_SAY)
        self.assertEqual(gw.train_flow.SILENT_TOUCH_NOTE, TOUCH_NOTE)
        first = gw.silent_clip(1, "Juniper", voice=False)
        self.assertEqual(first, {"action": "retry", "say": "I didn't hear it. Again after the chime."})
        second = gw.silent_clip(2, "Kitchen", voice=False)
        self.assertEqual(second["action"], "stop")
        self.assertEqual(second["say"], TOUCH_SAY)
        self.assertEqual(second["note"], TOUCH_NOTE)
        self.assertNotIn("Kitchen", second["say"])
        self.assertNotIn("Hey Grok", second["say"])


class TouchEnrollSilence(unittest.TestCase):
    def test_two_silent_clips_save_nothing(self):
        """A touch req (voice is not true) stops on the second silent clip.

        The session directory is removed, so nothing new is saved. last.message
        is the line the admin status copies into row.message.
        """
        posted, notices = [], []
        root = tempfile.mkdtemp()
        old = (gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR, gw.time.sleep,
               gw.ENROLL_STATE.get("busy"), gw.ENROLL_STATE.get("last"))

        def post_state(state=None, **kw):
            if state == "notice":
                notices.append(kw.get("text"))
            heard = kw.get("heard") or ""
            if "|" in str(heard):
                posted.append(str(heard).split("|")[1])
            elif state:
                posted.append(state)

        def read16(raw=False):
            return gw.np.zeros(1280, gw.np.int16)

        gw.post_state = post_state
        gw.subprocess.Popen = lambda *a, **k: None
        gw.ENROLL_DIR = root
        gw.time.sleep = lambda s: None
        gw.ENROLL_STATE["busy"] = False
        gw.ENROLL_STATE["last"] = None
        gw.ENROLL_STATE["discard"] = False
        gw.ENROLL_STATE["pause"] = False
        gw.ENROLL_STATE.pop("silent_stop", None)
        saved = []
        try:
            with redirect_stdout(io.StringIO()):
                gw.enroll(
                    {"phrase": "hey_juniper", "persona": "juniper", "count": 5, "voice": False},
                    read16, "default")
            last = gw.ENROLL_STATE.get("last") or {}
            for dirpath, _dirs, files in os.walk(root):
                saved.extend(os.path.join(dirpath, name) for name in files)
            busy = gw.ENROLL_STATE.get("busy")
        finally:
            (gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR, gw.time.sleep) = old[:4]
            gw.ENROLL_STATE["busy"] = old[4]
            gw.ENROLL_STATE["last"] = old[5]
            gw.ENROLL_STATE.pop("silent_stop", None)
        self.assertEqual(posted.count("recording"), 2)
        self.assertEqual(posted.count("quiet"), 2)
        self.assertNotIn("saved", posted)
        self.assertNotIn("error", posted)
        self.assertEqual(notices, [TOUCH_SAY])
        self.assertEqual(saved, [])
        self.assertFalse(busy)
        self.assertEqual(last.get("stage"), "failed")
        # Admin status copies this onto the phrase row as row.message.
        self.assertEqual(last.get("message"), TOUCH_SAY)


class AppJsSilent(unittest.TestCase):
    def test_silent_kind_reuses_show_error(self):
        with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        start = js.find("  const showError = kind => {")
        end = js.find("  const paintRow = ", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        block = js[start:end]
        self.assertIn('kind === "silent"', block)
        self.assertIn(TOUCH_SAY, block)
        self.assertIn(TOUCH_NOTE, block)
        self.assertEqual(block.count('class="acts"'), 1)
        self.assertIn('id="wwagain" type="button">Try again</button>', block)
        self.assertIn('id="wwcancel" type="button">Cancel</button>', block)
        self.assertNotIn(">Record voice<", block)
        fail = js.find('if (state === "failed")')
        branch = js[fail:js.find("if (state === \"training\"", fail)]
        self.assertIn('msg === "%s"' % TOUCH_SAY, branch)
        self.assertIn('showError("silent")', branch)
        self.assertIn('showError("mic")', branch)
        self.assertIn('showError("busy")', branch)
        self.assertIn('showError("train")', branch)


if __name__ == "__main__":
    unittest.main()
