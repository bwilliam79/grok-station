"""0.13.18 email summaries and pinned Needs you."""
import os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import cards

INSTRUCTION = "When you email {owner}, also post a short version here: your email’s name as the kind, then its lead sentence (60 characters max). When you need {owner} to decide or do something, post it as Needs you and clear it once it’s handled. Don’t post progress or “working on” updates."

class EmailUpdates(unittest.TestCase):
    def test_template_is_required_not_optional(self):
        for mode in ("cloud", "home"):
            text = (ROOT / "admin" / ("routine-" + mode + ".txt")).read_text()
            self.assertIn(INSTRUCTION, text)
            self.assertNotIn("Station updates (optional)", text)

    def test_email_schema_limits_and_meta(self):
        import test_gs1310_updates_feed as old
        doc = old.upd(type_="update", line="A" * 60, kind="Release", detail="D" * 90)
        got = cards.validate_update(doc)
        self.assertEqual(got["update"]["kind"], "Release")
        for field, value in (("kind", "K" * 19), ("line", "L" * 61), ("detail", "D" * 91)):
            bad = dict(doc, update=dict(doc["update"], **{field: value}))
            with self.assertRaises(cards.CardError): cards.validate_update(bad)
        import grok_station as gs
        self.assertEqual(gs.update_meta({"bot": "otto", "type": "update", "kind": "Release"}), "Otto · Release")
        self.assertEqual(gs.update_meta({"bot": "otto", "type": "needs_you"}), "⚠ Needs you · Otto")

    def test_browser_copy_substitutes_owner(self):
        import json, subprocess
        js = (ROOT / "admin/web/app.js").read_text()
        def fn(name):
            start = js.index("function " + name + "(")
            return js[start:js.index("\nfunction ", start + 1)]
        templates = "\n".join("const " + name + " = " + json.dumps(js.split("const " + name + " = String.raw`", 1)[1].split("`;", 1)[0]) + ";" for name in ("BOT_ROUTINE", "BOT_ROUTINE_HOME", "SETUP_PARAMS"))
        for owner in ("Sample Owner", "", "bad'name"):
            for name in ("routineForBot", "routineUpdate"):
                prog = templates + "\nfunction stationAddress(){return 'https://station.example';}\nfunction deployScript(){return 'SCRIPT';}\nfunction fillUpdateCommand(b){return b;}\n" + fn(name)
                prog += "\nprocess.stdout.write(" + name + "('otto','','','home'," + json.dumps(owner) + "));"
                result = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("{owner}", result.stdout)
                self.assertIn(INSTRUCTION.format(owner="Sample Owner" if owner == "Sample Owner" else "your owner"), result.stdout)

    def test_owner_substitution_matches_exact_policy(self):
        sys.path.insert(0, str(ROOT / "admin"))
        import gs_routine
        for owner in ("Sample Owner", None):
            body = gs_routine.render(gs_routine.TEMPLATES["home"], "home", "otto", "a" * 40, "b" * 64, owner=owner)
            self.assertIn(INSTRUCTION.format(owner=owner or "your owner"), body)
            self.assertNotIn("{owner}", body)
            self.assertEqual(gs_routine.check(body, "home"), "0.13.18")

    def test_browser_copy_owner_matches_server(self):
        import json, re, subprocess, tempfile
        sys.path.insert(0, str(ROOT / "admin"))
        import gs_routine
        js = (ROOT / "admin/web/app.js").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"(?m)^function stationOwnerName\(", js), "browser Copy owner substitution missing")
        self.assertIn("STATION_OWNER.v =", js)
        self.assertIn("data.setup.owner", js)
        def tmpl(name):
            return js.split("const %s = String.raw`" % name, 1)[1].split("`;", 1)[0]
        def extract(name):
            m = re.search(r"\n(?:async )?function " + name + r"\(", js)
            start = m.start() + 1
            end = js.index("\n}", start) + 2
            return js[start:end] + "\n"
        fp = ":".join(["AB"] * 32)
        pub = "A" * 43 + "="
        station = "https://station-demo.local"
        pins = {"commit": "a" * 40, "files": {"scripts/gs-update.sh": "b" * 64}}
        for value in ("Sample Owner", "", "bad'name", None):
            for mode in ("home", "cloud"):
                expected = gs_routine.owner_name(value)
                prog = (
                    "const BOT_ROUTINE = " + json.dumps(tmpl("BOT_ROUTINE")) + ";\n"
                    "const BOT_ROUTINE_HOME = " + json.dumps(tmpl("BOT_ROUTINE_HOME")) + ";\n"
                    "const SETUP_PARAMS = " + json.dumps(tmpl("SETUP_PARAMS")) + ";\n"
                    "const STATION_OWNER = {v: " + json.dumps(value) + "};\n"
                    "const DEPLOY_PINS = " + json.dumps(pins) + ";\n"
                    "function stationAddress(){ return " + json.dumps(station) + "; }\n"
                    "function deployScript(){ return 'SCRIPT'; }\n"
                    + extract("stationOwnerName")
                    + extract("stationUpdateCommand")
                    + extract("fillUpdateCommand")
                    + extract("routineUpdate")
                    + extract("routineForBot")
                    + "const out = [routineUpdate('otto'," + json.dumps(pub) + "," + json.dumps(fp) + "," + json.dumps(mode) + "),"
                    + "routineForBot('otto'," + json.dumps(pub) + "," + json.dumps(fp) + "," + json.dumps(mode) + ")];\n"
                    + "process.stdout.write(JSON.stringify(out));\n"
                )
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                    fh.write(prog)
                    path = fh.name
                try:
                    run = subprocess.run(["node", path], capture_output=True, text=True)
                finally:
                    os.unlink(path)
                self.assertEqual(run.returncode, 0, run.stderr)
                for copied in json.loads(run.stdout):
                    self.assertNotIn("{owner}", copied)
                    self.assertIn(INSTRUCTION.format(owner=expected), copied)
                    self.assertIn("--owner '" + expected + "'", copied)

    def test_kind_replacement_and_24_hour_expiry(self):
        f = cards.UpdateFeed()
        row = {"type": "update", "kind": "Release", "line": "Shipped", "detail": "Ready to test", "time": 1000}
        f.put("otto", "a", row)
        f.put("otto", "b", dict(row, time=1001))
        self.assertEqual(len(f.items), 1)
        f.expire(1001 + 86400)
        self.assertFalse(f.items)

    def test_one_needs_you_per_bot_until_cleared(self):
        f = cards.UpdateFeed()
        row = {"type": "needs_you", "line": "Choose a version", "time": 1000}
        f.put("otto", "a", row)
        f.put("otto", "b", dict(row, time=1001))
        f.expire(1001 + 86400 * 3)
        self.assertEqual(len(f.items), 1)
        self.assertTrue(f.clear("otto", "b"))



# Real HTTP transport: signed envelope -> relay -> post_station_show -> apply_update.
sys.path.insert(0, str(ROOT / "tests"))
import test_gs1310_updates_feed as previous
class SignedRoundTrip(previous.RelayGate):
    def test_real_signed_update_clear_and_rule(self):
        import http.server, threading, json
        from unittest.mock import patch
        import grok_station as gs
        feed = cards.UpdateFeed()
        force_error = [False]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    if force_error[0]: raise cards.CardError("schema")
                    gs.apply_update(doc, feed)
                    code = 200
                except cards.CardError:
                    code = 400
                self.send_response(code)
                self.end_headers()
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.relay.write_updates("jules", True)
        try:
            with patch.object(self.relay, "_station_env_values", return_value={"GROK_STATION_TOKEN": "test", "GROK_STATION_PORT": str(server.server_port)}):
                for doc in (previous.upd(feed_rule="Only releases", expires_at=previous.iso(previous.T0 + 3600)),
                            dict(previous.tc._meta(), v=1, op="update_clear", id="u1")):
                    raw = previous.tc._seal(self.station, self.signer, json.dumps(doc))
                    status, _, _ = self.relay.accept_show(raw, self.station, self.keys, self.known,
                        previous.botreply.Limiter(50, 60), self.relay.post_station_show, ttl_max=3600)
                    self.assertEqual(status, 200)
                self.assertEqual(self.relay.read_feed_rules()["jules"], "Only releases")
                self.assertFalse(feed.items)
                force_error[0] = True
                raw = previous.tc._seal(self.station, self.signer, json.dumps(previous.upd(uid="refused")))
                status, result, _ = self.relay.accept_show(raw, self.station, self.keys, self.known,
                    previous.botreply.Limiter(50, 60), self.relay.post_station_show, ttl_max=3600)
                self.assertEqual((status, result["error"]), (400, "schema"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
