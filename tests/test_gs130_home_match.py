"""WLED grammar, registry reload, host checks, and the JSON call built for each action."""
import asyncio, copy, json, os, re, sys, tempfile, time, unittest

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import home

REG = {
    "version": 1,
    "integrations": {"wled": {"enabled": True}},
    "devices": [
        {"id": "w-3f9a", "kind": "wled", "host": "wled-kitchen.local", "name": "Kitchen Strip",
         "aliases": ["kitchen lights"], "type": "light",
         "caps": ["onoff", "bri", "color", "preset"], "presets": {"party": 3, "white": 7}},
        {"id": "w-porch", "kind": "wled", "host": "wled-porch.local", "name": "Porch Light",
         "aliases": [], "type": "light", "caps": ["onoff"], "presets": {}},
        {"id": "w-hall", "kind": "wled", "host": "wled-hall.local", "name": "Hall",
         "aliases": [], "type": "light", "caps": ["onoff", "preset"], "presets": {"red": 4, "party": 1}},
        {"id": "w-mom", "kind": "wled", "host": "wled-lamp.local", "name": "Mom's Lamp",
         "aliases": [], "type": "light", "caps": ["onoff"], "presets": {}},
    ],
}


class _Resp:
    def __init__(self, status, data):
        self.status = status
        self._data = data
        self.content = self

    async def read(self, n):
        return self._data[:n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Stub:
    def __init__(self, responses=None):
        self.responses = list(responses or [(200, b"{}")])
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": str(url), "json": kw.get("json"), "kw": kw})
        status, data = self.responses.pop(0) if self.responses else (200, b"{}")
        return _Resp(status, data)


class Names(unittest.TestCase):
    def test_normalize_and_clashes(self):
        self.assertEqual(home.normalize_name("Porch Light"), "porch")
        self.assertEqual(home.normalize_name("the porch lights"), "porch")
        self.assertEqual(home.normalize_name("  THE   Porch  "), "porch")
        self.assertEqual(home.normalize_name("Mom's Lamp"), "moms lamp")
        self.assertEqual(home.normalize_name("moms lamp"), "moms lamp")
        self.assertNotEqual(home.normalize_name("under-cabinet"), home.normalize_name("under cabinet"))
        self.assertEqual(home.normalize_name("the lights"), "")
        self.assertEqual(home.normalize_name("Light"), "")
        self.assertTrue(home.name_clashes("Porch Light", ["the porch lights", "kitchen"]))
        self.assertTrue(home.name_clashes("Mom's Lamp", ["moms lamp"]))
        self.assertTrue(home.name_clashes("the light", ["lights"]))
        self.assertFalse(home.name_clashes("under-cabinet", ["under cabinet"]))
        self.assertFalse(home.name_clashes("Porch Light", ["kitchen strip"]))
        self.assertIn("normalize_name equality", home.name_clashes.__doc__)
        self.assertIn("punctuation", home.normalize_name.__doc__.lower())

    def test_colors_and_percent(self):
        self.assertEqual(len(home.COLORS), 16)
        self.assertEqual(home.COLORS["red"], (255, 0, 0))
        self.assertEqual(home.COLORS["warm white"], (255, 180, 70))
        self.assertEqual(home.COLORS["cool white"], (200, 220, 255))
        self.assertEqual(home.COLORS["gold"], (255, 160, 0))
        self.assertEqual(home.bri_from_pct(0), 0)
        self.assertEqual(home.bri_from_pct(40), 102)
        self.assertEqual(home.bri_from_pct(50), 128)
        self.assertEqual(home.bri_from_pct(100), 255)
        self.assertEqual(home.BRI_STEP, 51)


class Match(unittest.TestCase):
    def match(self, text, reg=None):
        return home.home_match(text, REG if reg is None else reg)

    def test_positive_commands(self):
        cases = (
            ("turn off the kitchen strip", "off", "w-3f9a", {}),
            ("turn the kitchen strip off", "off", "w-3f9a", {}),
            ("switch on the porch light", "on", "w-porch", {}),
            ("switch the porch off", "off", "w-porch", {}),
            ("Hey Grok, turn off the kitchen strip", "off", "w-3f9a", {}),
            ("hey grok, could you please dim the kitchen strip to 25 percent?", "bri", "w-3f9a", {"pct": 25}),
            ("please please turn on the kitchen strip", "on", "w-3f9a", {}),
            ("turn on the kitchen strip please", "on", "w-3f9a", {}),
            ("can you please turn off kitchen lights", "off", "w-3f9a", {}),
            ("turn off the kitchen", "off", "w-3f9a", {}),
            ("set the kitchen lights to 0", "bri", "w-3f9a", {"pct": 0}),
            ("set kitchen strip to 100 percent", "bri", "w-3f9a", {"pct": 100}),
            ("dim kitchen strip to 40", "bri", "w-3f9a", {"pct": 40}),
            ("brighten the kitchen strip", "brighten", "w-3f9a", {}),
            ("dim the kitchen strip", "dim", "w-3f9a", {}),
            ("set the kitchen strip to red", "color", "w-3f9a", {"rgb": [255, 0, 0]}),
            ("set kitchen strip to warm white", "color", "w-3f9a", {"rgb": [255, 180, 70]}),
            ("set the kitchen strip to white", "color", "w-3f9a", {"rgb": [255, 255, 255]}),
            ("set the kitchen strip to party", "preset", "w-3f9a", {"ps": 3}),
            ("set the kitchen strip to preset party", "preset", "w-3f9a", {"ps": 3}),
            ("set kitchen strip to preset white", "preset", "w-3f9a", {"ps": 7}),
            ("kitchen strip preset party", "preset", "w-3f9a", {"ps": 3}),
            ("Turn On The Porch Light", "on", "w-porch", {}),
            ("hey max, please turn off the porch", "off", "w-porch", {}),
            ("turn off Mom\u2019s lamp", "off", "w-mom", {}),
            ("set hall to preset red", "preset", "w-hall", {"ps": 4}),
            ("hall preset red", "preset", "w-hall", {"ps": 4}),
        )
        for text, action, ident, extra in cases:
            got = self.match(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["action"], action, text)
            self.assertEqual(got["id"], ident, text)
            self.assertEqual(got["kind"], "wled", text)
            self.assertNotIn("name", got, text)
            for key, val in extra.items():
                self.assertEqual(got.get(key), val, text)
            if action != "bri":
                self.assertNotIn("pct", got, text)

    def test_negative_commands(self):
        for text in (
            "what color is the kitchen strip",
            "what color is the kitchen strip?",
            "turn off the lights",
            "lights off",
            "everything off",
            "ask Max to turn off the light",
            "ask Max to turn off the kitchen strip",
            "turn off the garage",
            "turn off the kitchen strip what color is it",
            "dim kitchen strip to 101",
            "set kitchen strip to 101 percent",
            "brighten the porch",
            "dim the porch",
            "set the porch light to red",
            "dim the porch to 40",
            "please please please turn on the kitchen strip",
            "",
            "turn on",
            "kitchen strip",
            "set the kitchen strip to chartreuse",
            "set the kitchen strip to preset nope",
            "set hall to red",
            "brighten the hall",
            "turn off the lamp",
            "is the kitchen strip on",
        ):
            self.assertIsNone(self.match(text), text)
        self.assertIsNone(home.home_match(None, REG))
        self.assertIsNone(home.home_match("turn off the kitchen strip", None))
        self.assertIsNone(home.home_match("turn off the kitchen strip", {}))

    def test_clash_and_disabled_do_not_guess(self):
        reg = copy.deepcopy(REG)
        reg["devices"].append({"id": "w-other", "kind": "wled", "host": "wled-other.local", "name": "Other",
                                "aliases": ["kitchen lights"], "type": "light", "caps": ["onoff"]})
        self.assertIsNone(self.match("turn off kitchen lights", reg))
        self.assertIsNone(self.match("turn off the kitchen", reg))
        off = copy.deepcopy(REG)
        off["integrations"]["wled"]["enabled"] = False
        self.assertIsNone(self.match("turn off the kitchen strip", off))
        off["integrations"]["wled"]["enabled"] = 1
        self.assertIsNone(self.match("turn off the kitchen strip", off))

    def test_hook_is_after_recipe_and_before_heard(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        self.assertRegex(src, r"(?m)^import home$")
        take = src.index("taken = recipe_taken(heard, post_recipe_cmd)")
        hook = src.index("intent = home.home_match(heard, home.load_registry())")
        heard = src.index('st["turn_heard"] = True')
        self.assertLess(take, hook)
        self.assertLess(hook, heard)
        block = src[hook:heard]
        self.assertIn('{"type": "home_tone", "ok": bool(ok)}', block)
        self.assertIn("end_routed_turn()", block)
        self.assertNotIn("play_fixed", block)
        self.assertNotIn('"heard"', block)
        self.assertNotIn("response.create", block)
        self.assertNotIn('st["turn_heard"]', block)
        line = [ln for ln in open(os.path.join(ROOT, "install.sh")) if ln.startswith("for f in relay.py")][0]
        self.assertIn("home.py", line)
        opens = re.findall(r"open\([^)]*\)", open(os.path.join(ROOT, "relay", "home.py"), encoding="utf-8").read())
        self.assertEqual(opens, ['open(path, "rb")'])


class Host(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)

    def test_private_local_and_loopback_flag(self):
        always = {
            "wled-kitchen.local": True,
            "wled-kitchen.local:80": True,
            "wled-kitchen.local.": True,
            "10.0.0.5": True,
            "10.0.0.5:8080": True,
            "192.168.4.20": True,
            "169.254.1.1": True,
            "169.254.169.254": False,
            "fd00::1": True,
            "[fd00::1]:80": True,
            "fe80::1": True,
            "::ffff:10.0.0.5": True,
            "8.8.8.8": False,
            "203.0.113.5": False,
            "192.0.2.1": False,
            "0.0.0.0": False,
            "255.255.255.255": False,
            "2001:db8::1": False,
            "::ffff:8.8.8.8": False,
            "::ffff:203.0.113.5": False,
            "example.com": False,
            "localhost": False,
            "http://wled-kitchen.local": False,
            "user@wled-kitchen.local": False,
            "wled-kitchen.local:0": False,
            "wled-kitchen.local:65536": False,
            "10.0.0.5:0": False,
            "": False,
        }
        loop = ("127.0.0.1", "127.0.0.1:9", "::1", "[::1]:9", "::ffff:127.0.0.1")
        os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)
        for host, want in always.items():
            self.assertEqual(home.host_ok(host), want, host)
        for host in loop:
            self.assertFalse(home.host_ok(host), host)
        os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        for host, want in always.items():
            self.assertEqual(home.host_ok(host), want, host)
        for host in loop:
            self.assertTrue(home.host_ok(host), host)
        self.assertIn("HOME_ALLOW_LOOPBACK_FOR_TESTS", home.host_ok.__doc__)
        self.assertIn("Tests only", home.__doc__)


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "integrations.json")
        self.env = os.environ.get("HOME_REGISTRY")
        home._cache.clear()

    def tearDown(self):
        if self.env is None:
            os.environ.pop("HOME_REGISTRY", None)
        else:
            os.environ["HOME_REGISTRY"] = self.env
        home._cache.clear()

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return self.path

    def test_load_filters_and_reloads_on_mtime(self):
        doc = copy.deepcopy(REG)
        doc["devices"].append({"id": "h-91c2", "kind": "homebridge", "host": "homebridge.local",
                                "name": "Porch Switch", "aliases": [], "type": "switch", "caps": ["onoff"]})
        self.write(doc)
        got = home.load_registry(self.path)
        ids = [d["id"] for d in got["devices"]]
        self.assertEqual(ids, ["w-3f9a", "w-porch", "w-hall", "w-mom"])
        self.assertNotIn("homebridge", json.dumps(got))
        self.assertIs(home.load_registry(self.path), got)
        mtime = os.stat(self.path).st_mtime_ns
        doc["devices"][0]["id"] = "w-new"
        self.write(doc)
        os.utime(self.path, ns=(mtime, mtime))
        self.assertIs(home.load_registry(self.path), got)
        self.assertEqual(got["devices"][0]["id"], "w-3f9a")
        os.utime(self.path, ns=(mtime + 10**9, mtime + 10**9))
        fresh = home.load_registry(self.path)
        self.assertEqual(fresh["devices"][0]["id"], "w-new")
        self.assertIsNot(fresh, got)

    def test_missing_malformed_and_oversized_are_empty(self):
        missing = os.path.join(self.tmp, "nope.json")
        self.assertEqual(home.load_registry(missing)["devices"], [])
        self.write({"version": 1, "integrations": {"wled": {"enabled": True}}, "devices": REG["devices"][:1]})
        self.assertEqual(home.load_registry(missing)["devices"], [])  # still missing, cached
        with open(missing, "w", encoding="utf-8") as fh:
            fh.write("{")
        self.assertEqual(home.load_registry(missing)["devices"], [])
        os.utime(missing, ns=(time.time_ns(), time.time_ns()))
        with open(missing, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "integrations": {"wled": {"enabled": True}}, "devices": REG["devices"][:1]}, fh)
        self.assertEqual(home.load_registry(missing)["devices"], [])
        body = json.dumps({"version": 1, "integrations": {"wled": {"enabled": True}},
                            "devices": [REG["devices"][0]]}).encode()
        pad = body + b" " * (home._READ_MAX - len(body)) + b"x"
        self.assertGreater(len(pad), home._READ_MAX)
        with open(self.path, "wb") as fh:
            fh.write(pad)
        home._cache.pop(self.path, None)
        self.assertEqual(home.load_registry(self.path)["devices"], [])
        os.environ["HOME_REGISTRY"] = self.path
        home._cache.clear()
        self.write(REG)
        self.assertEqual(home.load_registry()["devices"][0]["id"], "w-3f9a")

    def test_string_aliases_are_not_split(self):
        doc = copy.deepcopy(REG)
        doc["devices"] = [doc["devices"][0]]
        doc["devices"][0]["aliases"] = "kitchen lights"
        self.write(doc)
        got = home.load_registry(self.path)
        self.assertEqual(got["devices"][0]["aliases"], [])
        self.assertIsNone(home.home_match("turn off kitchen lights", got))
        self.assertEqual(home.home_match("turn off the kitchen strip", got)["action"], "off")


class Perform(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        home._offline.clear()
        self.logs = []
        self.flag = os.environ.get("HOME_ALLOW_LOOPBACK_FOR_TESTS")
        os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)

    async def asyncTearDown(self):
        if self.flag is None:
            os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)
        else:
            os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = self.flag
        home._offline.clear()

    async def _run(self, text, stub, reg=None):
        intent = home.home_match(text, REG if reg is None else reg)
        self.assertIsNotNone(intent, text)
        ok = await home.run_intent(stub, intent, self.logs.append)
        return ok, intent

    async def test_bodies_log_line_and_no_dial_of_a_refused_host(self):
        stub = Stub([(200, b"{}")] * 8)
        ok, _ = await self._run("turn off the kitchen strip", stub)
        self.assertTrue(ok)
        ok, _ = await self._run("set kitchen strip to 50 percent", stub)
        self.assertTrue(ok)
        ok, _ = await self._run("set kitchen strip to 40", stub)
        self.assertTrue(ok)
        ok, _ = await self._run("set the kitchen strip to red", stub)
        self.assertTrue(ok)
        ok, _ = await self._run("kitchen strip preset party", stub)
        self.assertTrue(ok)
        self.assertEqual([c["json"] for c in stub.calls], [
            {"on": False},
            {"on": True, "bri": 128},
            {"on": True, "bri": 102},
            {"on": True, "seg": [{"col": [[255, 0, 0]]}]},
            {"ps": 3},
        ])
        for call in stub.calls:
            self.assertEqual(call["url"], "http://wled-kitchen.local/json/state")
            self.assertFalse(call["kw"]["allow_redirects"])
            self.assertEqual(call["kw"]["max_redirects"], 0)
            self.assertEqual(call["kw"]["timeout"].total, 3)
        self.assertEqual(self.logs, [
            "home wled off ok", "home wled bri ok", "home wled bri ok", "home wled color ok", "home wled preset ok"])
        blob = "\n".join(self.logs)
        for banned in ("Kitchen", "wled-kitchen", "party", "red", "255", "128"):
            self.assertNotIn(banned, blob)

        bright = Stub([(200, b'{"bri": 128}'), (200, b"{}"), (200, b'{"bri": 250}'), (200, b"{}"),
                       (200, b'{"bri": 10}'), (200, b"{}")])
        self.logs.clear()
        self.assertTrue((await self._run("brighten the kitchen strip", bright))[0])
        self.assertTrue((await self._run("brighten the kitchen strip", bright))[0])
        self.assertTrue((await self._run("dim the kitchen strip", bright))[0])
        self.assertEqual(bright.calls[0]["method"], "GET")
        self.assertIsNone(bright.calls[0]["json"])
        self.assertEqual(bright.calls[1]["json"], {"bri": 179})
        self.assertEqual(bright.calls[3]["json"], {"bri": 255})
        self.assertEqual(bright.calls[5]["json"], {"bri": 0})
        self.assertNotIn("on", bright.calls[1]["json"])

        refused = Stub()
        self.logs.clear()
        intent = {"id": "w-loop", "kind": "wled", "host": "127.0.0.1:9", "action": "off"}
        self.assertFalse(await home.run_intent(refused, intent, self.logs.append))
        self.assertEqual(refused.calls, [])
        self.assertEqual(self.logs, ["home wled off fail"])
        os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        intent = {"id": "w-pub", "kind": "wled", "host": "203.0.113.5", "action": "on"}
        self.assertFalse(await home.run_intent(refused, intent, self.logs.append))
        self.assertEqual(refused.calls, [])
        ipv6 = Stub()
        intent = {"id": "w-6", "kind": "wled", "host": "[fd00::1]:80", "action": "on"}
        self.assertTrue(await home.run_intent(ipv6, intent, self.logs.append))
        self.assertEqual(ipv6.calls[0]["url"], "http://[fd00::1]:80/json/state")

    async def test_offline_timestamp_sticks_until_success(self):
        fail = Stub([(500, b"no"), (500, b"no")])
        intent = {"id": "w-off-1", "kind": "wled", "host": "wled-kitchen.local", "action": "off"}
        self.assertFalse(await home.run_intent(fail, intent, self.logs.append))
        first = home.offline_since("w-off-1")
        self.assertIsInstance(first, float)
        await asyncio.sleep(0.02)
        self.assertFalse(await home.run_intent(fail, intent, self.logs.append))
        self.assertEqual(home.offline_since("w-off-1"), first)
        self.assertTrue(await home.run_intent(Stub([(200, b"{}")]), intent, self.logs.append))
        self.assertIsNone(home.offline_since("w-off-1"))
        self.logs.clear()
        bad = {"id": "w-off-1", "kind": "wled", "host": "wled-kitchen.local", "action": "explode"}
        self.assertFalse(await home.run_intent(Stub(), bad, self.logs.append))
        self.assertEqual(self.logs, ["home wled command fail"])
        self.assertNotIn("explode", self.logs[0])

        class Boom:
            def request(self, *a, **k):
                raise aiohttp.ClientConnectionError("nope")

        self.assertFalse(await home.run_intent(Boom(), intent, self.logs.append))
        self.assertIsNotNone(home.offline_since("w-off-1"))
