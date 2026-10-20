"""Signed release manifests: minisign, trust, rotation, planning, staged apply."""

import base64, hashlib, inspect, json, os, re, stat, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import release


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
PRIMARY_ID = bytes.fromhex("a000000000000001")
BACKUP_ID = bytes.fromhex("b000000000000002")
_AUDIT_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \S+ \S+ \S+ .+$")


def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_bytes(sk):
    return sk.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)


def minisign_pub(sk, key_id, comment=True):
    raw = b"Ed" + key_id + public_bytes(sk)
    blob = base64.b64encode(raw).decode("ascii")
    if not comment:
        return blob
    return f"untrusted comment: minisign public key {key_id.hex()}\n{blob}\n"


def minisign_sig(sk, key_id, message, trusted="timestamp:1700000000", prehash=False):
    """Build a minisign file. Global signature is sig || trusted-comment line."""
    payload = hashlib.blake2b(message, digest_size=64).digest() if prehash else message
    sig = sk.sign(payload)
    line = "trusted comment: " + trusted
    global_sig = sk.sign(sig + line.encode("utf-8"))
    body = (b"ED" if prehash else b"Ed") + key_id + sig
    return (
        "untrusted comment: signature\n"
        + base64.b64encode(body).decode("ascii") + "\n"
        + line + "\n"
        + base64.b64encode(global_sig).decode("ascii") + "\n")


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


def signed(sk, key_id, doc, prehash=False):
    raw = dumps(doc)
    return raw, minisign_sig(sk, key_id, raw, prehash=prehash)


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


class Keyed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.primary_sk = Ed25519PrivateKey.generate()
        cls.backup_sk = Ed25519PrivateKey.generate()
        cls.primary_pub = minisign_pub(cls.primary_sk, PRIMARY_ID)
        cls.backup_pub = minisign_pub(cls.backup_sk, BACKUP_ID)

    def store(self, revoked=None, path=None):
        return release.TrustStore(self.primary_pub, self.backup_pub, revoked or [], path=path)


class MinisignTests(Keyed):
    def test_public_key_comment_is_optional(self):
        with_comment = release.parse_public_key(self.primary_pub)
        bare = release.parse_public_key(minisign_pub(self.primary_sk, PRIMARY_ID, comment=False))
        self.assertEqual(with_comment.key_id, PRIMARY_ID)
        self.assertEqual(with_comment.public, public_bytes(self.primary_sk))
        self.assertEqual(bare.key_id, with_comment.key_id)
        self.assertEqual(bare.public, with_comment.public)
        self.assertEqual(len(with_comment.public), 32)

    def test_ed25519_message_and_global_signature(self):
        message = b"release-manifest"
        sig = minisign_sig(self.primary_sk, PRIMARY_ID, message)
        parsed = release.parse_signature(sig)
        self.assertEqual(parsed.algorithm, b"Ed")
        self.assertEqual(parsed.key_id, PRIMARY_ID)
        self.assertEqual(len(parsed.sig), 64)
        self.assertTrue(release.verify_signature(self.primary_pub, message, sig))
        changed = sig.replace("untrusted comment: signature", "untrusted comment: note", 1)
        self.assertTrue(release.verify_signature(self.primary_pub, message, changed))

    def test_prehashed_blake2b_512(self):
        message = b"m" * 200
        self.assertEqual(len(hashlib.blake2b(message, digest_size=64).digest()), 64)
        sig = minisign_sig(self.primary_sk, PRIMARY_ID, message, prehash=True)
        self.assertEqual(release.parse_signature(sig).algorithm, b"ED")
        self.assertTrue(release.verify_signature(self.primary_pub, message, sig))
        # Labeling a raw signature as ED must fail: ED verifies the 64-byte hash.
        lines = minisign_sig(self.primary_sk, PRIMARY_ID, message, prehash=False).splitlines()
        blob = bytearray(base64.b64decode(lines[1]))
        self.assertEqual(bytes(blob[:2]), b"Ed")
        blob[:2] = b"ED"
        lines[1] = base64.b64encode(bytes(blob)).decode("ascii")
        forged = "\n".join(lines) + "\n"
        with self.assertRaises(release.ManifestError) as ctx:
            release.verify_signature(self.primary_pub, message, forged)
        self.assertEqual(ctx.exception.reason, "unsigned")

    def test_keyid_must_match_even_when_the_signature_bytes_verify(self):
        message = b"release-manifest"
        lines = minisign_sig(self.primary_sk, PRIMARY_ID, message).splitlines()
        blob = bytearray(base64.b64decode(lines[1]))
        blob[2:10] = b"\xff" * 8
        lines[1] = base64.b64encode(bytes(blob)).decode("ascii")
        moved = "\n".join(lines) + "\n"
        with self.assertRaises(release.ManifestError) as ctx:
            release.verify_signature(self.primary_pub, message, moved)
        self.assertEqual(ctx.exception.reason, "unsigned")

    def test_tampered_message_trusted_comment_or_global_signature_rejected(self):
        message = b"release-manifest"
        sig = minisign_sig(self.primary_sk, PRIMARY_ID, message)
        flipped = bytearray(message)
        flipped[0] ^= 1
        with self.assertRaises(release.ManifestError) as ctx:
            release.verify_signature(self.primary_pub, bytes(flipped), sig)
        self.assertEqual(ctx.exception.reason, "unsigned")
        trusted = sig.replace("timestamp:1700000000", "timestamp:1700000001", 1)
        with self.assertRaises(release.ManifestError):
            release.verify_signature(self.primary_pub, message, trusted)
        lines = sig.splitlines()
        glob = bytearray(base64.b64decode(lines[3]))
        glob[0] ^= 1
        lines[3] = base64.b64encode(bytes(glob)).decode("ascii")
        with self.assertRaises(release.ManifestError):
            release.verify_signature(self.primary_pub, message, "\n".join(lines) + "\n")
        other = Ed25519PrivateKey.generate()
        other_pub = minisign_pub(other, bytes.fromhex("c000000000000003"))
        with self.assertRaises(release.ManifestError):
            release.verify_signature(other_pub, message, sig)


class TrustTests(Keyed):
    def test_primary_or_backup_signature_accepted(self):
        raw, primary_sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        _raw_b, backup_sig = signed(self.backup_sk, BACKUP_ID, manifest_doc())
        store = self.store()
        self.assertEqual(store.verify_manifest(raw, primary_sig).key_id, PRIMARY_ID)
        self.assertEqual(store.verify_manifest(raw, backup_sig).key_id, BACKUP_ID)
        stranger = Ed25519PrivateKey.generate()
        _raw_s, stranger_sig = signed(stranger, bytes.fromhex("c000000000000003"), manifest_doc())
        with self.assertRaises(release.ManifestError) as ctx:
            store.verify_manifest(raw, stranger_sig)
        self.assertEqual(ctx.exception.reason, "unsigned")

    def test_revoked_keyid_rejected(self):
        raw, sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        store = self.store(revoked=[PRIMARY_ID.hex().upper()])
        with self.assertRaises(release.ManifestError) as ctx:
            store.verify_manifest(raw, sig)
        self.assertEqual(ctx.exception.reason, "unsigned")
        _raw_b, backup_sig = signed(self.backup_sk, BACKUP_ID, manifest_doc())
        self.assertEqual(store.verify_manifest(raw, backup_sig).key_id, BACKUP_ID)

    def test_prehashed_release_verifies(self):
        raw, sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc(), prehash=True)
        doc = release.load_trusted_manifest(raw, sig, self.store(), now=NOW)
        self.assertEqual(doc["version"], "v1.2.0")

    def test_rotation_requires_current_backup_key(self):
        fresh = Ed25519PrivateKey.generate()
        fresh_id = bytes.fromhex("d000000000000004")
        body = {
            "new_primary": self.backup_pub,
            "new_backup": minisign_pub(fresh, fresh_id),
            "revoked": [PRIMARY_ID.hex()],
            "issued_at": iso(NOW),
        }
        raw = dumps(body)
        primary_sig = minisign_sig(self.primary_sk, PRIMARY_ID, raw)
        store = self.store()
        before = (store.primary.key_id, store.backup.key_id, list(store.revoked))
        with self.assertRaises(release.ManifestError) as ctx:
            store.apply_rotation(raw, primary_sig, now=NOW)
        self.assertEqual(ctx.exception.reason, "unsigned")
        self.assertEqual((store.primary.key_id, store.backup.key_id, list(store.revoked)), before)
        broken = dict(body)
        broken["new_primary"] = "not-a-key"
        bad_raw = dumps(broken)
        bad_sig = minisign_sig(self.backup_sk, BACKUP_ID, bad_raw)
        with self.assertRaises(release.ManifestError) as ctx:
            store.apply_rotation(bad_raw, bad_sig, now=NOW)
        self.assertEqual(ctx.exception.reason, "bad-format")
        self.assertEqual(store.primary.key_id, PRIMARY_ID)

    def test_c21_old_primary_rejected_after_rotation(self):
        fresh = Ed25519PrivateKey.generate()
        fresh_id = bytes.fromhex("d000000000000004")
        fresh_pub = minisign_pub(fresh, fresh_id)
        release_raw, release_sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trust.json")
            store = self.store(revoked=["aaaaaaaaaaaaaaaa"], path=path)
            store.save(path)
            self.assertEqual(store.verify_manifest(release_raw, release_sig).key_id, PRIMARY_ID)
            rotation = dumps({
                "new_primary": self.backup_pub,
                "new_backup": fresh_pub,
                "revoked": [PRIMARY_ID.hex()],
                "issued_at": iso(NOW),
            })
            event = store.apply_rotation(rotation, minisign_sig(self.backup_sk, BACKUP_ID, rotation), now=NOW)
            self.assertEqual(event, "release-key-rotated")
            with self.assertRaises(release.ManifestError) as ctx:
                store.verify_manifest(release_raw, release_sig)
            self.assertEqual(ctx.exception.reason, "unsigned")
            promoted, promoted_sig = signed(self.backup_sk, BACKUP_ID, manifest_doc())
            self.assertEqual(store.verify_manifest(promoted, promoted_sig).key_id, BACKUP_ID)
            newer, newer_sig = signed(fresh, fresh_id, manifest_doc())
            self.assertEqual(store.verify_manifest(newer, newer_sig).key_id, fresh_id)
            loaded = release.TrustStore.load(path)
            self.assertEqual(loaded.primary.key_id, BACKUP_ID)
            self.assertEqual(loaded.backup.key_id, fresh_id)
            self.assertEqual(loaded.revoked, ["aaaaaaaaaaaaaaaa", PRIMARY_ID.hex()])
            line = release.format_audit(iso(NOW), "release", "-", "release-key-rotated", "-")
            self.assertEqual(line, iso(NOW) + " release - release-key-rotated -")
            self.assertNotIn(PRIMARY_ID.hex(), line)


class ManifestTests(Keyed):
    def test_strict_version_and_digest(self):
        mutations = []
        doc = manifest_doc(); doc["version"] = "1.2.0"; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["station"]["sha256"] = "AB" * 32; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["relay"]["images"]["relay"] = "cd" * 32; mutations.append(doc)
        doc = manifest_doc(); doc["note"] = "extra"; mutations.append(doc)
        doc = manifest_doc(); doc["components"]["station"]["tarball"] = "../station.tar.gz"; mutations.append(doc)
        doc = manifest_doc(); doc["expires"] = iso(NOW + timedelta(days=30, seconds=1)); mutations.append(doc)
        for doc in mutations:
            raw, sig = signed(self.primary_sk, PRIMARY_ID, doc)
            with self.assertRaises(release.ManifestError) as ctx:
                release.load_trusted_manifest(raw, sig, self.store(), now=NOW)
            self.assertEqual(ctx.exception.reason, "bad-format", doc.get("version"))

    def test_validity_window_and_skew_boundaries(self):
        store = self.store()
        raw, sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        self.assertEqual(release.load_trusted_manifest(raw, sig, store, now=NOW)["version"], "v1.2.0")
        # issued_at == now + 5 min is allowed; one second later is future.
        for extra, reason in ((timedelta(minutes=5), None), (timedelta(minutes=5, seconds=1), "future")):
            issued = NOW + extra
            doc = manifest_doc()
            doc["issued_at"] = iso(issued)
            doc["expires"] = iso(issued + timedelta(days=1))
            raw, sig = signed(self.primary_sk, PRIMARY_ID, doc)
            if reason is None:
                release.load_trusted_manifest(raw, sig, store, now=NOW)
            else:
                with self.assertRaises(release.ManifestError) as ctx:
                    release.load_trusted_manifest(raw, sig, store, now=NOW)
                self.assertEqual(ctx.exception.reason, reason)
        # now == expires is allowed; one second later is expired.
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=1))
        doc["expires"] = iso(NOW)
        raw, sig = signed(self.primary_sk, PRIMARY_ID, doc)
        release.load_trusted_manifest(raw, sig, store, now=NOW)
        with self.assertRaises(release.ManifestError) as ctx:
            release.load_trusted_manifest(raw, sig, store, now=NOW + timedelta(seconds=1))
        self.assertEqual(ctx.exception.reason, "expired")

    def test_c20_expired_notice_and_audit(self):
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=10))
        doc["expires"] = iso(NOW - timedelta(seconds=1))
        raw, sig = signed(self.primary_sk, PRIMARY_ID, doc)
        store = self.store()
        result = release.check_update("v1.0.0", raw, sig=sig, trust=store, now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "expired")
        self.assertEqual(result.audit, "update-check-expired")
        self.assertEqual(result.display, (
            "Can't check", "Update info is out of date. Updates may be hidden."))
        self.assertEqual(result.title, "Can't check")
        self.assertEqual(result.message, "Update info is out of date. Updates may be hidden.")
        with self.assertRaises(release.ManifestError) as ctx:
            release.load_trusted_manifest(raw, sig, store, now=NOW)
        self.assertEqual(ctx.exception.reason, "expired")
        notice = release.present_check_error(ctx.exception)
        self.assertEqual(notice["audit"], "update-check-expired")
        self.assertEqual(notice["title"], "Can't check")
        # A dict cached earlier is stale once `now` passes expires.
        cached = release.load_trusted_manifest(raw, sig, store, now=NOW - timedelta(days=1))
        stale = release.check_update("v1.0.0", cached, now=NOW)
        self.assertEqual(stale.audit, "update-check-expired")

    def test_unsigned_is_not_reported_as_expired(self):
        doc = manifest_doc()
        doc["issued_at"] = iso(NOW - timedelta(days=10))
        doc["expires"] = iso(NOW - timedelta(seconds=1))
        raw, sig = signed(self.primary_sk, PRIMARY_ID, doc)
        lines = sig.splitlines()
        blob = bytearray(base64.b64decode(lines[1]))
        blob[-1] ^= 1
        lines[1] = base64.b64encode(bytes(blob)).decode("ascii")
        bad = "\n".join(lines) + "\n"
        with self.assertRaises(release.ManifestError) as ctx:
            release.load_trusted_manifest(raw, bad, self.store(), now=NOW)
        self.assertEqual(ctx.exception.reason, "unsigned")
        with self.assertRaises(release.ManifestError) as ctx:
            release.check_update("v1.0.0", raw, now=NOW)
        self.assertEqual(ctx.exception.reason, "unsigned")
        fresh, fresh_sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        with self.assertRaises(release.ManifestError) as ctx:
            release.parse_manifest(fresh, now=NOW)
        self.assertEqual(ctx.exception.reason, "unsigned")
        self.assertEqual(
            release.parse_manifest(fresh, fresh_sig, self.store(), now=NOW)["version"], "v1.2.0")

    def test_check_update_reports_a_newer_release(self):
        raw, sig = signed(self.primary_sk, PRIMARY_ID, manifest_doc())
        result = release.check_update("v1.0.0", raw, sig=sig, trust=self.store(), now=NOW)
        self.assertTrue(result.ok)
        self.assertTrue(result.update)
        self.assertEqual(result.version, "v1.2.0")
        self.assertIsNone(result.audit)
        doc = manifest_doc()
        doc["version"] = "v1.10.0"
        numeric = release.check_update("v1.9.0", doc, now=NOW)
        self.assertTrue(numeric.update)
        same = release.check_update("v1.2.0", manifest_doc(), now=NOW)
        self.assertFalse(same.update)
        self.assertEqual(same.reason, "current")


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
