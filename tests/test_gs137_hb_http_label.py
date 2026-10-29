#!/usr/bin/env python3
"""0.13.7 Homebridge http label (Noor SPEC gs-hb-http-label rev1, Nix conditions).

The private check runs on the server, on the IP the station actually connects to (checked_base, after DNS):
10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, fc00::/7, fe80::/10, ::1. Anything else, or a failed resolve,
keeps the full note. It runs on every status read. Boundary addresses are built from integers so the repo carries
no real LAN address literals.
"""
import atexit
import ipaddress
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
if "grok_admin" not in sys.modules:
    # grok_admin reads these paths once at import and keeps them for the whole test process,
    # so the folder is removed when the process exits, not at module teardown.
    _own = tempfile.mkdtemp(prefix="gs137-hb-")
    atexit.register(shutil.rmtree, _own, True)
    os.environ.setdefault("ADMIN_STATE_DIR", os.path.join(_own, "state"))
    os.environ.setdefault("ADMIN_RUN_DIR", os.path.join(_own, "run"))
    os.environ.setdefault("ADMIN_PERSONAS", os.path.join(_own, "personas.json"))
    os.environ.setdefault("ADMIN_RELAY_ENV", os.path.join(_own, "missing-admin-relay.env"))
    os.environ.setdefault("ADMIN_WAKE_CTL", "http://127.0.0.1:9")
    os.makedirs(os.environ["ADMIN_STATE_DIR"], exist_ok=True)
    os.makedirs(os.environ["ADMIN_RUN_DIR"], exist_ok=True)

import grok_admin as ga  # noqa: E402
import gs_integrations as gsi  # noqa: E402


def v4(a, b, c, d):
    return ipaddress.IPv4Address((a << 24) | (b << 16) | (c << 8) | d)


def v6(first16, last=0):
    return ipaddress.IPv6Address((first16 << 112) | last)


class PrivateRanges(unittest.TestCase):
    def test_each_private_range_with_edges(self):
        for lo, hi in (
            (v4(10, 0, 0, 0), v4(10, 255, 255, 255)),
            (v4(172, 16, 0, 0), v4(172, 31, 255, 255)),
            (v4(192, 168, 0, 0), v4(192, 168, 255, 255)),
            (v4(127, 0, 0, 0), v4(127, 255, 255, 255)),
            (v4(169, 254, 0, 0), v4(169, 254, 255, 255)),
            (v6(0xFC00), v6(0xFDFF, (1 << 112) - 1)),
            (v6(0xFE80), v6(0xFEBF, (1 << 112) - 1)),
        ):
            self.assertTrue(gsi.connect_ip_private(lo), lo)
            self.assertTrue(gsi.connect_ip_private(hi), hi)
            self.assertTrue(gsi.connect_ip_private(str(lo)), lo)
        self.assertTrue(gsi.connect_ip_private(ipaddress.IPv6Address(1)))  # ::1

    def test_just_outside_each_range_is_not_private(self):
        for ip in (
            v4(9, 255, 255, 255), v4(11, 0, 0, 0),
            v4(172, 15, 255, 255), v4(172, 32, 0, 0),
            v4(192, 167, 255, 255), v4(192, 169, 0, 0),
            v4(126, 255, 255, 255), v4(128, 0, 0, 0),
            v4(169, 253, 255, 255), v4(169, 255, 0, 0),
            v6(0xFBFF, (1 << 112) - 1), v6(0xFE00),
            v6(0xFE7F, (1 << 112) - 1), v6(0xFEC0),
            ipaddress.IPv6Address(0), ipaddress.IPv6Address(2),
        ):
            self.assertFalse(gsi.connect_ip_private(ip), ip)

    def test_public_and_junk_are_not_private(self):
        for ip in ("203.0.113.9", "198.51.100.7", "8.8.8.8", "2001:db8::1", "2606:4700::1111", "", None, "nope"):
            self.assertFalse(gsi.connect_ip_private(ip), ip)

    def test_ipv4_mapped_v6_uses_the_v4_rule(self):
        self.assertTrue(gsi.connect_ip_private(ipaddress.IPv6Address("::ffff:" + str(v4(10, 0, 0, 5)))))
        self.assertFalse(gsi.connect_ip_private(ipaddress.IPv6Address("::ffff:203.0.113.9")))


class ResolvedAddress(unittest.TestCase):
    def setUp(self):
        self.saved = gsi.resolve_host, gsi.station_addresses
        gsi.station_addresses = lambda: ["10.0.0.8"]

    def tearDown(self):
        gsi.resolve_host, gsi.station_addresses = self.saved

    def test_name_checked_on_the_resolved_ip(self):
        gsi.resolve_host = lambda host: ["10.0.0.5"]
        self.assertTrue(gsi.hb_http_private("http://homebridge.local:8581"))

    def test_name_resolving_public_is_not_private(self):
        gsi.resolve_host = lambda host: ["203.0.113.9"]
        self.assertFalse(gsi.hb_http_private("http://homebridge.local:8581"))

    def test_resolve_failure_is_not_private(self):
        gsi.resolve_host = lambda host: []
        self.assertFalse(gsi.hb_http_private("http://homebridge.local:8581"))

        def boom(host):
            raise OSError("no dns")
        gsi.resolve_host = boom
        self.assertFalse(gsi.hb_http_private("http://homebridge.local:8581"))

    def test_literal_private_and_https(self):
        self.assertTrue(gsi.hb_http_private("http://10.0.0.5:8581"))
        self.assertFalse(gsi.hb_http_private("https://10.0.0.5:8581"))
        self.assertFalse(gsi.hb_http_private(""))
        self.assertFalse(gsi.hb_http_private(None))


class StatusView(unittest.TestCase):
    """GET /api/integrations view: http_private on a Connected card, re-checked on every read."""

    def setUp(self):
        self.saved = gsi.resolve_host, gsi.station_addresses, gsi.hb_block_kind
        gsi.station_addresses = lambda: ["10.0.0.8"]
        gsi.hb_block_kind = lambda: None
        self.h = object.__new__(ga.Handler)
        self.doc = {"version": 1,
                    "integrations": {"homebridge": {"enabled": True, "base": "http://homebridge.local:8581"}},
                    "devices": [{"id": "hb1", "kind": "homebridge", "name": "lamp", "aliases": [], "type": "light",
                                 "uid": "u1"}]}

    def tearDown(self):
        gsi.resolve_host, gsi.station_addresses, gsi.hb_block_kind = self.saved

    def _hb(self):
        return self.h._integration_view(self.doc)["integrations"]["homebridge"]

    def test_rechecked_on_every_read(self):
        gsi.resolve_host = lambda host: ["10.0.0.5"]
        self.assertIs(self._hb()["http_private"], True)
        gsi.resolve_host = lambda host: ["203.0.113.9"]
        self.assertIs(self._hb()["http_private"], False)
        gsi.resolve_host = lambda host: []
        self.assertIs(self._hb()["http_private"], False)
        gsi.resolve_host = lambda host: ["10.0.0.5"]
        self.assertIs(self._hb()["http_private"], True)

    def test_not_connected_never_gets_the_label(self):
        gsi.resolve_host = lambda host: ["10.0.0.5"]
        self.doc["devices"] = []
        hb = self._hb()
        self.assertEqual(hb["status"], "signed_in")
        self.assertIs(hb["http_private"], False)


class Client(unittest.TestCase):
    def setUp(self):
        self.js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        self.css = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

    def test_card_uses_pill_only_when_server_says_private(self):
        self.assertIn('hb.http_private === true ? hbHttpPill(hb.base) : hbHttpWarn(hb.base)', self.js)

    def test_full_note_unchanged_on_add_edit_and_verify(self):
        self.assertEqual(len(re.findall(r"(?<!function )hbHttpWarn\((?:checked|base)\)", self.js)), 2)
        fn = self.js.split("function hbHttpWarn(base)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("<b>Not encrypted.</b> This address uses http", fn)

    def test_pill_markup(self):
        fn = self.js.split("function hbHttpPill(base)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('aria-label="Not encrypted (http). Show details"', fn)
        self.assertIn('http <span aria-hidden="true">ⓘ</span>', fn)
        self.assertIn('role="dialog"', fn)
        self.assertIn("${HB_HTTP_NOTE}", fn)
        self.assertIn('id="hbpopx">Close</button>', fn)
        note = self.js.split("const HB_HTTP_NOTE = ", 1)[1].split("\n", 1)[0]
        self.assertIn("<b>Not encrypted.</b> This address uses http, so the connection isn't encrypted. That's fine "
                      "on your home network. To encrypt it, turn on https in Homebridge's settings and use the https "
                      "address.", note)

    def test_focus_and_escape(self):
        fn = self.js.split("function hbPillWire(root)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('ev.key === "Escape"', fn)
        self.assertIn("pill.focus()", fn)
        self.assertIn('$("#hbpopx", pop).focus()', fn)

    def test_css_tokens_and_hit_area(self):
        rules = "\n".join(l for l in self.css.splitlines() if ".hbpill" in l or ".hbpop" in l or ".hbaddr" in l)
        self.assertIn("gap:8px", rules)
        self.assertIn("color:#A89B8C", rules)
        self.assertIn("border:1px solid rgba(168,155,140,.4)", rules)
        self.assertIn("border-radius:999px", rules)
        self.assertIn("width:max(100%,24px);height:max(100%,24px)", rules)
        self.assertIn("width:max(100%,44px);height:max(100%,44px)", rules)
        self.assertIn(".hbpill:hover{color:#F5EFE6;border-color:#A89B8C}", rules)
        self.assertIn(".hbpill:focus-visible{outline:2px solid #3FD3B6;outline-offset:2px}", rules)
        self.assertIn(".hbpill:active{background:rgba(245,239,230,.08)}", rules)
        self.assertIn("position:fixed;left:0;right:0;bottom:0", rules)  # bottom sheet on phone


    def test_address_line_under_status_line(self):
        # Noor answer: directly under "Connected · N devices", host:port, 8px gap, then the pill.
        self.assertIn('<p class="sub">${esc(hbSub)}</p>${hbStatus === "not_set_up" ? "" : hb.http_private === true ? hbHttpPill(hb.base)', self.js)
        fn = self.js.split("function hbHttpPill(base)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('<p class="hbaddrl"><span class="hbaddrtx">${esc(addr)}</span><button', fn)
        self.assertIn("hbHostPort(base)", fn)
        rule = [l for l in self.css.splitlines() if l.startswith(".hbaddrl{")][0]
        for part in ("display:flex", "align-items:center", "gap:8px", "font-size:var(--fs-body)", "color:#A89B8C"):
            self.assertIn(part, rule)
        self.assertIn("--fs-body:15px", self.css)

    def test_host_port_only(self):
        import json, subprocess
        start = self.js.index("function hbHostPort(")
        src = self.js[start:self.js.index("\n  }\n", start) + 4]
        cases = ["http://homebridge.local:8581", "http://user:tok@homebridge.local:8581/api/x?token=1#f",
                 "http://[fd00::5]:8581/path", "homebridge.local:8581/p", "u@homebridge.local:8581/p"]
        out = subprocess.run(["node", "-e", src + "process.stdout.write(JSON.stringify(%s.map(hbHostPort)))" % json.dumps(cases)],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), ["homebridge.local:8581", "homebridge.local:8581", "[fd00::5]:8581",
                                                  "homebridge.local:8581", "homebridge.local:8581"])


if __name__ == "__main__":
    unittest.main()
