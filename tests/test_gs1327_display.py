"""0.13.27 Display settings (Noor rev1, bq-0061).

Night clock and Fade share Save. Light-sensor fallback times stay visible.
Hold stays 5–120 in the page. One rotation control. Leaving dirty warns.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class DisplayRev1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("admin", "web", "app.js"))
        cls.css = _read(os.path.join("admin", "web", "admin.css"))
        cls.panel = cls.js.split("async function displayPanel", 1)[1].split("async function xaiKeyPanel", 1)[0]

    def test_sections_keep_field_ids_and_one_rotation(self):
        order = [">Night clock<", ">Timing<", ">Brightness<", ">Units<", "Advanced: team logo sources"]
        idx = [self.panel.index(title) for title in order]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn(">Clock and night clock<", self.panel)
        self.assertNotIn(">Screens<", self.panel)
        self.assertNotIn('id="crot"', self.panel)
        self.assertNotIn("Card rotation", self.panel)
        self.assertIn("GROK_STATION_CARD_ROTATE", self.panel)
        for fid in ("non", "nf", "nt", "hold", "urot", "shold", "fade", "dmode", "dday", "dnight",
                    "dfrom", "dto", "dsr", "dss", "dun", "chosts", "dsave"):
            self.assertIn('id="%s"' % fid, self.panel, fid)
        self.assertIn('max="120"', self.panel.split('id="hold"', 1)[0][-80:] + self.panel.split('id="hold"', 1)[1][:40])
        self.assertIn("[10, 15, 20, 30, 45, 60]", self.panel)
        self.assertIn("Dim clock on a black screen.", self.panel)
        self.assertIn("How long a spoken reply stays on screen after speech ends.", self.panel)
        self.assertIn("aria-describedby=\"uroth\"", self.panel)
        self.assertIn("Sunrise and sunset", self.panel)
        self.assertIn(">Light sensor<", self.panel)

    def test_night_and_fade_save_with_the_page(self):
        pre, post = self.panel.split('$("#dsave", el).onclick', 1)
        self.assertNotIn("putDisp({GROK_STATION_NIGHT", pre)
        self.assertNotIn("putDisp({GROK_STATION_FADE", pre)
        self.assertIn("GROK_STATION_NIGHT", post)
        self.assertIn("GROK_STATION_FADE", post)
        self.assertIn("dispDirty", self.panel)
        self.assertIn("setPageGuard", self.panel)

    def test_unsaved_leave_warns(self):
        self.assertIn("beforeunload", self.js)
        self.assertIn("Leave without saving", self.js)
        self.assertIn("confirmLeave", self.js)
        self.assertIn(">Unsaved changes<", self.panel)

    def test_mode_gates_keep_values_and_fallback_times(self):
        self.assertIn('id="levels"', self.panel)
        self.assertIn('id="dtimes"', self.panel)
        self.assertIn('id="sun"', self.panel)
        self.assertIn("Fallback times", self.panel)
        self.assertIn("no reading", self.panel)
        self.assertIn("Used when the sensor has no reading.", self.panel)
        self.assertNotIn("Fallback times. Used when", self.panel)
        self.assertIn("Schedule window. Not the Night clock hours.", self.panel)
        gate = self.panel.split("const applyMode", 1)[1].split("const putDisp", 1)[0]
        self.assertIn("auto-sensor", gate)
        self.assertIn("dtimes", gate)
        self.assertNotIn("noReading", gate.split("dtimes", 1)[0])
        self.assertIn("hidden", gate)
        self.assertNotIn(".remove()", gate)
        self.assertIn('data-p="time"', self.panel)
        self.assertIn("Set the location on ", self.panel)
        self.assertIn(">Time and location</a> first.", self.panel)

    def test_spacing_focus_and_skeleton(self):
        self.assertIn(".dispcard{padding:20px 22px}", self.css)
        self.assertNotIn(".dispcard{padding:8px 12px}", self.css)
        self.assertNotIn("height:30px;min-height:30px", self.css)
        self.assertIn("minmax(300px,1fr)", self.css)
        self.assertIn(".dispcard select#urot{height:44px;min-height:44px", self.css)
        focus = self.css.split(".dispcard .tog:focus-visible", 1)[1].split("}", 1)[0]
        self.assertIn("outline:2px solid var(--gs-teal)", focus)
        self.assertIn("outline-offset:2px", focus)
        self.assertNotIn("clearAdvFocus", self.panel)
        self.assertNotIn(".blur()", self.panel)
        skel, form = self.panel.split("await Promise.all", 1)
        self.assertIn("skel(", skel)
        self.assertIn("/api/settings/display", form)
        self.assertIn("/api/cards", form)
        self.assertIn("Example night clock. The black layer is the night level.", self.panel)


if __name__ == "__main__":
    unittest.main()
