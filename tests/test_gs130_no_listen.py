"""Home control opens no listening socket. Client only: the source has no bind or listen."""
import os, socket, sys, tempfile, unittest

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes.fake_wled import FakeWLED
from test_gs130_home_flow import drive_transcript, load_mods, write_reg

HOME_SRC = os.path.join(ROOT, "relay", "home.py")


def owned_inodes():
    found = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink("/proc/self/fd/" + name)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            found.add(target[len("socket:["):-1])
    return found


def parse_table(text, inodes, kind):
    """Ports from one /proc/net table. TCP keeps state 0A only. UDP keeps every bound row."""
    ports = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[9] not in inodes:
            continue
        port = int(fields[1].rsplit(":", 1)[1], 16)
        if kind == "udp" or fields[3] == "0A":
            ports.add(port)
    return ports


def owned_ports():
    """(listening TCP ports, bound UDP ports) owned by this process."""
    inodes = owned_inodes()
    tcp, udp = set(), set()
    for path, kind in (("/proc/net/tcp", "tcp"), ("/proc/net/tcp6", "tcp"),
                       ("/proc/net/udp", "udp"), ("/proc/net/udp6", "udp")):
        try:
            text = open(path).read()
        except OSError:
            continue
        got = parse_table(text, inodes, kind)
        if kind == "tcp":
            tcp |= got
        else:
            udp |= got
    return tcp, udp


class Ports(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay, self.home = load_mods()
        self.flag = os.environ.get("HOME_ALLOW_LOOPBACK_FOR_TESTS")
        self.reg_env = os.environ.get("HOME_REGISTRY")
        os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        self.home._offline.clear()

    async def asyncTearDown(self):
        if self.flag is None:
            os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)
        else:
            os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = self.flag
        if self.reg_env is None:
            os.environ.pop("HOME_REGISTRY", None)
        else:
            os.environ["HOME_REGISTRY"] = self.reg_env
        self.home._offline.clear()
        self.home._cache.clear()

    def test_00_source_never_binds(self):
        src = open(HOME_SRC, encoding="utf-8").read()
        for word in ("bind", "listen", "start_server", "create_server"):
            self.assertNotIn(word, src, word)

    def test_01_proc_columns_and_parser(self):
        lines = open("/proc/net/tcp").read().splitlines()
        self.assertGreater(len(lines), 1)
        fields = lines[1].split()
        self.assertGreaterEqual(len(fields), 10)
        self.assertRegex(fields[1], r"^[0-9A-F]+:[0-9A-F]+$")
        self.assertRegex(fields[3], r"^[0-9A-F]{2}$")
        self.assertTrue(fields[9].isdigit())
        sample = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4242 1 0000000000000000 100 0 0 10 0\n"
            "   1: 0100007F:0050 00000000:0000 01 00000000:00000000 00:00000000 00000000  1000        0 4242 1 0000000000000000 100 0 0 10 0\n"
            "   2: 00000000:17C0 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 9999 1 0000000000000000 100 0 0 10 0\n"
        )
        self.assertEqual(parse_table(sample, {"4242"}, "tcp"), {8080})
        self.assertEqual(parse_table(sample, {"9999"}, "tcp"), {6080})
        udp = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "  42: 0100007F:0400 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000        0 77 1 0000000000000000 100 0 0 10 0\n"
        )
        self.assertEqual(parse_table(udp, {"77"}, "udp"), {1024})
        self.assertEqual(parse_table(udp, {"nope"}, "udp"), set())

    def test_02_parser_sees_a_real_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            tcp, _udp = owned_ports()
            self.assertIn(port, tcp)
        finally:
            sock.close()
        tcp, _udp = owned_ports()
        self.assertNotIn(port, tcp)

    async def test_run_intent_opens_no_listener(self):
        fake = FakeWLED()
        await fake.start()
        try:
            before = owned_ports()
            self.assertIn(fake.port, before[0])
            reg = {"version": 1, "integrations": {"wled": {"enabled": True}}, "devices": [{
                "id": "w-port", "kind": "wled", "host": fake.host, "name": "Kitchen Strip",
                "aliases": ["kitchen lights"], "type": "light", "caps": ["onoff"], "presets": {}}]}
            intent = self.home.home_match("hey grok, please turn off the kitchen strip", reg)
            self.assertEqual(intent["action"], "off")
            async with aiohttp.ClientSession() as http:
                ok = await self.home.run_intent(http, intent, lambda line: None)
            self.assertTrue(ok)
            self.assertEqual(fake.requests[-1]["body"], {"on": False})
            after = owned_ports()
            self.assertEqual(before[0] - {fake.port}, after[0] - {fake.port})
            self.assertEqual(before[1], after[1])
            self.assertEqual(before, after)
        finally:
            await fake.close()

    async def test_transcript_opens_no_listener(self):
        fake = FakeWLED()
        await fake.start()
        path = os.path.join(tempfile.mkdtemp(), "integrations.json")
        os.environ["HOME_REGISTRY"] = path
        snaps = {}
        try:
            def prepare():
                write_reg(path, fake.host, "w-port-flow", self.home)

            def ready():
                snaps["before"] = owned_ports()

            def done():
                snaps["after"] = owned_ports()

            out = await drive_transcript(self.relay, "turn off the kitchen strip", prepare, ready, done)
            self.assertIn(fake.port, snaps["before"][0])
            self.assertEqual(snaps["before"], snaps["after"])
            tones = [e for e in out["events"] if e.get("type") == "home_tone"]
            self.assertEqual(tones, [{"type": "home_tone", "ok": True}])
            self.assertEqual([r["body"] for r in fake.requests if r["method"] == "POST"], [{"on": False}])
        finally:
            await fake.close()
