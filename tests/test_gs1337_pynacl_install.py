#!/usr/bin/env python3
"""0.13.37: PyNaCl is installed on every first-time home setup, pinned and hash-checked, and fails closed.

gs-seal-setup vendor-install is the only installer (install-home.sh runs it on every first-time setup, also for
a second bot on a computer that already has the helper). These tests drive it in-process with local wheels:
  * success: the locked wheel is installed, the manifest and lock stamp are written;
  * a hash mismatch refuses with a clear message and installs nothing;
  * a download failure refuses with a clear message and installs nothing (no fallback);
  * an already-present set that matches the lock is kept, with no download;
and check install-home.sh runs vendor-install on both paths and dies with a clear message when it fails.
"""
import hashlib, importlib.machinery, importlib.util, io, json, os, re, sys, tempfile, unittest, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(ROOT, "scripts", "gs-seal-setup")
INSTALL = os.path.join(ROOT, "scripts", "install-home.sh")
LOCK = os.path.join(ROOT, "scripts", "vendor-lock.txt")
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _load():
    loader = importlib.machinery.SourceFileLoader("gs_seal_setup_1337", SETUP)
    spec = importlib.util.spec_from_loader("gs_seal_setup_1337", loader)
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)  # the helper narrows sys.path to the stdlib when it loads
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


su = _load()


def wheel(version="1.6.2", extra=b""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nacl/__init__.py", ("__version__ = %r\n" % version).encode() + extra)
        zf.writestr("nacl/public.py", b"")
        zf.writestr("nacl/signing.py", b"")
        zf.writestr("pynacl-%s.dist-info/METADATA" % version, b"Name: pynacl\nVersion: %s\n" % version.encode())
    return buf.getvalue()


class PyNaClInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        os.chmod(self.home, 0o700)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self._fetch = su.fetch_wheel
        self._check = su._import_check
        self.fetched = []

    def tearDown(self):
        su.fetch_wheel = self._fetch
        su._import_check = self._check
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def root(self):
        return os.path.join(self.home, ".config", "grok-station")

    def lock(self, digest, version="1.6.2"):
        path = os.path.join(self.home, "vendor-lock.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# test lock\npynacl==%s \\\n    --hash=sha256:%s\n" % (version, digest))
        return path

    def serve(self, blob):
        def fetch(name, version, hashes):
            self.fetched.append((name, version))
            return blob
        su.fetch_wheel = fetch

    def run_cli(self, lock):
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            rc = su.main(["vendor-install", "--lock", lock])
            return rc, sys.stderr.getvalue()
        finally:
            sys.stderr = old

    def test_success_installs_the_locked_wheel(self):
        blob = wheel()
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        rc, err = self.run_cli(lock)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.fetched, [("pynacl", "1.6.2")])
        vendor = os.path.join(self.root(), "vendor")
        self.assertTrue(os.path.isfile(os.path.join(vendor, "nacl", "__init__.py")))
        doc = json.load(open(os.path.join(self.root(), "vendor-manifest.json")))
        self.assertIn("nacl/__init__.py", doc)
        stamp = open(os.path.join(self.root(), "vendor-lock.sha256")).read().strip()
        self.assertRegex(stamp, r"^[0-9a-f]{64}$")
        self.assertEqual(os.stat(os.path.join(self.root(), "vendor-lock.sha256")).st_mode & 0o777, 0o600)

    def test_hash_mismatch_refuses_and_installs_nothing(self):
        blob = wheel()
        lock = self.lock("ab" * 32)  # the lock pins a different file than the one served
        self.serve(blob)
        rc, err = self.run_cli(lock)
        self.assertEqual(rc, 1)
        self.assertIn("PyNaCl install refused: the pynacl 1.6.2 download did not match its pinned sha256.", err)
        self.assertIn("Nothing unverified was installed", err)
        self.assertFalse(os.path.exists(os.path.join(self.root(), "vendor")))
        self.assertFalse(os.path.exists(os.path.join(self.root(), "vendor-manifest.json")))
        self.assertEqual([n for n in os.listdir(self.root()) if n.startswith(".vendor-")], [])

    def test_network_hash_mismatch_is_named(self):
        def fetch(name, version, hashes):
            raise su.Refused("hash")
        su.fetch_wheel = fetch
        rc, err = self.run_cli(self.lock("cd" * 32))
        self.assertEqual(rc, 1)
        self.assertIn("did not match its pinned sha256", err)

    def test_install_failure_refuses_without_fallback(self):
        def fetch(name, version, hashes):
            raise su.Refused()
        su.fetch_wheel = fetch
        rc, err = self.run_cli(self.lock("ef" * 32))
        self.assertEqual(rc, 1)
        self.assertIn("PyNaCl install refused: could not download pynacl 1.6.2 from PyPI", err)
        self.assertIn("Nothing unverified was installed", err)
        self.assertFalse(os.path.exists(os.path.join(self.root(), "vendor")))
        src = open(SETUP).read()
        self.assertNotRegex(src, r"\bpip install\b|ensurepip|import pip\b")

    def test_install_failure_keeps_a_previous_working_set(self):
        blob = wheel()
        self.serve(blob)
        self.assertEqual(self.run_cli(self.lock(hashlib.sha256(blob).hexdigest()))[0], 0)
        before = open(os.path.join(self.root(), "vendor", "nacl", "__init__.py"), "rb").read()

        def fetch(name, version, hashes):
            raise su.Refused()
        su.fetch_wheel = fetch
        rc, err = self.run_cli(self.lock("12" * 32, version="1.6.3"))
        self.assertEqual(rc, 1)
        self.assertIn("could not download pynacl 1.6.3", err)
        self.assertEqual(open(os.path.join(self.root(), "vendor", "nacl", "__init__.py"), "rb").read(), before)

    def test_bad_lock_is_a_clear_refusal(self):
        path = os.path.join(self.home, "bad-lock.txt")
        open(path, "w").write("pynacl>=1.0\n")
        rc, err = self.run_cli(path)
        self.assertEqual(rc, 1)
        self.assertIn("PyNaCl install refused: the vendor lock file is missing or not valid.", err)

    def test_matching_set_is_kept_without_a_download(self):
        blob = wheel()
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        self.assertEqual(self.run_cli(lock), (0, ""))
        vendor = os.path.join(self.root(), "vendor")
        ino = os.stat(vendor).st_ino
        seen = []
        su._import_check = lambda want: seen.append(want) or True
        self.fetched.clear()
        rc, err = self.run_cli(lock)
        self.assertEqual((rc, err), (0, "PyNaCl 1.6.2 is already installed and verifies. Keeping it.\n"))
        self.assertEqual(self.fetched, [])
        self.assertEqual(seen, ["1.6.2"])
        self.assertEqual(os.stat(vendor).st_ino, ino)

    def test_matching_set_is_kept_with_the_real_import_check(self):
        """The keep check imports nacl from the verified vendor tree in a fresh interpreter (no stub)."""
        blob = wheel()
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        self.assertEqual(self.run_cli(lock)[0], 0)
        self.fetched.clear()
        self.assertEqual(self.run_cli(lock), (0, "PyNaCl 1.6.2 is already installed and verifies. Keeping it.\n"))
        self.assertEqual(self.fetched, [])

    def test_real_import_check_rejects_a_wrong_nacl_version(self):
        blob = wheel(extra=b"__version__ = '1.5.0'\n")  # dist-info says 1.6.2, the module says otherwise
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        self.assertEqual(self.run_cli(lock)[0], 0)
        self.fetched.clear()
        self.assertEqual(self.run_cli(lock), (0, ""))
        self.assertEqual(self.fetched, [("pynacl", "1.6.2")])

    def test_a_set_from_an_older_install_without_a_stamp_is_kept(self):
        blob = wheel()
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        self.assertEqual(self.run_cli(lock)[0], 0)
        os.unlink(os.path.join(self.root(), "vendor-lock.sha256"))  # 0.13.36 and older wrote no stamp
        su._import_check = lambda want: True
        self.fetched.clear()
        self.assertEqual(self.run_cli(lock)[0], 0)
        self.assertEqual(self.fetched, [])
        self.assertTrue(os.path.isfile(os.path.join(self.root(), "vendor-lock.sha256")))

    def test_a_different_version_or_a_tampered_set_is_reinstalled(self):
        old = wheel("1.6.1")
        self.serve(old)
        self.assertEqual(self.run_cli(self.lock(hashlib.sha256(old).hexdigest(), "1.6.1"))[0], 0)
        su._import_check = lambda want: True
        new = wheel("1.6.2")
        self.serve(new)
        self.fetched.clear()
        self.assertEqual(self.run_cli(self.lock(hashlib.sha256(new).hexdigest()))[0], 0)
        self.assertEqual(self.fetched, [("pynacl", "1.6.2")])
        # a changed file fails the manifest, so the set is downloaded again
        with open(os.path.join(self.root(), "vendor", "nacl", "public.py"), "ab") as fh:
            fh.write(b"# changed\n")
        self.fetched.clear()
        self.assertEqual(self.run_cli(self.lock(hashlib.sha256(new).hexdigest()))[0], 0)
        self.assertEqual(self.fetched, [("pynacl", "1.6.2")])

    def test_a_set_that_does_not_import_is_reinstalled(self):
        blob = wheel()
        lock = self.lock(hashlib.sha256(blob).hexdigest())
        self.serve(blob)
        self.assertEqual(self.run_cli(lock)[0], 0)
        su._import_check = lambda want: False
        self.fetched.clear()
        self.assertEqual(self.run_cli(lock)[0], 0)
        self.assertEqual(self.fetched, [("pynacl", "1.6.2")])


class InstallerWiring(unittest.TestCase):
    def test_install_home_runs_vendor_install_on_every_first_setup_and_fails_closed(self):
        text = open(INSTALL).read()
        calls = re.findall(r'^"\$S/gs-seal-setup" vendor-install --lock "\$S/vendor-lock.txt" \\\n  \|\| die "PyNaCl was not installed', text, re.M)
        self.assertEqual(len(calls), 1)
        # outside the "helper not present" branch, so a second bot on this computer runs it too
        start = text.index('if [ "$have" = "1" ]; then\n  S="$BIN"')
        branch_end = text.index("\nfi\n", start)
        call = text.index('"$S/gs-seal-setup" vendor-install')
        self.assertGreater(call, branch_end)
        self.assertLess(call, text.index("phase=setup"))
        self.assertNotIn("pip install", text)

    def test_lock_pins_pynacl_with_hashes_and_says_why(self):
        reqs = su.parse_vendor_lock(open(LOCK).read())
        names = {n: v for n, v, _h in reqs}
        self.assertEqual(names.get("pynacl"), "1.6.2")
        for _n, _v, hashes in reqs:
            self.assertTrue(hashes and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes))
        self.assertIn("# Why: PyNaCl seals each reply", open(LOCK).read())


if __name__ == "__main__":
    unittest.main()
