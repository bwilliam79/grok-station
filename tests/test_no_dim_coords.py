"""Item 18: the Display page has no latitude/longitude; DIM_LAT/DIM_LON exist only as retired names."""
import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def read(*p): return open(os.path.join(ROOT, *p), encoding="utf-8").read()

class NoDimCoords(unittest.TestCase):
    def test_admin_form_has_no_coordinates(self):
        js = read("admin", "web", "app.js")
        for word in ("DIM_LAT", "DIM_LON", 'id="dlat"', 'id="dlon"', ">Latitude", ">Longitude", "Latitude <", "Longitude <"):
            self.assertNotIn(word, js)
        self.assertIn("Set the location on Time and location first.", js)

    def test_only_retired_mentions_remain_in_code(self):
        helper = read("admin", "grok-station-helper")
        self.assertNotRegex(helper, r'"GROK_STATION_DIM_LAT": \(')  # no validator: a save can't write it
        self.assertIn('RETIRED = {"display": ("GROK_STATION_DIM_LAT", "GROK_STATION_DIM_LON")}', helper)
        import sys
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import dimming
        self.assertNotIn("GROK_STATION_DIM_LAT", dimming.DIM_KEYS)
        self.assertNotIn("GROK_STATION_DIM_LON", dimming.DIM_KEYS)
        self.assertEqual(dimming.RETIRED_DIM, ("GROK_STATION_DIM_LAT", "GROK_STATION_DIM_LON"))
        for f in (("app", "grok_station.py"), ("admin", "grok_admin.py"), ("relay", "relay.py"), ("wake", "grok_wake.py")):
            self.assertNotIn("DIM_LAT", read(*f), f)
            self.assertNotIn("DIM_LON", read(*f), f)

    def test_offsets_show_minutes(self):
        js = read("admin", "web", "app.js")
        for fid in ("dsr", "dss"):
            m = re.search(r'id="%s"[^>]*><span class="unit">min</span>' % fid, js)
            self.assertIsNotNone(m, fid)
        self.assertIn("Minutes before (−) or after (+) sunrise and sunset.", js)
