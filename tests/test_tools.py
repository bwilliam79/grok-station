"""Station tools (relay/tools.py) and their wiring: offered to every persona, dispatched without side effects
beyond a message to the station, timers handled on the Pi. No network: weather is fed a canned Open-Meteo reply."""
import asyncio, datetime, json, os, sys, tempfile, time, unittest
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import tools as T  # noqa: E402

SAMPLE = {"current": {"temperature_2m": 61.4, "apparent_temperature": 59.2, "weather_code": 2, "wind_speed_10m": 8.6,
                      "relative_humidity_2m": 55},
          "daily": {"time": ["2026-10-19", "2026-10-20"], "weather_code": [3, 61], "temperature_2m_max": [66.2, 58.9],
                    "temperature_2m_min": [44.6, 41.0], "precipitation_probability_max": [10, 80]}}


class Time(unittest.TestCase):
    def test_local_and_other_zone(self):
        now = datetime.datetime(2026, 10, 19, 22, 5, tzinfo=datetime.timezone.utc)
        r = T.get_time({}, now)
        self.assertEqual(r["timezone"], T.TZ)
        self.assertTrue(r["ok"])
        r = T.get_time({"timezone": "Asia/Tokyo"}, now)
        self.assertEqual(r["date"], "Tuesday, October 20, 2026"); self.assertEqual(r["time"], "7:05 AM")
        self.assertFalse(T.get_time({"timezone": "Mars/Base"})["ok"])


class Weather(unittest.TestCase):
    def test_summary(self):
        r = T.weather_summary(SAMPLE)
        self.assertEqual(r["now"]["temperature"], 61); self.assertEqual(r["now"]["conditions"], "partly cloudy")
        self.assertEqual(r["tomorrow"], {"high": 59, "low": 41, "conditions": "light rain", "rain_chance_pct": 80})
        r = T.weather_summary(SAMPLE, "tomorrow")
        self.assertNotIn("now", r); self.assertNotIn("today", r)

    def test_unconfigured_location_says_so(self):
        old = T.LAT, T.LON
        T.LAT = T.LON = ""
        try:
            r = asyncio.run(T.get_weather({}, None))
        finally:
            T.LAT, T.LON = old
        self.assertFalse(r["ok"]); self.assertIn("No weather location", r["error"])

    def test_url_has_no_key_and_uses_config(self):
        old = T.LAT, T.LON
        T.LAT, T.LON = "40.0", "-90.0"
        try:
            u = T.weather_url()
        finally:
            T.LAT, T.LON = old
        self.assertTrue(u.startswith("https://api.open-meteo.com/")); self.assertIn("latitude=40.0", u)
        self.assertNotIn("key", u.lower().replace("weather_code", ""))


class Units(unittest.TestCase):
    def c(self, v, a, b):
        return T.convert_units({"value": v, "from_unit": a, "to_unit": b})

    def test_conversions(self):
        self.assertEqual(self.c(1, "mile", "km")["result"], 1.61)
        self.assertEqual(self.c(350, "F", "celsius")["result"], 176.67)
        self.assertEqual(self.c(-40, "degrees C", "°F")["result"], -40.0)
        self.assertEqual(self.c(1, "cup", "tbsp")["result"], 16.0)
        self.assertEqual(self.c(3, "tsp", "tablespoons")["result"], 1.0)
        self.assertEqual(self.c(2.2, "lbs", "kg")["result"], 0.998)
        self.assertEqual(self.c(1, "acre", "sq ft")["result"], 43560.0)
        self.assertEqual(self.c(60, "mph", "km/h")["result"], 96.56)
        self.assertEqual(self.c(1, "mm", "mi")["result"], 6.21e-07)

    def test_errors(self):
        self.assertFalse(self.c(1, "kg", "km")["ok"])
        self.assertFalse(self.c(1, "smoot", "m")["ok"])
        self.assertFalse(self.c("lots", "m", "ft")["ok"])


class Timers(unittest.TestCase):
    def test_set_list_cancel(self):
        timers, now = {}, 1000.0
        out, msg = T.set_timer({"seconds": 600, "label": "pasta <script>"}, timers, now)
        self.assertTrue(out["ok"]); self.assertEqual(out["label"], "pasta script"); self.assertEqual(out["length"], "10 minutes")
        self.assertEqual(msg["type"], "timer_set"); self.assertEqual(msg["seconds"], 600)
        T.set_timer({"seconds": 90}, timers, now)
        lst = T.list_timers(timers, now + 30)["timers"]
        self.assertEqual(lst, [{"label": "timer", "left": "1 minute"}, {"label": "pasta script", "left": "9 minutes, 30 seconds"}])
        self.assertFalse(T.cancel_timer({}, timers, now)[0]["ok"])  # two running: ask which
        out, msg = T.cancel_timer({"label": "Pasta Script"}, timers, now)
        self.assertEqual(out["cancelled"], "pasta script"); self.assertEqual(msg["type"], "timer_cancel")
        self.assertTrue(T.cancel_timer({}, timers, now)[0]["ok"])

    def test_limits(self):
        timers = {}
        self.assertFalse(T.set_timer({"seconds": 0}, timers, 0)[0]["ok"])
        self.assertFalse(T.set_timer({"seconds": 90000}, timers, 0)[0]["ok"])
        for _ in range(10):
            T.set_timer({"seconds": 60}, timers, 0)
        self.assertFalse(T.set_timer({"seconds": 60}, timers, 0)[0]["ok"])

    def test_parse_from_pi(self):
        q = json.dumps([{"id": "abc123", "label": "eggs", "left": 120}, {"id": "../x", "left": 5}, {"id": "zz", "left": "x"}])
        t = T.parse_timers(q, 100.0)
        self.assertEqual(list(t), ["abc123"]); self.assertEqual(t["abc123"]["due"], 220.0)
        self.assertEqual(T.parse_timers("not json", 0), {})


class Cards(unittest.TestCase):
    """Display cards are small, display-only, and absent when the tool fails."""

    def test_time_card(self):
        now = datetime.datetime(2026, 10, 19, 22, 5, tzinfo=datetime.timezone.utc)
        out, card = T.time_result({"timezone": "Europe/London"}, now)
        self.assertTrue(out["ok"])
        self.assertEqual(card, {"type": "card", "kind": "time", "zone_label": "London", "hhmm": "11:05 PM",
                                 "day": "Mon", "offset": "+6 h"})
        out, card = T.time_result({"timezone": "Asia/Tokyo"}, now)
        self.assertEqual(card["zone_label"], "Tokyo")
        self.assertEqual(card["hhmm"], "7:05 AM")
        self.assertEqual(card["day"], "Tue")
        self.assertEqual(card["offset"], "+14 h")
        out, card = T.time_result({"timezone": "Asia/Kolkata"}, now)
        self.assertEqual(card["offset"], "+10:30 h")
        self.assertEqual(card["hhmm"], "3:35 AM")
        out, card = T.time_result({"timezone": "America/New_York"}, now)
        self.assertEqual(card["zone_label"], "New York")
        self.assertEqual(card["offset"], "+1 h")
        out, card = T.time_result({}, now)
        self.assertEqual(card["zone_label"], "Chicago")
        self.assertEqual(card["day"], "Mon")
        self.assertEqual(card["hhmm"], "5:05 PM")
        self.assertEqual(card["offset"], "+0 h")
        out, card = T.time_result({"timezone": "Mars/Base"}, now)
        self.assertFalse(out["ok"])
        self.assertIsNone(card)

    def test_weather_card_has_no_place(self):
        j = json.loads(json.dumps(SAMPLE))
        j.update(latitude=41.88, longitude=-87.63, timezone="America/Chicago", place="the kitchen")
        card = T.weather_card(j)
        self.assertEqual(card["kind"], "weather")
        self.assertEqual(card["icon"], "partly")
        self.assertEqual(card["now_temp"], 61)
        self.assertEqual(card["units"], "°F")
        self.assertEqual(card["conditions"], "partly cloudy")
        self.assertEqual(card["today"], {"high": 66, "low": 45})
        self.assertEqual(card["tomorrow"], {"high": 59, "low": 41})
        blob = json.dumps(card).lower()
        for banned in ("place", "lat", "lon", "kitchen", "chicago", "41.88", "-87.63", "latitude", "longitude"):
            self.assertNotIn(banned, blob)
        self.assertNotIn("place", card)
        old = T.UNITS
        T.UNITS = "celsius"
        try:
            self.assertEqual(T.weather_card(SAMPLE)["units"], "°C")
        finally:
            T.UNITS = old
        self.assertIsNone(T.weather_card({"current": {}, "daily": {}}))

    def test_icon_mapping(self):
        expect = {0: "sun", 1: "partly", 2: "partly", 3: "cloud", 45: "fog", 48: "fog", 51: "rain", 63: "rain",
                  67: "rain", 80: "rain", 82: "rain", 71: "snow", 77: "snow", 85: "snow", 95: "storm", 99: "storm",
                  123: "cloud"}
        for code, icon in expect.items():
            self.assertEqual(T.wmo_icon(code), icon, code)
        self.assertEqual(T.wmo_icon(None), "cloud")
        self.assertEqual(T.wmo_icon("nope"), "cloud")
        rainy = json.loads(json.dumps(SAMPLE))
        rainy["current"]["weather_code"] = 95
        self.assertEqual(T.weather_card(rainy)["icon"], "storm")

    def test_convert_card_and_failures(self):
        out, card = T.convert_result({"value": 350, "from_unit": "F", "to_unit": "celsius"})
        self.assertTrue(out["ok"])
        self.assertEqual(card, {"type": "card", "kind": "convert", "value": "350", "from_unit": "°F",
                                 "result": "176.67", "to_unit": "°C"})
        out, card = T.convert_result({"value": 1, "from_unit": "mile", "to_unit": "km"})
        self.assertEqual(card["value"], "1")
        self.assertEqual(card["from_unit"], "mi")
        self.assertEqual(card["result"], "1.61")
        self.assertEqual(card["to_unit"], "km")
        out, card = T.convert_result({"value": 1, "from_unit": "kg", "to_unit": "km"})
        self.assertFalse(out["ok"])
        self.assertIsNone(card)
        out, card = T.convert_result({"value": "lots", "from_unit": "m", "to_unit": "ft"})
        self.assertIsNone(card)

    def test_run_sends_a_card_only_on_success(self):
        class Resp:
            def __init__(self, status, body):
                self.status, self.body = status, body
            async def read(self):
                return self.body
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class Http:
            def __init__(self, status, body):
                self.status, self.body = status, body
            def get(self, url, timeout=None):
                return Resp(self.status, self.body)

        old = T.LAT, T.LON, T._timeout
        T.LAT, T.LON, T._timeout = "41.0", "-87.0", lambda s: None
        try:
            out, card = asyncio.run(T.run("get_weather", {"day": "tomorrow"}, Http(200, json.dumps(SAMPLE).encode()), {}, 0))
            self.assertNotIn("now", out)  # spoken summary follows the requested day
            self.assertIn("place", out)   # the model still hears where it is
            self.assertEqual(card["kind"], "weather")
            self.assertIn("now_temp", card)  # the card is not filtered by day
            self.assertNotIn("place", card)
            out, card = asyncio.run(T.run("get_weather", {}, Http(503, b""), {}, 0))
            self.assertFalse(out["ok"])
            self.assertIsNone(card)
            T.LAT = T.LON = ""
            out, card = asyncio.run(T.run("get_weather", {}, None, {}, 0))
            self.assertFalse(out["ok"])
            self.assertIsNone(card)
        finally:
            T.LAT, T.LON, T._timeout = old
        out, card = asyncio.run(T.run("get_time", {"timezone": "Mars/Base"}, None, {}, 0))
        self.assertFalse(out["ok"])
        self.assertIsNone(card)
        out, card = asyncio.run(T.run("convert_units", {"value": 2, "from_unit": "cup", "to_unit": "tbsp"}, None, {}, 0))
        self.assertEqual(card["kind"], "convert")
        self.assertEqual(card["result"], "32")
        out, msg = asyncio.run(T.run("set_timer", {"seconds": 5, "label": "tea"}, None, {}, 0))
        self.assertEqual(msg["type"], "timer_set")
        self.assertNotEqual(msg.get("kind"), "card")
        with open(os.path.join(os.path.dirname(__file__), "..", "relay", "relay.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('log(f"tool {ev[\'name\']} ok={out.get(\'ok\')}")', src)


class Wiring(unittest.TestCase):
    def test_every_persona_gets_tools_and_safety_allows_only_timers(self):
        import relay
        names = lambda roster, slug: [t["name"] for t in relay.tools_for(roster, slug)]
        for slug in ("grok", "hank"):
            self.assertTrue(T.NAMES <= set(names({"hank": {"name": "Hank"}}, slug)))
        self.assertTrue(T.NAMES <= set(names({}, "grok")))  # even with no roster
        self.assertIn("kitchen timers", relay.SAFETY)
        for s in T.SPECS:  # no tool can send, post, buy or reach a person
            self.assertNotRegex(s["description"].lower(), r"\b(send|email|post|buy|order|call someone)\b")

    def test_dispatch(self):
        timers = {}
        out, msg = asyncio.run(T.run("set_timer", {"seconds": 5}, None, timers, 0))
        self.assertTrue(out["ok"]); self.assertEqual(msg["type"], "timer_set")
        self.assertEqual(asyncio.run(T.run("nope", {}, None, timers, 0))[0]["ok"], False)


class PiTimers(unittest.TestCase):
    def test_wake_side(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
        import grok_wake as gw
        gw.TIMERS_FILE = os.path.join(tempfile.mkdtemp(), "timers.json")
        gw.TIMERS.clear()
        gw.timer_event({"type": "timer_set", "id": "abc123", "seconds": 60, "label": "tea!"})
        gw.timer_event({"type": "timer_set", "id": "../bad", "seconds": 60})
        gw.timer_event({"type": "timer_set", "id": "def456", "seconds": 999999})
        self.assertEqual(list(gw.TIMERS), ["abc123"]); self.assertEqual(gw.TIMERS["abc123"]["label"], "tea")
        q = json.loads(__import__("urllib.parse").parse.unquote(gw.timers_query()))
        self.assertEqual(q[0]["id"], "abc123"); self.assertTrue(55 <= q[0]["left"] <= 60)
        gw.TIMERS.clear(); gw.timers_load()
        self.assertIn("abc123", gw.TIMERS)  # survives a restart
        gw.timer_event({"type": "timer_cancel", "id": "abc123"})
        self.assertEqual(gw.TIMERS, {}); self.assertEqual(gw.timers_query(), "")


if __name__ == "__main__":
    unittest.main()


class StationLocation(unittest.TestCase):
    """Single device: the relay reads the location the admin page saved, without a restart."""

    def test_weather_url_follows_the_station_env_file(self):
        import tempfile, time as _t
        d = tempfile.mkdtemp(); f = os.path.join(d, "env")
        old = (T.STATION_ENV, T.LAT, T.LON)
        T.STATION_ENV, T.LAT, T.LON = f, "", ""
        try:
            self.assertIsNone(T.weather_url())  # no file: no location
            open(f, "w").write("GROK_STATION_TOKEN=x\n")
            self.assertIsNone(T.weather_url())
            _t.sleep(0.01)
            open(f, "w").write("GROK_STATION_TOKEN=x\nGROK_STATION_LAT=41.5\nGROK_STATION_LON=-88.1\nGROK_STATION_UNITS=celsius\n")
            os.utime(f, ns=(time_ns() + 10**9, time_ns() + 10**9))
            u = T.weather_url()
            self.assertIn("latitude=41.5", u); self.assertIn("longitude=-88.1", u); self.assertIn("celsius", u)
        finally:
            T.STATION_ENV, T.LAT, T.LON = old

    def test_relay_unit_can_read_the_env_file(self):
        unit = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "systemd",
                                 "grok-station-relay.service")).read()
        self.assertIn("SupplementaryGroups=grok", unit)


def time_ns():
    import time as _t
    return _t.time_ns()
