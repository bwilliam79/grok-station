#!/usr/bin/env python3
"""0.13.26: first-time Set up needs the installed setup-params file (bq-0067).

tools/install-files.sh copies the admin payload the relay reads. path=first builds the
handoff from admin/setup-params.txt next to VERSION. A tree staged the way the installer
copies those files must parse pins and post them. A missing or unparseable file, and a
missing station state, each keep their short name. Neither becomes "bad request", and
neither is posted to the main bot.
"""
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402,F401  sets the admin env, then imports grok_admin
import grok_admin as ga  # noqa: E402
from test_gs1316_setup_relay import BODY, Station  # noqa: E402

INSTALL = os.path.join(ROOT, "tools", "install-files.sh")
NAMED = ("missing install pins", "station state")


def installer_admin_sources():
    """Repo paths on the install -m644 line whose destination is /opt/grok-station/admin/."""
    line = None
    with open(INSTALL, encoding="utf-8") as fh:
        for raw in fh:
            s = raw.strip()
            if s.startswith("install -m644 ") and s.endswith(" /opt/grok-station/admin/"):
                line = s
                break
    if not line:
        raise AssertionError("install-files.sh has no install -m644 line for /opt/grok-station/admin/")
    srcs = []
    for tok in line.split()[2:-1]:
        if not (tok.startswith('"$S/') and tok.endswith('"')):
            raise AssertionError("unexpected install token: " + tok)
        srcs.append(os.path.join(ROOT, tok[4:-1]))
    return srcs


def stage_admin(dest):
    """Copy the admin payload with the installer's mode. Returns the source paths."""
    os.makedirs(dest, exist_ok=True)
    srcs = installer_admin_sources()
    subprocess.check_call(["install", "-m644", *srcs, dest + "/"])
    return srcs


class FirstTimeInstall(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.st = Station()
        self.admin = os.path.join(self.st.tmp, "admin")
        shutil.rmtree(self.admin)
        self.srcs = stage_admin(self.admin)

    def tearDown(self):
        self.st.close()

    def _mode(self, name):
        return os.stat(os.path.join(self.admin, name)).st_mode & 0o777

    async def test_installed_pins_build_the_first_time_handoff(self):
        self.assertIn(os.path.join(ROOT, "admin", "setup-params.txt"), self.srcs)
        self.assertEqual(self._mode("setup-params.txt"), self._mode("gs_routine.py"))
        self.assertEqual(self._mode("setup-params.txt"), 0o644)
        status, body = await self.st.send(dict(BODY))
        self.assertEqual(status, 202, body)
        self.assertEqual(len(self.st.calls), 1)
        posted = self.st.calls[0]["json"]
        pin_c, pin_h = self.st.r.setup_install_pins()
        self.assertRegex(pin_c or "", r"^[0-9a-f]{40}$")
        self.assertRegex(pin_h or "", r"^[0-9a-f]{64}$")
        self.assertEqual(posted["template_commit"], pin_c)
        self.assertEqual(posted["template_sha256"], pin_h)
        self.assertEqual(posted["kind"], "setup_relay")
        self.assertEqual(self.st.pendings()[0]["frozen"], posted)
        self.assertNotIn("http", str(posted.get("template_commit")))

    async def test_missing_or_unparseable_pins_are_named_and_not_sent(self):
        path = os.path.join(self.admin, "setup-params.txt")
        if os.path.exists(path):
            os.unlink(path)
        status, body = await self.st.send(dict(BODY))
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "missing install pins"})
        self.assertEqual(self.st.calls, [])
        self.assertNotIn("bad request", body["error"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not a pin line\n")
        status, body = await self.st.send(dict(BODY))
        self.assertEqual((status, body), (400, {"error": "missing install pins"}))
        self.assertEqual(self.st.calls, [])

    async def test_missing_station_state_is_named_and_not_sent(self):
        path = os.path.join(self.admin, "setup-params.txt")
        if not os.path.exists(path):
            subprocess.check_call(["install", "-m644", os.path.join(ROOT, "admin", "setup-params.txt"), path])
        os.unlink(os.path.join(self.st.r.TLS_DIR, "fingerprint"))
        status, body = await self.st.send(dict(BODY))
        self.assertEqual((status, body), (400, {"error": "station state"}))
        self.assertEqual(self.st.calls, [])
        self.assertNotEqual(body["error"], "bad request")

    async def test_other_refusal_stays_bad_request(self):
        status, body = await self.st.send(dict(BODY, mode="desk"))
        self.assertEqual((status, body.get("error")), (400, "bad request"))
        self.assertEqual(self.st.calls, [])


class NamedErrorsReachThePage(unittest.TestCase):
    """public_error already keeps a short ASCII reason. These two must survive that path."""

    def setUp(self):
        ga._SETUP_INFLIGHT.clear()

    def tearDown(self):
        ga._SETUP_INFLIGHT.clear()

    def test_setup_send_does_not_collapse_the_names(self):
        doc = {"rows": {}}
        for i, msg in enumerate(NAMED):
            self.assertEqual(ga.public_error(msg), msg)
            self.assertNotIn("http", msg)
            self.assertTrue(msg.isascii() and msg.isprintable() and len(msg) <= 40)
            code, body = ga.setup_send(
                doc, "otto", known={"otto", "juniper"}, main_slug="juniper", main_name="Juniper",
                main_outdated=False, now=6000 + i,
                poster=lambda target, msg=msg: (400, {"error": msg}),
                name="Otto", wake="Hey Otto", computer="Office computer")
            self.assertEqual(code, 400, msg)
            self.assertEqual(body["error"], msg)
            self.assertNotEqual(body["error"], "bad request")
            self.assertEqual(body["setup"]["rows"]["otto"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
