#!/usr/bin/env python3
"""0.13.35: the main bot counts as behind unless it is on exactly this release.

Live on 0.13.34: the main bot's signed status named 0.13.33. Its 0.13.33 instructions equal 0.13.34's apart from
the release line and the pinned update command, so the 0.12.10 same-content rule marked it done. The Bots page
showed no Update button for it and the setup gate did not answer "Update the main bot first." Set up then sent
0.13.34's routine, update and install pins, the main bot checked them against its 0.13.33 literals, and declined
("couldn't pass it on").

Now finish_rows takes the main bot's slug and skips both same-content shortcuts for it, so the row is needs, the
Update button shows, and setup_send returns 409. Other bots keep the same-content rule. The relay also refuses a
setup_relay unless the main bot's signed status names this release, or it has no status and was paired while
this release was current. A failed finish check or a missing main row counts as outdated. On 0.13.34 (63704ff)
these fail.
Names and keys are made up.
"""
import asyncio, base64, json, os, re, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)

ga = ta.ga
gs_routine = ga.gs_routine
REL = re.search(r"release (\d+\.\d+\.\d+)\.", open(os.path.join(ROOT, "admin", "routine-cloud.txt")).readline()).group(1)
STATION = "https://st.example"
FP = ":".join(["AB"] * 32)
PUB = "A" * 43 + "="
C_OLD, C_NEW = "1" * 40, "2" * 40
S_OLD, S_NEW = "a" * 64, "b" * 64
PINS = {"commit": C_NEW, "update_commit": "3" * 40, "files": {"scripts/gs-update.sh": S_NEW}}
KEY = base64.b64encode(b"\x07" * 32).decode()


def _prev(rel):
    maj, mi, pa = (int(x) for x in rel.split("."))
    return "%d.%d.%d" % ((maj, mi, pa - 1) if pa else (maj, mi - 1, 99))


PREV = _prev(REL)


def _old_instr(slug):
    """The hash a cloud bot on PREV signs: this template with PREV's header and PREV's pinned update command."""
    rest = gs_routine.TEMPLATES["cloud"].split("\n", 1)[1]
    text = gs_routine.render("Grok Station instructions, release %s.\n" % PREV + rest, "cloud", slug, C_OLD, S_OLD,
                             STATION, FP, PUB)
    return gs_routine.instr_hash(text)


def _bot(slug, update=None):
    return {"slug": slug, "name": slug.title(), "paired": True, "public_key": KEY, "webhook": {"configured": True},
            "key_mode": "cloud", "update": update or {}}


class _Env(unittest.TestCase):
    """This tree's release, a fingerprint table where PREV and REL run the same content, and fixed pins."""

    def setUp(self):
        table = [{"release": PREV, "update_commit": C_OLD, "update_sha": S_OLD, "fp": {"home": "c" * 64, "cloud": "c" * 64}},
                 {"release": REL, "update_commit": C_NEW, "update_sha": S_NEW, "fp": {"home": "c" * 64, "cloud": "c" * 64}}]
        self.fin = {"sent": {}, "copied": {}, "updaters": {}, "setup": {}, "failed": {}}
        self.saved = (ga.version, ga.bot_content.read_table, ga.deploy_pins, ga.setup_owner_name, ga.read_finish)
        ga.version = lambda: REL
        ga.bot_content.read_table = lambda path=None: table
        ga.deploy_pins = lambda: PINS
        ga.setup_owner_name = lambda: ""
        ga.read_finish = lambda: json.loads(json.dumps(self.fin))

    def tearDown(self):
        (ga.version, ga.bot_content.read_table, ga.deploy_pins, ga.setup_owner_name, ga.read_finish) = self.saved

    def current_instr(self, slug):
        return gs_routine.instr_hash(ga.render_bot_routine(_bot(slug), STATION, FP, PUB, PINS))

    def rows(self, bots, main):
        view = ga.finish_rows(bots, STATION, FP, PUB, PINS, self.fin, now=1000, main=main)
        return {r["slug"]: r["state"] for g in view["computers"] for r in g["bots"]}, view["behind"]


class FinishRows(_Env):
    def test_main_on_older_release_with_same_content_needs_update(self):
        old = {"release": PREV, "instr": _old_instr("juno"), "at": 100}
        self.assertTrue(ga.same_content(_bot("juno"), PREV, old["instr"], STATION, FP, PUB))  # the 0.12.10 case
        states, behind = self.rows([_bot("juno", old)], main="juno")
        self.assertEqual((states["juno"], behind), ("needs", 1))

    def test_non_main_on_older_release_with_same_content_stays_done(self):
        kai = _bot("kai", {"release": PREV, "instr": _old_instr("kai"), "at": 100})
        cur = _bot("juno", {"release": REL, "instr": self.current_instr("juno"), "at": 100})
        states, behind = self.rows([kai, cur], main="juno")
        self.assertEqual((states["kai"], states["juno"], behind), ("done", "done", 0))
        states, _ = self.rows([kai], main=None)  # no main bot set: unchanged
        self.assertEqual(states["kai"], "done")

    def test_main_on_exact_release_is_done(self):
        u = {"release": REL, "instr": self.current_instr("juno"), "at": 100}
        states, behind = self.rows([_bot("juno", u)], main="juno")
        self.assertEqual((states["juno"], behind), ("done", 0))

    def test_main_paired_on_older_release_without_status_needs_update(self):
        self.fin["setup"] = {"juno": {"release": PREV, "at": 50}, "kai": {"release": PREV, "at": 50}}
        states, _ = self.rows([_bot("juno"), _bot("kai")], main="juno")
        self.assertEqual((states["juno"], states["kai"]), ("needs", "done"))

    def test_main_paired_on_this_release_without_status_is_done(self):
        self.fin["setup"] = {"juno": {"release": REL, "at": 50}}
        states, _ = self.rows([_bot("juno")], main="juno")
        self.assertEqual(states["juno"], "done")


class SetupGate(_Env):
    """bots_setup_send through _setup_ctx with a fake relay; /api/finish-update from the same relay read."""

    def drive(self, main_update, finish_rows=None):
        raw = {"bots": [_bot("juno", main_update), _bot("otto")], "list_source": "juno", "station_pub": PUB,
               "imported": {}}
        calls, sent = [], []

        def relay_call(method, url, body=None, **k):
            calls.append((method, url, body))
            if method == "GET" and url == "/admin/bots":
                return 200, json.loads(json.dumps(raw))
            if url == "/admin/bots/setup-relay":
                return 202, {"ok": True}
            return 404, {"error": "not found"}

        class H:
            def send(self, code, body):
                sent.append((code, body))
                return code

            def bots_unconfigured(self):
                return self.send(422, {"error": "not configured"})

            def _station_base(self):
                return STATION

            _setup_ctx = ga.Handler._setup_ctx
            _finish_inputs = ga.Handler._finish_inputs

        store = {"rows": {}}
        saved = (ga.relay_call, ga.station_base, ga.tls_info, ga.setup_load, ga.setup_merge, ga.audit,
                 ga.finish_rows)
        ga.relay_call = relay_call
        if finish_rows is not None:
            ga.finish_rows = finish_rows
        ga.station_base = lambda: STATION
        ga.tls_info = lambda: {"fingerprint": FP}
        ga.setup_load = lambda: json.loads(json.dumps(store))
        ga.setup_merge = lambda d, slug: store.update(d)
        ga.audit = lambda *a, **k: None
        try:
            page = {}
            if finish_rows is None:
                ga.Handler.api_get_finish_update(H(), "/api/finish-update", {}, {})
                page = {r["slug"]: r["state"] for g in sent[-1][1]["computers"] for r in g["bots"]}
            code = ga.Handler.bots_setup_send(H(), {"slug": "otto"})
        finally:
            (ga.relay_call, ga.station_base, ga.tls_info, ga.setup_load, ga.setup_merge, ga.audit,
             ga.finish_rows) = saved
        posts = [c for c in calls if c[1] == "/admin/bots/setup-relay"]
        return page, code, sent[-1][1], posts

    def test_main_behind_with_same_content_is_refused_409(self):
        page, code, body, posts = self.drive({"release": PREV, "instr": _old_instr("juno"), "at": 100})
        self.assertEqual(page["juno"], "needs")  # the Update button shows on the main bot's row
        self.assertEqual(code, 409)
        self.assertEqual(body.get("error"), "Update the main bot first.")
        self.assertEqual(posts, [])

    def test_finish_check_raising_fails_closed_409(self):
        def boom(*a, **k):
            raise RuntimeError("finish check broke")
        _, code, body, posts = self.drive({"release": REL, "instr": self.current_instr("juno"), "at": 100}, boom)
        self.assertEqual((code, body.get("error"), posts), (409, "Update the main bot first.", []))

    def test_main_row_missing_from_view_fails_closed_409(self):
        real = ga.finish_rows

        def without_main(bots, *a, **k):
            view = real(bots, *a, **k)
            for g in view["computers"]:
                g["bots"] = [r for r in g["bots"] if r["slug"] != "juno"]
            return view
        _, code, body, posts = self.drive({"release": REL, "instr": self.current_instr("juno"), "at": 100},
                                          without_main)
        self.assertEqual((code, body.get("error"), posts), (409, "Update the main bot first.", []))

    def test_main_on_exact_release_proceeds(self):
        page, code, body, posts = self.drive({"release": REL, "instr": self.current_instr("juno"), "at": 100})
        self.assertEqual(page["juno"], "done")
        self.assertEqual(code, 200, body)
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0][2]["slug"], posts[0][2]["target"]), ("juno", "otto"))


class RelayRefuses(unittest.IsolatedAsyncioTestCase):
    """Defense in depth, fail closed: the relay sends setup_relay only through a main bot on this release.

    Yes when the main bot's signed status names this release, or it has no signed status and its key was pinned
    while this release was current. Every other case is 409 "Update the main bot first." with no post.
    """

    def setUp(self):
        import test_gs1316_setup_relay as t
        self.st = t.Station()
        self.body = dict(t.BODY)
        self.main = self.body["slug"]
        r = self.st.r
        with open(r.VERSION_FILE) as fh:
            self.release = fh.read().strip()
        self._paired(None)

    def tearDown(self):
        self.st.close()

    def _status(self, release):
        with open(self.st.r.UPDATES_FILE, "w") as fh:
            json.dump({self.main: {"release": release, "instr": "0123456789abcdef", "helper": "-",
                                   "computer": "fedcba9876543210", "at": 1}}, fh)

    def _paired(self, release):
        with open(self.st.r.PAIRED_FILE, "w") as fh:
            json.dump({self.main: release} if release else {}, fh)

    async def _refused(self):
        status, body = await self.st.send(dict(self.body))
        self.assertEqual((status, body.get("error")), (409, "Update the main bot first."))
        self.assertEqual(self.st.calls, [])
        self.assertEqual(self.st.pendings(), [])

    async def _posts(self):
        status, _ = await self.st.send(dict(self.body))
        self.assertEqual(status, 202)
        self.assertEqual(len(self.st.calls), 1)

    async def test_main_on_older_release_refused_before_any_post(self):
        self._status(_prev(self.release))
        self._paired(self.release)  # a signed status outranks the pairing stamp
        await self._refused()

    async def test_main_on_this_release_posts(self):
        self._status(self.release)
        await self._posts()

    async def test_no_status_paired_on_this_release_posts(self):
        self._paired(self.release)
        await self._posts()

    async def test_no_status_paired_on_older_release_refused(self):
        self._paired(_prev(self.release))
        await self._refused()

    async def test_no_status_and_no_pairing_stamp_refused(self):
        await self._refused()

    async def test_no_status_and_unreadable_pairing_stamps_refused(self):
        with open(self.st.r.PAIRED_FILE, "w") as fh:
            fh.write("{not json")
        await self._refused()

    async def test_unreadable_updates_file_refused_even_with_stamp(self):
        self._paired(self.release)
        with open(self.st.r.UPDATES_FILE, "w") as fh:
            fh.write("{not json")
        await self._refused()

    async def test_updates_file_missing_and_no_stamp_refused(self):
        self.assertFalse(os.path.exists(self.st.r.UPDATES_FILE))
        await self._refused()

    async def test_no_station_release_refused(self):
        self._status(self.release)
        os.unlink(self.st.r.VERSION_FILE)
        await self._refused()


class PairingStamp(unittest.IsolatedAsyncioTestCase):
    """Pinning a key stamps the current release; a signed status, a revoke, a delete or a re-pin replaces it."""

    def setUp(self):
        import test_gs1316_setup_relay as t
        from unittest import mock
        self.st = t.Station()
        self.main = t.BODY["slug"]
        self.keys = os.path.join(self.st.tmp, "keys.json")
        self.p = mock.patch.object(self.st.r, "bot_keys_path", lambda: self.keys)
        self.p.start()
        with open(self.st.r.PAIRED_FILE, "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        self.p.stop()
        self.st.close()

    async def _call(self, method, url, body):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        app = web.Application()
        app.add_routes(self.st.r.ADMIN_ROUTES)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.request(method, url, json=body,
                                        headers={"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "c" * 64})
            return resp.status
        finally:
            await client.close()

    async def test_key_pin_stamps_this_release_and_a_status_clears_it(self):
        r = self.st.r
        from nacl.signing import SigningKey
        pub = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()
        self.assertEqual(await self._call("PUT", "/admin/bots/%s/key" % self.main,
                                          {"actor": "admin", "public_key": pub}), 200)
        with open(r.VERSION_FILE) as fh:
            rel = fh.read().strip()
        self.assertEqual(r.paired_releases(), {self.main: rel})
        self.assertTrue(r.main_release_ok(self.main, rel))
        self.assertFalse(r.main_release_ok(self.main, _prev(rel)))
        self.assertTrue(r.note_update(self.main, "%s instr=0123456789abcdef helper=- computer=fedcba9876543210"
                                      % _prev(rel)))
        self.assertEqual(r.paired_releases(), {})
        self.assertFalse(r.main_release_ok(self.main, rel))

    async def test_key_revoke_drops_the_stamp(self):
        r = self.st.r
        with open(r.PAIRED_FILE, "w") as fh:
            json.dump({self.main: "0.13.18"}, fh)
        self.assertEqual(await self._call("DELETE", "/admin/bots/%s/key" % self.main, {"actor": "admin"}), 200)
        self.assertEqual(r.paired_releases(), {})

    async def test_revoke_all_drops_every_stamp(self):
        r = self.st.r
        with open(r.PAIRED_FILE, "w") as fh:
            json.dump({self.main: "0.13.18", "otto": "0.13.18"}, fh)
        self.assertEqual(await self._call("POST", "/admin/bots/revoke-all", {"actor": "admin"}), 200)
        self.assertEqual(r.paired_releases(), {})

    def test_forget_bot_status_drops_the_stamp(self):
        r = self.st.r
        with open(r.PAIRED_FILE, "w") as fh:
            json.dump({self.main: "0.13.18", "otto": "0.13.18"}, fh)
        r.forget_bot_status(self.main)
        self.assertEqual(r.paired_releases(), {"otto": "0.13.18"})


class AdminMapsRelayRefusal(unittest.TestCase):
    def test_admin_maps_the_relay_refusal_to_409(self):
        self.assertEqual(ga.setup_browser_failure("Juno", (409, {"error": "Update the main bot first."})),
                         (409, "Update the main bot first."))


if __name__ == "__main__":
    unittest.main()
