"""Release manifests: HTTPS fetch, planning, and staged apply. No signing."""

import inspect, json, os, re, ssl, stat, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import release


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
_AUDIT_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \S+ \S+ \S+ .+$")


def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_doc():
    return {
        "version": "v1.2.0",
        "min_version_floor": "v1.0.0",
        "issued_at": iso(NOW),
        "expires": iso(NOW + timedelta(days=30)),
        "components": {
            "station": {"tarball": "station-v1.2.0.tar.gz", "sha256": "ab" * 32},
            "relay": {"images": {
                "relay": "sha256:" + "cd" * 32,
                "caddy": "sha256:" + "ef" * 32,
            }},
        },
        "compat": {"station_min_relay": "v1.1.0", "relay_min_station": "v1.0.0"},
    }


def dumps(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Ops:
    def __init__(self, installed_relay="v1.0.0", installed_station="v1.0.0"):
        self.installed = {"relay": installed_relay, "station": installed_station}
        self.initial = {"relay": "r0", "station": "s0"}
        self.running = dict(self.initial)
        self.payload = {"r0": b"good-relay", "s0": b"good-station"}
        self.t = NOW
        self.busy_until = NOW
        self.disk = 5000
        self.bad_digest = set()
        self.bad_health = set()
        self.health_mode = "gate"
        self.stage_calls = []
        self.switch_calls = []
        self.rollback_calls = []
        self.discarded = []
        self.retained = []
        self.marked = []
        self.slept = []
        self.pauses = 0
        self.resumes = 0
        self.n = 0

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += timedelta(seconds=float(seconds))

    def is_busy(self):
        return self.t < self.busy_until

    def disk_free_mb(self):
        return self.disk

    def current_slot(self, component):
        return self.initial[component]

    def stage(self, component, spec):
        self.stage_calls.append(component)
        self.n += 1
        slot = f"{component}-{self.n}"
        self.payload[slot] = b"staged"
        if component in self.bad_digest:
            if component == "station":
                return {"slot": slot, "digest": "00" * 32}
            return {"slot": slot, "digests": {name: "sha256:" + "00" * 32 for name in spec["images"]}}
        if component == "station":
            return {"slot": slot, "digest": spec["sha256"]}
        return {"slot": slot, "digests": dict(spec["images"])}

    def switch(self, component, slot):
        self.switch_calls.append((component, slot))
        self.running[component] = slot

    def health(self, component):
        failed = component in self.bad_health
        if self.health_mode == "gates" and failed:
            return [{"gate": "disk", "ok": True}, {"gate": "boot", "ok": False}]
        if self.health_mode == "bool":
            return not failed
        if failed:
            return {"ok": False, "gate": "boot"}
        return {"ok": True, "gate": "boot"}

    def rollback(self, component):
        self.rollback_calls.append(component)
        self.running[component] = self.initial[component]
        return self.installed[component]

    def discard(self, component, slot):
        self.discarded.append((component, slot))

    def retain(self, component, slots):
        if len(slots) > 2:
            raise AssertionError(slots)
        self.retained.append((component, list(slots)))

    def mark_good(self, component, slot):
        self.marked.append((component, slot))

    def pause_voice(self):
        self.pauses += 1

    def resume_voice(self):
        self.resumes += 1


class ManifestTests(unittest.TestCase):
    def test_strict_version_and_digest(self):
        mutations = []
        doc = manifest_doc(); doc["version"] = "1.2.0"; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["station"]["sha256"] = "AB" * 32; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["relay"]["images"]["relay"] = "cd" * 32; mutations.append(doc)
        doc = manifest_doc(); doc["note"] = "extra"; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["station"]["tarball"] = "../station.tar.gz"; mutations.append(doc)
        doc = manifest_doc(); doc["expires"] = iso(NOW + timedelta(days=30, seconds=1)); mutations.append(doc)
        for doc in mutations:
            with self.assertRaises(release.ManifestError) as ctx:
                release.parse_manifest(doc, now=NOW)
            self.assertEqual(ctx.exception.reason, "bad-format", doc.get("version"))
        raw = dumps(manifest_doc())
        raw = raw[:-1] + b",}"
        with self.assertRaises(release.ManifestError) as ctx:
            release.parse_manifest(raw, now=NOW)
        self.assertEqual(ctx.exception.reason, "bad-format")

    def test_validity_window_and_skew_boundaries(self):
        self.assertEqual(release.parse_manifest(manifest_doc(), now=NOW)["version"], "v1.2.0")
        self.assertEqual(release.load_trusted_manifest(dumps(manifest_doc()), now=NOW)["version"], "v1.2.0")
        # issued_at == now + 5 min is allowed; one second later is future.
        for extra, reason in ((timedelta(minutes=5), None), (timedelta(minutes=5, seconds=1), "future")):
            issued = NOW + extra
            doc = manifest_doc()
            doc["issued_at"] = iso(issued)
            doc["expires"] = iso(issued + timedelta(days=1))
            if reason is None:
                release.parse_manifest(doc, now=NOW)
            else:
                with self.assertRaises(release.ManifestError) as ctx:
                    release.parse_manifest(doc, now=NOW)
                self.assertEqual(ctx.exception.reason, reason)
                self.assertIsNone(release.present_check_error(ctx.exception))
        # now == expires is allowed; one second later is expired.
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=1))
        doc["expires"] = iso(NOW)
        release.parse_manifest(doc, now=NOW)
        with self.assertRaises(release.ManifestError) as ctx:
            release.parse_manifest(doc, now=NOW + timedelta(seconds=1))
        self.assertEqual(ctx.exception.reason, "expired")

    def test_c20_expired_notice_and_audit(self):
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=10))
        doc["expires"] = iso(NOW - timedelta(seconds=1))
        raw = dumps(doc)
        result = release.check_update("v1.0.0", raw, now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "expired")
        self.assertEqual(result.audit, "update-check-expired")
        self.assertEqual(result.display, (
            "Can't check", "Update info is out of date. Updates may be hidden."))
        self.assertEqual(result.title, "Can't check")
        self.assertEqual(result.message, "Update info is out of date. Updates may be hidden.")
        with self.assertRaises(release.ManifestError) as ctx:
            release.load_trusted_manifest(raw, now=NOW)
        self.assertEqual(ctx.exception.reason, "expired")
        notice = release.present_check_error(ctx.exception)
        self.assertEqual(notice["audit"], "update-check-expired")
        self.assertEqual(notice["title"], "Can't check")
        # A dict cached earlier is stale once `now` passes expires.
        cached = release.load_trusted_manifest(raw, now=NOW - timedelta(days=1))
        stale = release.check_update("v1.0.0", cached, now=NOW)
        self.assertEqual(stale.audit, "update-check-expired")

    def test_future_is_not_reported_as_expired(self):
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW + timedelta(minutes=5, seconds=1))
        doc["expires"] = iso(NOW + timedelta(days=1))
        with self.assertRaises(release.ManifestError) as ctx:
            release.check_update("v1.0.0", doc, now=NOW)
        self.assertEqual(ctx.exception.reason, "future")
        self.assertIsNone(release.present_check_error(ctx.exception))

    def test_check_update_reports_a_newer_release(self):
        raw = dumps(manifest_doc())
        result = release.check_update("v1.0.0", raw, now=NOW)
        self.assertTrue(result.ok)
        self.assertTrue(result.update)
        self.assertEqual(result.version, "v1.2.0")
        self.assertIsNone(result.audit)
        self.assertEqual(release.parse_manifest(raw, now=NOW)["version"], "v1.2.0")
        doc = manifest_doc()
        doc["version"] = "v1.10.0"
        numeric = release.check_update("v1.9.0", doc, now=NOW)
        self.assertTrue(numeric.update)
        same = release.check_update("v1.2.0", manifest_doc(), now=NOW)
        self.assertFalse(same.update)
        self.assertEqual(same.reason, "current")


class FetchTests(unittest.TestCase):
    def _fetch(self, body=b"{}"):
        seen = {}

        def fetcher(url, context):
            seen["url"] = url
            seen["context"] = context
            seen["calls"] = seen.get("calls", 0) + 1
            if isinstance(body, Exception):
                raise body
            return body

        return fetcher, seen

    def test_default_url_and_tls_verification(self):
        self.assertEqual(
            release.MANIFEST_URL,
            "https://github.com/bwilliam79/grok-station/releases/latest/download/manifest.json",
        )
        fetcher, seen = self._fetch(b"manifest-body")
        got = release.fetch_manifest(fetcher)
        self.assertEqual(got, b"manifest-body")
        self.assertEqual(seen["calls"], 1)
        self.assertEqual(seen["url"], release.MANIFEST_URL)
        context = seen["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        src = inspect.getsource(release)
        self.assertNotIn("CERT_NONE", src)
        self.assertNotIn("check_hostname = False", src)
        self.assertNotIn("verify=False", src)
        for name in ("Minisign", "minisign", "TrustStore", "verify_signature", "parse_public_key"):
            self.assertNotIn(name, src)
        with self.assertRaises(ValueError):
            release.ManifestError("unsigned")

    def test_allowed_hosts_are_fetched(self):
        allowed = (
            "https://github.com/bwilliam79/grok-station/releases/download/v1.2.0/manifest.json",
            "https://GitHub.com/bwilliam79/grok-station/releases/latest/download/manifest.json",
            "https://github.com:443/bwilliam79/grok-station/releases/latest/download/manifest.json",
            "https://objects.githubusercontent.com/example/manifest.json",
            "https://objects.githubusercontent.com/example/manifest.json?name=manifest.json",
        )
        for url in allowed:
            fetcher, seen = self._fetch(b"ok")
            self.assertEqual(release.fetch_manifest(fetcher, url), b"ok", url)
            self.assertEqual(seen["url"], url)
            self.assertEqual(seen["context"].verify_mode, ssl.CERT_REQUIRED)

    def test_http_wrong_host_and_tricks_are_rejected(self):
        rejected = (
            "http://github.com/bwilliam79/grok-station/releases/latest/download/manifest.json",
            "https://example.invalid/manifest.json",
            "https://github.com.example.invalid/manifest.json",
            "https://evil.github.com/manifest.json",
            "https://api.github.com/repos/bwilliam79/grok-station/releases/latest",
            "https://objects.githubusercontent.com.example.invalid/manifest.json",
            "https://githubusercontent.com/manifest.json",
            "https://user@github.com/bwilliam79/grok-station/releases/latest/download/manifest.json",
            "https://user:secret@github.com/manifest.json",
            "https://github.com:4443/manifest.json",
            "https://192.0.2.1/manifest.json",
            "https://github.com/manifest.json#frag",
            "ftp://github.com/manifest.json",
            "https://github.com./manifest.json",
            "https://github.com%2eexample.invalid/manifest.json",
            "https://github.com\\@evil.example/manifest.json",
            " https://github.com/manifest.json",
            "https://github.com/manifest.json\n",
            "",
        )
        for url in rejected:
            called = []

            def fetcher(target, _context, called=called):
                called.append(target)
                return b"no"

            with self.assertRaises(release.ManifestError) as ctx:
                release.fetch_manifest(fetcher, url)
            self.assertEqual(called, [], url)
            self.assertEqual(ctx.exception.reason, "fetch-failed", url)
            self.assertNotIn("example.invalid", str(ctx.exception))
            self.assertNotIn("192.0.2.1", str(ctx.exception))
            self.assertNotIn("secret", str(ctx.exception))

    def test_size_cap(self):
        cap = release.MAX_MANIFEST_BYTES
        fetcher, _seen = self._fetch(b"x" * cap)
        self.assertEqual(len(release.fetch_manifest(fetcher)), cap)
        over, _seen = self._fetch(b"x" * (cap + 1))
        with self.assertRaises(release.ManifestError) as ctx:
            release.fetch_manifest(over)
        self.assertEqual(ctx.exception.reason, "fetch-failed")
        self.assertEqual(ctx.exception.detail, "size")
        text, _seen = self._fetch("y" * (cap + 1))
        with self.assertRaises(release.ManifestError) as ctx:
            release.fetch_manifest(text)
        self.assertEqual(ctx.exception.detail, "size")

    def test_transport_and_body_failures(self):
        down, _seen = self._fetch(OSError("down"))
        with self.assertRaises(release.ManifestError) as ctx:
            release.fetch_manifest(down)
        self.assertEqual(ctx.exception.reason, "fetch-failed")
        self.assertEqual(ctx.exception.detail, "transport")
        self.assertIsNone(ctx.exception.__cause__)
        empty, _seen = self._fetch(None)
        with self.assertRaises(release.ManifestError) as ctx:
            release.fetch_manifest(empty)
        self.assertEqual(ctx.exception.detail, "body")
        with self.assertRaises(release.ManifestError) as ctx:
            release.fetch_manifest(None)
        self.assertEqual(ctx.exception.detail, "fetcher")

    def test_fetched_manifest_parses_without_a_signature(self):
        body = dumps(manifest_doc())
        fetcher, seen = self._fetch(body)
        parsed = release.parse_manifest(release.fetch_manifest(fetcher), now=NOW)
        self.assertEqual(parsed["version"], "v1.2.0")
        self.assertEqual(parsed["components"]["station"]["sha256"], "ab" * 32)
        self.assertTrue(seen["context"].check_hostname)


class RequestTests(unittest.TestCase):
    def test_c8_reject_same_or_older_version(self):
        current = manifest_doc()
        same = release.validate_update_request('{"version":"v1.2.0"}', "v1.2.0", current)
        self.assertEqual(same, "update-rejected downgrade")
        self.assertEqual(same.reason, "downgrade")
        older = manifest_doc()
        older["version"] = "v1.1.0"
        decision = release.validate_update_request({"version": "v1.1.0"}, "v1.4.0", older)
        self.assertEqual(decision.audit, "update-rejected downgrade")
        self.assertFalse(decision.ok)

    def test_c9_reject_below_floor_allow_floor_itself(self):
        doc = manifest_doc()
        doc["version"] = "v1.5.0"
        doc["min_version_floor"] = "v2.0.0"
        below = release.validate_update_request({"version": "v1.5.0"}, "v1.0.0", doc)
        self.assertEqual(below.reason, "downgrade")
        on_floor = manifest_doc()
        on_floor["version"] = "v1.0.0"
        on_floor["min_version_floor"] = "v1.0.0"
        allowed = release.validate_update_request({"version": "v1.0.0"}, "v0.9.0", on_floor)
        self.assertTrue(allowed.ok)
        self.assertIsNone(allowed.audit)

    def test_not_in_manifest_and_bad_args(self):
        doc = manifest_doc()
        other = release.validate_update_request({"version": "v9.9.9"}, "v1.0.0", doc)
        self.assertEqual(other.reason, "not-in-manifest")
        self.assertEqual(other.audit, "update-rejected not-in-manifest")
        bodies = [
            {"version": "v1.2.0", "url": "http://example.invalid/pkg"},
            {"version": "v1.2.0", "path": "../pkg.tar"},
            {"version": "v1.2.0", "cmd": "echo hi"},
            '{"version":"v1.2.0;id"}',
            '{"version":"v1.2.0","url":"http://example.invalid/pkg"}',
            "v1.2.0",
            ["v1.2.0"],
            {"Version": "v1.2.0"},
            "",
            "{",
        ]
        for body in bodies:
            decision = release.validate_update_request(body, "v1.0.0", doc)
            self.assertEqual(decision.reason, "bad-args", body)
            self.assertEqual(decision.audit, "update-rejected bad-args")
        good = release.validate_update_request(b'{"version":"v1.2.0"}', "v1.0.0", doc)
        self.assertTrue(good.ok)
        self.assertIsNone(good.audit)
        self.assertEqual(good.version, "v1.2.0")
        spaced = release.validate_update_request('{ "version" : "v1.2.0" }\n', "v1.0.0", doc)
        self.assertTrue(spaced.ok)


class PlanTests(unittest.TestCase):
    def test_soak_when_new_relay_still_runs_on_this_station(self):
        plan = release.plan_update("v1.0.0", "v1.1.0", manifest_doc())
        self.assertEqual(plan, ["relay", "soak:600", "station"])
        self.assertEqual(plan.steps, ["relay", "soak:600", "station"])
        self.assertTrue(plan.station_only_allowed)
        not_yet = release.plan_update("v1.0.0", "v1.0.0", manifest_doc())
        self.assertEqual(list(not_yet), ["relay", "soak:600", "station"])
        self.assertFalse(not_yet.station_only_allowed)

    def test_pause_voice_when_relay_is_not_compatible(self):
        doc = manifest_doc()
        doc["compat"]["relay_min_station"] = "v2.0.0"
        doc["compat"]["station_min_relay"] = "v1.0.0"
        plan = release.plan_update("v1.0.0", "v9.0.0", doc)
        self.assertEqual(list(plan), ["pause-voice", "relay", "station", "resume-voice"])
        self.assertFalse(plan.station_only_allowed)

    def test_version_compare_is_numeric(self):
        doc = manifest_doc()
        doc["compat"]["relay_min_station"] = "v1.10.0"
        older = release.plan_update("v1.9.0", "v1.10.0", doc)
        self.assertEqual(list(older), ["pause-voice", "relay", "station", "resume-voice"])
        doc["compat"]["relay_min_station"] = "v1.9.0"
        doc["compat"]["station_min_relay"] = "v1.9.0"
        newer = release.plan_update("v1.10.0", "v1.10.0", doc)
        self.assertEqual(list(newer), ["relay", "soak:600", "station"])
        self.assertTrue(newer.station_only_allowed)


class ApplyTests(unittest.TestCase):
    def _apply(self, ops, doc=None, station="v1.0.0", relay="v1.0.0", steps=None):
        doc = manifest_doc() if doc is None else doc
        return release.Updater(ops).apply(station, relay, doc, steps, now=NOW), doc

    def test_success_path_audits_and_soaks(self):
        ops = Ops()
        result, _doc = self._apply(ops)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "applied")
        self.assertEqual(ops.stage_calls, ["relay", "station"])
        self.assertEqual(ops.slept, [600])
        self.assertEqual(result.events, [
            "update-start v1.0.0 v1.2.0",
            "update-stage-ok",
            "update-switch",
            "update-health-ok boot",
            "update-start v1.0.0 v1.2.0",
            "update-stage-ok",
            "update-switch",
            "update-health-ok boot",
        ])
        self.assertIn(" relay update-start v1.0.0 v1.2.0", result.lines[0])
        self.assertIn(" station update-start v1.0.0 v1.2.0", result.lines[4])
        self.assertTrue(result.lines[0].startswith("2026-10-20T12:00:00Z release relay "))
        self.assertTrue(result.lines[4].startswith("2026-10-20T12:10:00Z release station "))
        blob = "\n".join(result.lines)
        self.assertNotIn("ab" * 32, blob)
        self.assertNotIn("cd" * 32, blob)
        for line in result.lines:
            self.assertRegex(line, _AUDIT_LINE)
        self.assertEqual(result.kept["relay"], ["relay-1", "r0"])
        self.assertEqual(result.kept["station"], ["station-2", "s0"])

    def test_c11_digest_mismatch_leaves_rollback_slot_untouched(self):
        ops = Ops()
        ops.bad_digest.add("relay")
        result, _doc = self._apply(ops)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.reason, "digest")
        self.assertEqual(ops.stage_calls, ["relay"])
        self.assertEqual(ops.switch_calls, [])
        self.assertEqual(ops.rollback_calls, [])
        self.assertEqual(ops.running["relay"], "r0")
        self.assertEqual(ops.payload["r0"], b"good-relay")
        self.assertEqual(result.current["relay"], "r0")
        self.assertNotIn("relay", result.kept)
        self.assertIn("update-start v1.0.0 v1.2.0", result.events)
        self.assertIn("update-aborted digest", result.events)
        self.assertNotIn("update-stage-ok", result.events)
        self.assertFalse(any(event.startswith("update-rolled-back") for event in result.events))
        # Station digest mismatch after a good relay does not roll the relay back
        # when the new relay still runs on the installed station.
        ops = Ops()
        ops.bad_digest.add("station")
        result, _doc = self._apply(ops)
        self.assertEqual(result.reason, "digest")
        self.assertEqual(ops.rollback_calls, [])
        self.assertEqual(ops.stage_calls, ["relay", "station"])
        self.assertEqual(ops.payload["s0"], b"good-station")
        self.assertEqual(len(ops.switch_calls), 1)
        self.assertEqual(ops.switch_calls[0][0], "relay")

    def test_c12_relay_failure_stops_station(self):
        ops = Ops()
        ops.bad_health.add("relay")
        result, _doc = self._apply(ops)
        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(result.reason, "health")
        self.assertEqual(ops.stage_calls, ["relay"])
        self.assertEqual(ops.rollback_calls, ["relay"])
        self.assertEqual(ops.running["relay"], "r0")
        self.assertEqual(sum(1 for event in result.events if event.startswith("update-start")), 1)
        self.assertIn("update-health-fail boot", result.events)
        self.assertIn("update-rolled-back v1.0.0", result.events)
        self.assertNotIn("update-health-ok boot", result.events)

    def test_c13_keeps_exactly_one_previous_slot(self):
        ops = Ops()
        updater = release.Updater(ops)
        doc = manifest_doc()
        last = None
        for _ in range(3):
            last = updater.apply("v1.0.0", "v1.0.0", doc, now=NOW)
            self.assertTrue(last.ok)
        self.assertEqual(last.kept["relay"], ["relay-5", "relay-3"])
        self.assertEqual(last.kept["station"], ["station-6", "station-4"])
        self.assertEqual(len(last.kept["relay"]), 2)
        self.assertEqual(len(last.kept["station"]), 2)
        self.assertNotIn("r0", last.kept["relay"])
        self.assertNotIn("s0", last.kept["station"])
        self.assertIn(("relay", "r0"), ops.discarded)
        self.assertIn(("station", "s0"), ops.discarded)
        self.assertIn(("relay", "relay-1"), ops.discarded)
        for _component, slots in ops.retained:
            self.assertLessEqual(len(slots), 2)
        self.assertEqual(ops.retained[-2], ("relay", ["relay-5", "relay-3"]))
        self.assertEqual(ops.retained[-1], ("station", ["station-6", "station-4"]))

    def test_health_failure_rolls_back_and_stops_on_first_bad_gate(self):
        ops = Ops()
        ops.health_mode = "bool"
        ops.bad_health.add("relay")
        result, _doc = self._apply(ops)
        self.assertIn("update-health-fail ready", result.events)
        self.assertIn("update-rolled-back v1.0.0", result.events)
        self.assertEqual(ops.rollback_calls, ["relay"])
        ops = Ops()
        ops.health_mode = "gates"
        ops.bad_health.add("relay")
        result, _doc = self._apply(ops)
        self.assertIn("update-health-ok disk", result.events)
        self.assertIn("update-health-fail boot", result.events)
        self.assertLess(
            result.events.index("update-health-ok disk"),
            result.events.index("update-health-fail boot"))
        self.assertEqual(ops.stage_calls, ["relay"])

    def test_coupled_station_failure_rolls_relay_back_and_resumes_voice(self):
        doc = manifest_doc()
        doc["compat"]["relay_min_station"] = "v2.0.0"
        ops = Ops()
        ops.bad_health.add("station")
        result, _doc = self._apply(ops, doc)
        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(ops.pauses, 1)
        self.assertEqual(ops.resumes, 1)
        self.assertEqual(ops.stage_calls, ["relay", "station"])
        self.assertEqual(ops.rollback_calls, ["station", "relay"])
        self.assertEqual(ops.running["relay"], "r0")
        self.assertEqual(ops.running["station"], "s0")
        self.assertEqual(ops.slept, [])

    def test_busy_defers_after_ten_minutes_and_disk_abort(self):
        ops = Ops()
        ops.busy_until = NOW + timedelta(seconds=601)
        ops.disk = 100
        result, _doc = self._apply(ops)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.reason, "busy")
        self.assertEqual(result.events, ["update-deferred busy"])
        self.assertEqual(ops.stage_calls, [])
        self.assertGreaterEqual((ops.t - NOW).total_seconds(), 600)
        self.assertLess((ops.t - NOW).total_seconds(), 601)
        ops = Ops()
        ops.busy_until = NOW + timedelta(seconds=120)
        result, _doc = self._apply(ops)
        self.assertTrue(result.ok)
        self.assertGreaterEqual(ops.slept[0], 1)
        self.assertEqual(ops.stage_calls[0], "relay")
        ops = Ops()
        ops.disk = 499
        result, _doc = self._apply(ops)
        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.reason, "disk")
        self.assertEqual(result.events, ["update-aborted disk"])
        self.assertEqual(ops.stage_calls, [])
        ops = Ops()
        ops.disk = 500
        result, _doc = self._apply(ops)
        self.assertTrue(result.ok)
        self.assertEqual(ops.stage_calls, ["relay", "station"])

    def test_station_only_when_relay_is_already_new_enough(self):
        ops = Ops()
        result, doc = self._apply(ops, station="v1.0.0", relay="v1.1.0", steps=["station"])
        self.assertTrue(result.ok)
        self.assertEqual(ops.stage_calls, ["station"])
        self.assertEqual(ops.slept, [])
        ops = Ops()
        result, _doc = self._apply(ops, doc, steps=["station"])
        self.assertEqual(result.reason, "station-only")
        self.assertEqual(ops.stage_calls, [])
        self.assertIn("update-aborted station-only", result.events)

    def test_expired_manifest_is_not_applied(self):
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=2))
        doc["expires"] = iso(NOW - timedelta(seconds=1))
        ops = Ops()
        with self.assertRaises(release.ManifestError) as ctx:
            release.Updater(ops).apply("v1.0.0", "v1.0.0", doc, now=NOW)
        self.assertEqual(ctx.exception.reason, "expired")
        self.assertEqual(ops.stage_calls, [])


class SettingsTests(unittest.TestCase):
    def test_autocheck_default_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            self.assertEqual(release.load_settings(path), {"autocheck": False})
            self.assertFalse(os.path.exists(path))
            self.assertEqual(release.set_autocheck(path, True), "update-autocheck on")
            self.assertEqual(release.load_settings(path), {"autocheck": True})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(release.set_autocheck(path, False), "update-autocheck off")
            self.assertEqual(release.load_settings(path), {"autocheck": False})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"autocheck": False, "theme": "warm"}, handle)
            release.set_autocheck(path, True)
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertIs(stored["autocheck"], True)
            self.assertEqual(stored["theme"], "warm")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"autocheck": "yes"}, handle)
            with self.assertRaises(ValueError):
                release.load_settings(path)

    def test_check_now_ignores_the_toggle(self):
        doc = manifest_doc()
        off = {"autocheck": False}
        skipped = release.scheduled_check(off, "v1.0.0", b"not-json", now=NOW)
        self.assertTrue(skipped.skipped)
        self.assertFalse(skipped.update)
        self.assertEqual(skipped.reason, "autocheck-off")
        manual = release.check_now("v1.0.0", doc, settings=off, now=NOW)
        self.assertFalse(manual.skipped)
        self.assertTrue(manual.update)
        scheduled = release.scheduled_check({"autocheck": True}, "v1.0.0", doc, now=NOW)
        self.assertFalse(scheduled.skipped)
        self.assertTrue(scheduled.update)

    def test_audit_line_redacts_key_material(self):
        line = release.format_audit("2026-10-20T12:00:00Z", "release", "relay", "update-stage-ok", "ab" * 32)
        self.assertNotIn("ab" * 32, line)
        self.assertTrue(line.endswith(" abab"))
        key_line = release.format_audit(
            "2026-10-20T12:00:00Z", "release", "-", "release-key-rotated", "a000000000000001")
        self.assertNotIn("a000000000000001", key_line)
        self.assertTrue(key_line.endswith(" 0001"))
        with self.assertRaises(ValueError):
            release.format_audit("2026-10-20T12:00:00Z", "release", "-", "update-stage-ok", "192.0.2.1")

    def test_module_does_not_touch_the_network(self):
        src = inspect.getsource(release)
        for banned in ("import socket", "import urllib", "import requests", "import http"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
