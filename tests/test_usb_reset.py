"""Root helper scripts/gs-usb-reset: one ALSA card, that card's own USB device, nothing else.

GS_USB_RESET_SYSFS is honored only when the process is not real root. These tests are not root,
so they point the helper at a fake sysfs tree. sysfs_root(euid=0) still ignores the variable.
"""
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "scripts", "gs-usb-reset")


def load():
    # The helper has no .py suffix, so spec_from_file_location would refuse it.
    name = "gs_usb_reset"
    loader = importlib.machinery.SourceFileLoader(name, HELPER)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def make_tree(root, *, product="eMeet OfficeCore M0 Plus", manufacturer="eMeet", vendor="0c76",
              product_id="161f", card="0", card_id="M0", escape=False, auth_link=False):
    """A tiny sysfs: card0 -> USB device 1-2, plus a hub parent and a sibling device."""
    usb = os.path.join(root, "devices", "usb1", "1-2")
    iface = os.path.join(usb, "1-2:1.0")
    os.makedirs(iface)
    _write(os.path.join(iface, "bInterfaceClass"), "01\n")
    _write(os.path.join(usb, "idVendor"), vendor + "\n")
    _write(os.path.join(usb, "idProduct"), product_id + "\n")
    _write(os.path.join(usb, "product"), product + "\n")
    _write(os.path.join(usb, "manufacturer"), manufacturer + "\n")
    hub = os.path.join(root, "devices", "usb1")
    _write(os.path.join(hub, "idVendor"), "1d6b\n")
    _write(os.path.join(hub, "idProduct"), "0002\n")
    _write(os.path.join(hub, "product"), "USB bus\n")
    _write(os.path.join(hub, "manufacturer"), "Linux\n")
    _write(os.path.join(hub, "authorized"), "1")
    sibling = os.path.join(root, "devices", "usb1", "9-9")
    os.makedirs(sibling)
    _write(os.path.join(sibling, "idVendor"), "1234\n")
    _write(os.path.join(sibling, "idProduct"), "5678\n")
    _write(os.path.join(sibling, "product"), "Keyboard\n")
    _write(os.path.join(sibling, "manufacturer"), "Generic\n")
    _write(os.path.join(sibling, "authorized"), "KEEP")
    outside = os.path.join(root, "outside")
    os.makedirs(outside)
    _write(os.path.join(outside, "authorized"), "STAY")
    _write(os.path.join(outside, "idVendor"), "0c76\n")
    _write(os.path.join(outside, "product"), "eMeet OfficeCore M0 Plus\n")
    sound = os.path.join(root, "class", "sound")
    os.makedirs(sound)
    card_dir = os.path.join(sound, "card" + card)
    os.makedirs(card_dir)
    _write(os.path.join(card_dir, "id"), card_id + "\n")
    if auth_link:
        os.symlink(os.path.join(outside, "authorized"), os.path.join(usb, "authorized"))
    else:
        _write(os.path.join(usb, "authorized"), "1")
    target = outside if escape else iface
    os.symlink(target, os.path.join(card_dir, "device"))
    return usb


def run_helper(root, *args):
    env = dict(os.environ)
    env["GS_USB_RESET_SYSFS"] = root
    return subprocess.run([sys.executable, HELPER, *args], capture_output=True, text=True, env=env, timeout=10)


class UsbReset(unittest.TestCase):
    def test_writes_zero_then_one_only_on_that_device(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root)
            auth = os.path.join(usb, "authorized")
            seen = []
            real = mod._store

            def wrap(path, data):
                seen.append((path, data))
                real(path, data)

            mod._store = wrap
            mod.reset_card("0", root)
            self.assertEqual(seen, [(auth, b"0"), (auth, b"1")])
            self.assertEqual(open(auth).read(), "1")
            self.assertEqual(open(os.path.join(root, "devices", "usb1", "authorized")).read(), "1")
            self.assertEqual(open(os.path.join(root, "devices", "usb1", "9-9", "authorized")).read(), "KEEP")
            self.assertEqual(open(os.path.join(root, "outside", "authorized")).read(), "STAY")
            proc = run_helper(root, "M0")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "ok\n")
            self.assertEqual(proc.stderr, "")
            self.assertEqual(open(auth).read(), "1")

    def test_card_id_emeet_matches_when_the_product_string_does_not(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root, product="USB Audio", manufacturer="Generic", card_id="EMEET")
            mod.reset_card("EMEET", root)
            self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")

    def test_refuses_bad_input_and_paths_outside_the_device(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root)
            auth = os.path.join(usb, "authorized")
            for args in ([], ["0", "1"], ["../0"], ["0;reboot"], ["card0"], ["100"], ["a/b"], ["M0 Plus"], ["-1"]):
                proc = run_helper(root, *args)
                self.assertNotEqual(proc.returncode, 0, args)
                self.assertTrue(proc.stdout.startswith("refused: "), proc.stdout)
                self.assertNotIn("/", proc.stdout)
                self.assertLessEqual(len(proc.stdout.strip()), 80)
                self.assertEqual(proc.stderr, "")
            self.assertEqual(open(auth).read(), "1")
            with self.assertRaises(mod.Refuse) as caught:
                mod.reset_card("card0", root)
            self.assertEqual(caught.exception.reason, "bad card")
            self.assertEqual(open(auth).read(), "1")

            escaped = os.path.join(root, "escape")
            make_tree(escaped, escape=True)
            proc = run_helper(escaped, "0")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "refused: outside device\n")
            self.assertEqual(open(os.path.join(escaped, "outside", "authorized")).read(), "STAY")

            linked = os.path.join(root, "linked")
            make_tree(linked, auth_link=True)
            proc = run_helper(linked, "0")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(open(os.path.join(linked, "outside", "authorized")).read(), "STAY")

            other = os.path.join(root, "other")
            make_tree(other, product="Keyboard", manufacturer="Generic", card_id="PCH", vendor="1234", product_id="5678")
            proc = run_helper(other, "0")
            self.assertEqual(proc.stdout, "refused: not speakerphone\n")
            self.assertEqual(open(os.path.join(other, "devices", "usb1", "1-2", "authorized")).read(), "1")

    def test_root_ignores_the_sysfs_override(self):
        mod = load()
        os.environ["GS_USB_RESET_SYSFS"] = "/tmp/not-sysfs"
        try:
            self.assertEqual(mod.sysfs_root(euid=0), "/sys")
            self.assertEqual(mod.sysfs_root(euid=1000), "/tmp/not-sysfs")
            with self.assertRaises(mod.Refuse):
                os.environ["GS_USB_RESET_SYSFS"] = "relative/path"
                mod.sysfs_root(euid=1000)
            with self.assertRaises(mod.Refuse):
                os.environ["GS_USB_RESET_SYSFS"] = "/tmp/../etc"
                mod.sysfs_root(euid=1000)
        finally:
            os.environ.pop("GS_USB_RESET_SYSFS", None)

    def _flaky_store(self, mod, fail_ones):
        """Wrap _store: the first fail_ones writes of 1 raise; every write is recorded."""
        seen = []
        real = mod._store
        left = {"n": fail_ones}

        def wrap(path, data):
            seen.append(data)
            if data == b"1" and left["n"] > 0:
                left["n"] -= 1
                raise OSError("busy")
            real(path, data)

        mod._store = wrap
        # mod.time is the process-wide time module: put sleep back, or every later test's sleep is a no-op.
        self.addCleanup(setattr, mod.time, "sleep", mod.time.sleep)
        mod.time.sleep = lambda _s: None
        return seen

    def test_failed_restore_is_retried_until_it_sticks(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root)
            seen = self._flaky_store(mod, 1)
            mod.reset_card("0", root)
            self.assertEqual(seen, [b"0", b"1", b"1"])
            self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")

    def test_restore_that_never_sticks_refuses_nonzero(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            make_tree(root)
            seen = self._flaky_store(mod, 99)
            with self.assertRaises(mod.Refuse) as cm:
                mod.reset_card("0", root)
            self.assertEqual(cm.exception.reason, "restore failed")
            self.assertEqual(seen, [b"0"] + [b"1"] * mod.RESTORE_TRIES)
            os.environ["GS_USB_RESET_SYSFS"] = root
            old_out = sys.stdout
            try:
                out = []
                mod.sys.stdout = type("W", (), {"write": lambda _s, t: out.append(t)})()
                self.assertEqual(mod.main(["gs-usb-reset", "0"]), 1)
            finally:
                mod.sys.stdout = old_out
                os.environ.pop("GS_USB_RESET_SYSFS", None)
            self.assertEqual("".join(out), "refused: restore failed\n")

    def test_needs_an_audio_interface(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root)
            os.remove(os.path.join(usb, "1-2:1.0", "bInterfaceClass"))
            with self.assertRaises(mod.Refuse) as cm:
                mod.reset_card("0", root)
            self.assertEqual(cm.exception.reason, "not speakerphone")

    def test_real_ids_match_without_the_name(self):
        mod = load()
        self.assertIn(("328f", "0109"), mod.EMEET_IDS)
        with tempfile.TemporaryDirectory() as root:
            usb = make_tree(root, product="USB Audio", manufacturer="Generic", card_id="Device",
                            vendor="328f", product_id="0109")
            mod.reset_card("0", root)
            self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")

    def test_lock_and_minimum_interval(self):
        mod = load()
        with tempfile.TemporaryDirectory() as root:
            make_tree(root)
            lock = os.path.join(root, "lock")
            clock = {"t": 1000.0}
            mod.locked_reset("0", root, lock, now=lambda: clock["t"])
            clock["t"] += 10
            with self.assertRaises(mod.Refuse) as cm:
                mod.locked_reset("0", root, lock, now=lambda: clock["t"])
            self.assertEqual(cm.exception.reason, "too soon")
            clock["t"] += mod.MIN_INTERVAL_S
            mod.locked_reset("0", root, lock, now=lambda: clock["t"])
            import fcntl
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                clock["t"] += 1000
                with self.assertRaises(mod.Refuse) as cm:
                    mod.locked_reset("0", root, lock, now=lambda: clock["t"])
                self.assertEqual(cm.exception.reason, "busy")
            finally:
                os.close(fd)
            self.assertEqual(mod.lock_path(euid=0), "/run/gs-usb-reset.lock")
            self.assertTrue(open(HELPER).readline().startswith("#!/usr/bin/python3 -I"))

    def test_install_wiring_and_docs(self):
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn('"$S/scripts/gs-usb-reset"', files)
        self.assertIn("/usr/local/lib/grok-station/gs-usb-reset", files)
        self.assertIn("sudoers-usb.example", files)
        self.assertIn("/etc/sudoers.d/grok-station-usb", files)
        self.assertIn("install -d -o root -g root -m755 /usr/local/lib/grok-station", files)
        sudoers = open(os.path.join(ROOT, "admin", "sudoers-usb.example")).read()
        self.assertIn("grok ALL=(root) NOPASSWD: /usr/local/lib/grok-station/gs-usb-reset\n", sudoers)
        self.assertNotIn("*", sudoers)
        src = open(HELPER).read()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("eval(", src)
        docs = open(os.path.join(ROOT, "docs", "operations.md")).read()
        self.assertIn("GS_USB_RESET_SYSFS", docs)
        self.assertIn("euid", docs)
        self.assertIn("/usr/local/lib/grok-station/gs-usb-reset", docs)
        self.assertIn("0755", docs)
        self.assertIn("0440", docs)


if __name__ == "__main__":
    unittest.main()
