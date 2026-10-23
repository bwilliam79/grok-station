"""Screen dimming: fake sensors, NOAA sun times, midnight schedule, levels, admin form."""
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import dimming


def _helper():
    g = {"__name__": "helper"}
    exec(compile(open(os.path.join(ROOT, "admin", "grok-station-helper")).read(), "helper", "exec"), g)
    return g


class Sensors(unittest.TestCase):
    def test_iio_input_then_raw_scale_and_names_only(self):
        with tempfile.TemporaryDirectory() as root:
            dev = os.path.join(root, "sys", "bus", "iio", "devices", "iio:device0")
            os.makedirs(dev)
            open(os.path.join(dev, "name"), "w").write("BH1750\n")
            open(os.path.join(dev, "in_illuminance_input"), "w").write("42.5\n")
            open(os.path.join(dev, "in_illuminance_raw"), "w").write("1\n")
            other = os.path.join(root, "sys", "bus", "iio", "devices", "iio:device1")
            os.makedirs(other)
            open(os.path.join(other, "name"), "w").write("humidity\n")
            found = dimming.detect_sensors(root)
            self.assertEqual([(s["name"], s["source"]) for s in found], [("bh1750", "iio")])
            self.assertEqual(dimming.current_lux(root), 42.5)
            self.assertEqual(dimming.hardware_status(root)["lux"], 42.5)
            os.remove(os.path.join(dev, "in_illuminance_input"))
            open(os.path.join(dev, "in_illuminance_raw"), "w").write("100\n")
            open(os.path.join(dev, "in_illuminance_scale"), "w").write("0.5\n")
            self.assertEqual(dimming.read_lux(dev), 50.0)
            i2c = os.path.join(root, "sys", "bus", "i2c", "devices", "1-0010")
            os.makedirs(i2c)
            open(os.path.join(i2c, "name"), "w").write("veml7700\n")
            # A regular file named like a bus is not probed (not a character device).
            os.makedirs(os.path.join(root, "dev"))
            open(os.path.join(root, "dev", "i2c-1"), "w").close()
            names = [(s["name"], s["source"]) for s in dimming.detect_sensors(root)]
            self.assertIn(("veml7700", "i2c"), names)
            status = dimming.hardware_status(root)
            blob = json.dumps(status)
            self.assertNotIn("/", blob)
            self.assertNotIn("iio:device", blob)
            self.assertEqual(status["sensor"]["name"], "bh1750")
            weird = os.path.join(root, "sys", "bus", "iio", "devices", "iio:device2")
            os.makedirs(weird)
            open(os.path.join(weird, "name"), "w").write("Weird Sensor!\n")
            open(os.path.join(weird, "in_illuminance_input"), "w").write("1\n")
            # device0 is still first alphabetically and still has lux
            self.assertEqual(dimming.hardware_status(root)["sensor"]["name"], "bh1750")

    def test_i2c_name_without_illuminance_has_no_reading(self):
        with tempfile.TemporaryDirectory() as root:
            i2c = os.path.join(root, "sys", "bus", "i2c", "devices", "1-0023")
            os.makedirs(i2c)
            open(os.path.join(i2c, "name"), "w").write("bh1750\n")
            status = dimming.hardware_status(root)
            self.assertEqual(status["sensor"], {"name": "bh1750", "source": "i2c"})
            self.assertIsNone(status["lux"])
            self.assertNotIn("/", json.dumps(status))

    def test_absent_sensor_is_fine(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(dimming.detect_sensors(root), [])
            self.assertIsNone(dimming.current_lux(root))
            self.assertEqual(dimming.hardware_status(root), {"sensor": None, "backlight": None, "lux": None})

    def test_backlight_write_overlay_and_stamp(self):
        with tempfile.TemporaryDirectory() as root:
            panel = os.path.join(root, "sys", "class", "backlight", "panel")
            os.makedirs(panel)
            open(os.path.join(panel, "max_brightness"), "w").write("255\n")
            bright = os.path.join(panel, "brightness")
            open(bright, "w").write("255\n")
            self.assertTrue(dimming.write_backlight(50, root))
            self.assertEqual(open(bright).read().strip(), "128")
            stamp = os.path.join(root, "cache", "dim-level")
            self.assertEqual(dimming.apply_brightness(40, root, stamp), "backlight")
            self.assertIsNone(dimming.overlay_level())
            self.assertEqual(open(stamp).read(), "1\n")
            self.assertEqual(open(bright).read().strip(), "102")  # round(40/100*255)
            os.remove(bright)
            os.mkdir(bright)  # not a writable file, even for root
            try:
                self.assertEqual(dimming.apply_brightness(20, root, stamp), "overlay")
                self.assertEqual(dimming.overlay_level(), 20)
                self.assertEqual(dimming.apply_brightness(None, root, stamp), "released")
                self.assertFalse(os.path.isfile(stamp))
                self.assertIsNone(dimming.overlay_level())
            finally:
                dimming.set_overlay(None)
            # No directory component: do not call makedirs("").
            here = os.path.join(root, "dim-level")
            os.chdir(root)
            try:
                self.assertIn(dimming.apply_brightness(10, root, "dim-level"), ("backlight", "overlay"))
            finally:
                os.chdir(ROOT)
                dimming.set_overlay(None)
            self.assertTrue(os.path.isfile(here) or dimming.overlay_level() is None)


class Sun(unittest.TestCase):
    def test_julian_day_and_declination(self):
        self.assertEqual(dimming._julian_day(2000, 1, 1), 2451544.5)
        self.assertAlmostEqual(dimming.solar_declination(2024, 6, 21), 23.4385549487431, delta=0.02)
        self.assertAlmostEqual(dimming.solar_declination(2024, 12, 21), -23.438020690684176, delta=0.02)
        t = dimming._jc(dimming._julian_day(2024, 6, 21))
        self.assertAlmostEqual(dimming._equation_of_time(t), -1.816, delta=0.02)

    def test_known_sunrise_and_longitude_shift(self):
        eq = dimming.sun_times(0, 0, 2024, 3, 20)
        self.assertAlmostEqual(eq["rise"], 364.03205698844874, delta=0.02)
        self.assertAlmostEqual(eq["set"], 1090.5461910191812, delta=0.02)
        self.assertFalse(eq["always_up"])
        june = dimming.sun_times(0, 0, 2024, 6, 21)
        east = dimming.sun_times(0, 15, 2024, 6, 21)
        self.assertAlmostEqual(june["rise"], 358.2386149776352, delta=0.02)
        self.assertAlmostEqual(june["set"], 1085.6115413954676, delta=0.02)
        self.assertAlmostEqual(east["rise"], 298.2295577655072, delta=0.02)
        self.assertAlmostEqual(june["rise"] - east["rise"], 60.009, delta=0.02)
        self.assertAlmostEqual(june["set"] - east["set"], 60.009, delta=0.02)
        nyc = dimming.sun_times(40, -74, 2024, 6, 21)
        self.assertAlmostEqual(nyc["rise"], 567.4632961972476, delta=0.02)
        self.assertAlmostEqual(nyc["set"], 1468.4603471189116, delta=0.02)

    def test_polar_and_local_dark(self):
        up = dimming.sun_times(80, 0, 2024, 6, 21)
        down = dimming.sun_times(80, 0, 2024, 12, 21)
        self.assertTrue(up["always_up"])
        self.assertIsNone(up["rise"])
        self.assertTrue(down["always_down"])
        self.assertIsNone(down["set"])
        night = time.struct_time((2024, 6, 21, 21, 0, 0, 4, 173, -1))
        noon = time.struct_time((2024, 6, 21, 12, 0, 0, 4, 173, -1))
        early = time.struct_time((2024, 6, 21, 5, 0, 0, 4, 173, -1))
        morning = time.struct_time((2024, 6, 21, 6, 0, 0, 4, 173, -1))
        self.assertTrue(dimming.sun_is_dark(40, -74, night, utc_offset_min=-240))
        self.assertFalse(dimming.sun_is_dark(40, -74, noon, utc_offset_min=-240))
        self.assertTrue(dimming.sun_is_dark(40, -74, early, utc_offset_min=-240))
        self.assertFalse(dimming.sun_is_dark(40, -74, morning, utc_offset_min=-240))
        self.assertFalse(dimming.sun_is_dark(80, 0, noon, utc_offset_min=0))
        dec = time.struct_time((2024, 12, 21, 12, 0, 0, 4, 356, -1))
        self.assertTrue(dimming.sun_is_dark(80, 0, dec, utc_offset_min=0))

    def test_no_network_imports(self):
        src = open(dimming.__file__, encoding="utf-8").read()
        self.assertNotIn("urllib", src)
        self.assertNotIn("socket", src)
        self.assertNotIn("requests", src)


class Schedule(unittest.TestCase):
    def cfg(self, **kw):
        base = {"mode": "schedule", "day": 90, "night": 20, "start": 22 * 60, "end": 6 * 60,
                "lat": None, "lon": None, "sunrise_offset": 0, "sunset_offset": 0}
        base.update(kw)
        return base

    def at(self, hour, minute):
        return time.struct_time((2024, 6, 21, hour, minute, 0, 4, 173, -1))

    def test_window_wraps_midnight_and_end_is_exclusive(self):
        self.assertTrue(dimming.in_window(23 * 60, 22 * 60, 6 * 60))
        self.assertTrue(dimming.in_window(1 * 60, 22 * 60, 6 * 60))
        self.assertTrue(dimming.in_window(5 * 60 + 59, 22 * 60, 6 * 60))
        self.assertFalse(dimming.in_window(6 * 60, 22 * 60, 6 * 60))
        self.assertFalse(dimming.in_window(12 * 60, 22 * 60, 6 * 60))
        self.assertFalse(dimming.in_window(22 * 60, 22 * 60, 22 * 60))  # empty, not all day
        self.assertEqual(dimming.select_level(self.cfg(), self.at(23, 30)), (20, None))
        self.assertEqual(dimming.select_level(self.cfg(), self.at(6, 0)), (90, None))
        self.assertEqual(dimming.select_level(self.cfg(), self.at(5, 59)), (20, None))
        self.assertEqual(dimming.select_level(self.cfg(start=None), self.at(23, 0)), (90, "no-times"))

    def test_levels_and_fallbacks(self):
        self.assertEqual(dimming.level_from_lux(20, 90, 20), 20)
        self.assertEqual(dimming.level_from_lux(80, 90, 20), 90)
        self.assertEqual(dimming.level_from_lux(50, 90, 20), 55)
        self.assertEqual(dimming.level_from_lux(50, 100, 0), 50)
        self.assertEqual(dimming.level_from_lux(19, 90, 20), 20)
        sensor = self.cfg(mode="auto-sensor")
        self.assertEqual(dimming.select_level(sensor, self.at(12, 0), lux=50), (55, None))
        self.assertEqual(dimming.select_level(sensor, self.at(23, 0), lux=None)[1], "no-sensor")
        self.assertEqual(dimming.select_level(sensor, self.at(23, 0), lux=None)[0], 20)
        bare = self.cfg(mode="auto-sensor", start=None, end=None, lat=40.0, lon=-74.0)
        level, why = dimming.select_level(bare, self.at(12, 0), lux=None, utc_offset_min=-240)
        self.assertEqual(why, "no-sensor")
        self.assertEqual(level, 90)
        none = self.cfg(mode="auto-sensor", start=None, end=None)
        self.assertEqual(dimming.select_level(none, self.at(23, 0), lux=None), (90, "no-sensor"))
        self.assertEqual(dimming.select_level(self.cfg(mode="off"), self.at(23, 0)), (None, None))
        self.assertTrue(dimming.in_dim_period(sensor, self.at(12, 0), lux=20))
        self.assertFalse(dimming.in_dim_period(sensor, self.at(12, 0), lux=20.1))

    def test_overlay_matches_css_opacity(self):
        self.assertEqual(dimming.overlay_alpha(None), 0)
        self.assertEqual(dimming.overlay_alpha(100), 0)
        self.assertEqual(dimming.overlay_alpha(0), 255)
        self.assertEqual(dimming.overlay_alpha(20), round(80 * 255 / 100))
        raw = bytearray([0x00, 0xF8])  # RGB565 red
        dimming.shade_rgb565(raw, 50)
        self.assertEqual(bytes(raw), bytes([0x00, 0x80]))  # red 31 -> 16
        raw = bytearray([0x00, 0xF8, 0xFF])  # odd trailing byte is not a pixel
        dimming.shade_rgb565(raw, 0)
        self.assertEqual(raw[-1], 0xFF)
        self.assertEqual(raw[0], 0)
        try:
            dimming.set_overlay(20)
            self.assertEqual(dimming.overlay_level(), 20)
            dimming.set_overlay(100)
            self.assertIsNone(dimming.overlay_level())
        finally:
            dimming.set_overlay(None)


class Form(unittest.TestCase):
    def test_coordinates_and_validation(self):
        self.assertEqual(dimming.normalize_coord("90.004", 90), "90.00")
        self.assertIsNone(dimming.normalize_coord("90.006", 90))
        self.assertEqual(dimming.normalize_coord("-180.004", 180), "-180.00")
        self.assertIsNone(dimming.normalize_coord("-180.006", 180))
        self.assertEqual(dimming.normalize_coord("41.875", 90), "41.88")
        self.assertEqual(dimming.normalize_coord("-87.635", 180), "-87.64")
        self.assertEqual(dimming.normalize_coord("12.3456", 90), "12.35")
        self.assertEqual(dimming.normalize_coord("+1.2", 90), "1.20")
        self.assertIsNone(dimming.normalize_coord("1e2", 90))
        self.assertEqual(dimming.normalize_coord("", 90), "")
        self.assertIsNone(dimming.normalize_coord(None, 90))
        self.assertEqual(dimming.normalize_coord(" 12.3 ", 90), "12.30")
        body, err = dimming.normalize_form({
            "GROK_STATION_DIM_MODE": "sun", "GROK_STATION_DIM_LAT": "41.878",
            "GROK_STATION_DIM_LON": "-87.634", "GROK_STATION_HOLD": "30"})
        self.assertIsNone(err)
        self.assertEqual(body["GROK_STATION_HOLD"], "30")
        self.assertEqual(body["GROK_STATION_DIM_MODE"], "sun")
        self.assertEqual(body["GROK_STATION_DIM_DAY"], "90")
        self.assertEqual(body["GROK_STATION_DIM_NIGHT"], "20")
        self.assertEqual(body["GROK_STATION_DIM_LAT"], "41.88")
        self.assertEqual(body["GROK_STATION_DIM_LON"], "-87.63")
        self.assertEqual(body["GROK_STATION_DIM_SUNRISE"], "0")
        _b, err = dimming.normalize_form({"GROK_STATION_DIM_MODE": "schedule"})
        self.assertEqual(err, "Dim from and dim until need a time like 22:00.")
        _b, err = dimming.normalize_form({"GROK_STATION_DIM_MODE": "off", "GROK_STATION_DIM_FROM": "abc"})
        self.assertEqual(err, "Dim from and dim until need a time like 22:00.")
        body, err = dimming.normalize_form({
            "GROK_STATION_DIM_MODE": "auto-sensor", "GROK_STATION_DIM_DAY": 90, "GROK_STATION_DIM_NIGHT": "20"})
        self.assertIsNone(err)
        self.assertEqual(body["GROK_STATION_DIM_LAT"], "")
        self.assertEqual(body["GROK_STATION_DIM_LON"], "")
        self.assertEqual(body["GROK_STATION_DIM_MODE"], "auto-sensor")
        body, err = dimming.normalize_form({"GROK_STATION_DIM_MODE": "off", "GROK_STATION_DIM_FROM": "22:00:00"})
        self.assertIsNone(err)
        self.assertEqual(body["GROK_STATION_DIM_FROM"], "22:00")
        view = dimming.display_view({"GROK_STATION_DIM_LAT": "12.3456", "GROK_STATION_HOLD": "30"})
        self.assertEqual(view["GROK_STATION_DIM_LAT"], "12.35")
        self.assertNotIn("latitude", json.dumps(view).lower())

    def test_config_fallback_rounds_station_location(self):
        cfg = dimming.config_from_env({
            "GROK_STATION_DIM_MODE": "sun", "GROK_STATION_LAT": "41.8781", "GROK_STATION_LON": "-87.6298"})
        self.assertEqual(round(cfg["lat"], 2), 41.88)
        self.assertEqual(round(cfg["lon"], 2), -87.63)
        partial = dimming.config_from_env({
            "GROK_STATION_DIM_LAT": "10.00", "GROK_STATION_LAT": "41.88", "GROK_STATION_LON": "-87.63"})
        self.assertIsNone(partial["lat"])
        self.assertIsNone(partial["lon"])
        bad = dimming.config_from_env({"GROK_STATION_DIM_MODE": "sensor", "GROK_STATION_DIM_DAY": "nope"})
        self.assertEqual(bad["mode"], "off")
        self.assertEqual(bad["day"], 90)


class HelperRules(unittest.TestCase):
    LAT = ("", "0.00", "8.50", "10.00", "12.35", "90.00", "-90.00", "-56.79")
    LON = ("", "0.00", "180.00", "-180.00", "139.76", "-87.63", "179.99")
    BAD_LAT = ("180.01", "090.00", "00", "181", "12.345", "1e2", "+1.20", "91.00", "-91.00", "90.01", "99.99")
    BAD_LON = ("180.01", "-180.01", "090.00", "181.00")

    def test_patterns_match_normalized_values(self):
        g = _helper()
        rules = {k: spec[1] for k, spec in g["SETTINGS"]["display"].items()}
        for value in self.LAT:
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_LAT"], value), value)
        for value in self.LON:
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_LON"], value), value)
        for value in self.BAD_LAT:
            self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_LAT"], value), value)
        for value in self.BAD_LON:
            self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_LON"], value), value)
        for value in ("off", "schedule", "sun", "auto-sensor"):
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_MODE"], value))
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_MODE"], "sensor"))
        for value in ("0", "90", "100", "-30", "180", "-180"):
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_SUNRISE"], value), value)
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_SUNRISE"], "00"))
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_SUNRISE"], "181"))
        for value in ("0", "20", "90", "100"):
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_DAY"], value))
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_DAY"], "00"))
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_DAY"], "01"))
        for value in ("", "22:00", "06:00"):
            self.assertIsNotNone(re.fullmatch(rules["GROK_STATION_DIM_FROM"], value), value)
        self.assertIsNone(re.fullmatch(rules["GROK_STATION_DIM_FROM"], "25:00"))

    def _save(self, g, body):
        old = sys.stdin
        buf = io.StringIO()
        sys.stdin = io.StringIO(json.dumps(body))
        code = 0
        try:
            with redirect_stdout(buf):
                try:
                    g["settings_set"]("display")
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 0
        finally:
            sys.stdin = old
        return code, buf.getvalue()

    def test_bad_latitude_does_not_change_the_env_file(self):
        g = _helper()
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, "env")
            open(env, "w").write("GROK_STATION_HOLD=30\n")
            g["ENV"]["display"] = env
            g["station_env_owner"] = lambda: (os.getuid(), os.getgid(), 0o640)
            g["run"] = lambda *a, **k: (0, "")
            code, text = self._save(g, {"GROK_STATION_DIM_LAT": "12.3456", "GROK_STATION_DIM_LON": "-87.63"})
            self.assertEqual(code, 1)
            self.assertNotIn("12.3456", open(env).read())
            code, _text = self._save(g, {"GROK_STATION_DIM_LAT": "91.00", "GROK_STATION_DIM_LON": "0.00"})
            self.assertEqual(code, 1)
            self.assertNotIn("91.00", open(env).read())
            self.assertNotIn("GROK_STATION_DIM_LAT", open(env).read())
            self.assertNotIn("12.3456", text)
            code, _text = self._save(g, {"GROK_STATION_DIM_MODE": "schedule", "GROK_STATION_DIM_DAY": "90"})
            self.assertEqual(code, 1)
            self.assertNotIn("GROK_STATION_DIM_MODE", open(env).read())
            ran = []
            g["run"] = lambda *a, **k: ran.append(a) or (0, "")
            code, text = self._save(g, {
                "GROK_STATION_DIM_MODE": "sun", "GROK_STATION_DIM_DAY": "90", "GROK_STATION_DIM_NIGHT": "20",
                "GROK_STATION_DIM_FROM": "22:00", "GROK_STATION_DIM_TO": "06:00",
                "GROK_STATION_DIM_LAT": "41.88", "GROK_STATION_DIM_LON": "-87.63",
                "GROK_STATION_DIM_SUNRISE": "0", "GROK_STATION_DIM_SUNSET": "-30"})
            self.assertEqual(code, 0, text)
            saved = open(env).read()
            self.assertIn("GROK_STATION_DIM_LAT=41.88", saved)
            self.assertIn("GROK_STATION_DIM_LON=-87.63", saved)
            self.assertIn("GROK_STATION_DIM_MODE=sun", saved)
            self.assertNotIn("41.88", text)
            self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o640)
            self.assertIn(("systemctl", "restart", "grok-station"), ran)


class DisplayFace(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            "GROK_STATION_DIM_MODE", "GROK_STATION_DIM_FROM", "GROK_STATION_DIM_TO",
            "GROK_STATION_DIM_DAY", "GROK_STATION_DIM_NIGHT", "GROK_STATION_SYS_ROOT",
            "GROK_STATION_DIM_STAMP", "SDL_VIDEODRIVER", "PYGAME_HIDE_SUPPORT_PROMPT")}
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import pygame
            import grok_station as gs
        except ImportError:
            self.skipTest("pygame is not installed")
        self.pygame = pygame
        self.gs = gs
        self._night = gs.NIGHT
        dimming.set_overlay(None)

    def tearDown(self):
        dimming.set_overlay(None)
        if hasattr(self, "gs"):
            self.gs.NIGHT = self._night
            self.gs._DIM.update(sig=None, lux=None, at=0.0, face=None, mode="off", why=None)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_night_face_follows_the_dim_window_only_when_the_toggle_is_on(self):
        gs = self.gs
        os.environ["GROK_STATION_DIM_MODE"] = "off"
        gs.NIGHT = "22-6"
        late = time.struct_time((2024, 6, 21, 23, 0, 0, 4, 173, -1))
        self.assertEqual(gs.night_face(late), gs.is_night(late))
        self.assertTrue(gs.is_night(late))
        os.environ["GROK_STATION_DIM_MODE"] = "schedule"
        os.environ["GROK_STATION_DIM_FROM"] = "23:00"
        os.environ["GROK_STATION_DIM_TO"] = "01:00"
        before = time.struct_time((2024, 6, 21, 22, 30, 0, 4, 173, -1))
        self.assertTrue(gs.is_night(before))
        self.assertFalse(gs.night_face(before))  # dim window has not started
        self.assertTrue(gs.night_face(late))
        gs.NIGHT = ""
        self.assertFalse(gs.night_face(late))

    def test_refresh_writes_backlight_only_when_the_level_changes(self):
        gs = self.gs
        with tempfile.TemporaryDirectory() as root:
            panel = os.path.join(root, "sys", "class", "backlight", "panel")
            os.makedirs(panel)
            open(os.path.join(panel, "max_brightness"), "w").write("100\n")
            bright = os.path.join(panel, "brightness")
            open(bright, "w").write("100\n")
            stamp = os.path.join(root, "dim-level")
            os.environ["GROK_STATION_SYS_ROOT"] = root
            os.environ["GROK_STATION_DIM_STAMP"] = stamp
            os.environ["GROK_STATION_DIM_MODE"] = "schedule"
            os.environ["GROK_STATION_DIM_FROM"] = "00:00"
            os.environ["GROK_STATION_DIM_TO"] = "00:01"
            os.environ["GROK_STATION_DIM_DAY"] = "80"
            os.environ["GROK_STATION_DIM_NIGHT"] = "80"  # same level either side of midnight
            gs._DIM.update(sig=None, lux=None, at=0.0, face=None, mode="off")
            buf = io.StringIO()
            with redirect_stdout(buf):
                gs.refresh_dim(force=True)
            self.assertEqual(open(bright).read().strip(), "80")
            self.assertIn("dim backlight 80", buf.getvalue())
            open(bright, "w").write("1\n")
            gs._DIM["at"] = 0.0
            with redirect_stdout(io.StringIO()):
                again = gs.refresh_dim()
            self.assertEqual(open(bright).read().strip(), "1")  # unchanged level, no rewrite
            self.assertNotEqual(again, "overlay")
            os.remove(bright)
            os.mkdir(bright)
            gs._DIM.update(sig=None, at=0.0)
            with redirect_stdout(io.StringIO()):
                why = gs.refresh_dim(force=True)
            self.assertEqual(why, "overlay")
            self.assertEqual(dimming.overlay_level(), 80)
            dimming.set_overlay(None)

    def test_flush_scales_a_copy_and_leaves_the_surface_bright(self):
        pygame, gs = self.pygame, self.gs
        if not pygame.display.get_init():
            pygame.display.init()
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            disp = gs.Display(fb=path, size=(48, 24))
            disp.screen.fill(pygame.Color(0, 255, 0))
            before = bytes(disp.screen.get_buffer().raw)
            dimming.set_overlay(0)
            disp.flush()
            after = bytes(disp.screen.get_buffer().raw)
            self.assertEqual(after, before)
            with open(path, "rb") as handle:
                written = handle.read()
            self.assertNotEqual(written, before)
            self.assertEqual(written, b"\x00" * len(written))
            dimming.set_overlay(0)
            disp.screen.fill(pygame.Color(255, 0, 0))
            disp.flush_rect(pygame.Rect(0, 0, 4, 2))
            with open(path, "rb") as handle:
                punched = handle.read(8)
            self.assertEqual(punched, b"\x00" * 8)  # partial update stays on the dimmed copy
            dimming.set_overlay(None)
            disp.screen.fill(pygame.Color(0, 255, 0))
            disp.flush_rect(pygame.Rect(0, 0, 4, 2))
            with open(path, "rb") as handle:
                cleared = handle.read()
            # overlay off: the rect matches the bright surface, not black
            self.assertNotEqual(cleared[:8], b"\x00" * 8)
        finally:
            dimming.set_overlay(None)
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
