"""0.13.42: the USB reset must not deauthorize the speakerphone, and a stuck mic is obvious.

The 3:30 reset used to write authorized 0 then 1. On USB 328f:0109 that leaves the device
enumerated with an empty bConfigurationValue and no ALSA card, while the helper still
printed ok. These tests use a fake sysfs and a fake usbfs node.
"""
import datetime
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402,F401  sets the admin sandbox before grok_admin is imported
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
HELPER = os.path.join(ROOT, "scripts", "gs-usb-reset")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


def load_helper():
    name = "gs_usb_reset_1342"
    loader = importlib.machinery.SourceFileLoader(name, HELPER)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def make_mic(root, *, vendor="328f", product_id="0109", card_id="Plus", config="1",
             audio=True, with_card=True, product="OfficeCore M0 Plus", manufacturer="eMeet"):
    """card0 -> USB 1-2, plus a usbfs stand-in at devfs/001/002."""
    usb = os.path.join(root, "sys", "devices", "usb1", "1-2")
    iface = os.path.join(usb, "1-2:1.0")
    os.makedirs(iface)
    if audio:
        _write(os.path.join(iface, "bInterfaceClass"), "01\n")
    _write(os.path.join(usb, "idVendor"), vendor + "\n")
    _write(os.path.join(usb, "idProduct"), product_id + "\n")
    _write(os.path.join(usb, "product"), product + "\n")
    _write(os.path.join(usb, "manufacturer"), manufacturer + "\n")
    _write(os.path.join(usb, "authorized"), "1")
    _write(os.path.join(usb, "bConfigurationValue"), config)
    _write(os.path.join(usb, "busnum"), "1\n")
    _write(os.path.join(usb, "devnum"), "2\n")
    hub = os.path.join(root, "sys", "devices", "usb1")
    _write(os.path.join(hub, "idVendor"), "1d6b\n")
    _write(os.path.join(hub, "idProduct"), "0002\n")
    _write(os.path.join(hub, "authorized"), "1")
    sound = os.path.join(root, "sys", "class", "sound")
    os.makedirs(sound)
    if with_card:
        card = os.path.join(sound, "card0")
        os.makedirs(card)
        _write(os.path.join(card, "id"), card_id + "\n")
        os.symlink(iface, os.path.join(card, "device"))
    node = os.path.join(root, "devfs", "001", "002")
    os.makedirs(os.path.dirname(node))
    with open(node, "wb") as fh:
        fh.write(b"")
    # The bus view the detector walks. A real sysfs entry is a symlink.
    bus = os.path.join(root, "sys", "bus", "usb", "devices")
    os.makedirs(bus)
    os.symlink(usb, os.path.join(bus, "1-2"))
    return usb, node


def run_helper(root, *args, verify="0"):
    env = dict(os.environ)
    env["GS_USB_RESET_SYSFS"] = os.path.join(root, "sys")
    env["GS_USB_RESET_DEVFS"] = os.path.join(root, "devfs")
    env["GS_USB_RESET_VERIFY_S"] = verify
    return __import__("subprocess").run(
        [sys.executable, HELPER, *args], capture_output=True, text=True, env=env, timeout=10)


class UsbResetPort(unittest.TestCase):
    def test_never_writes_authorized_and_resets_the_port(self):
        mod = load_helper()
        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            if isinstance(path, str) and os.path.basename(path) == "authorized":
                if flags & (os.O_WRONLY | os.O_RDWR):
                    opened.append(os.path.basename(path))
            return real_open(path, flags, *args, **kwargs)

        os.open = spy
        try:
            with tempfile.TemporaryDirectory() as root:
                usb, node = make_mic(root)
                os.environ["GS_USB_RESET_SYSFS"] = os.path.join(root, "sys")
                os.environ["GS_USB_RESET_DEVFS"] = os.path.join(root, "devfs")
                os.environ["GS_USB_RESET_VERIFY_S"] = "0"
                mod.reset_card("Plus", os.path.join(root, "sys"))
                self.assertEqual(opened, [])
                self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")
                self.assertEqual(open(node, "rb").read(), b"USBDEVFS_RESET\n")
                self.assertEqual(mod.devfs_root(euid=0), "/dev/bus/usb")
                os.environ["GS_USB_RESET_DEVFS"] = "/tmp/not-usbfs"
                self.assertEqual(mod.devfs_root(euid=0), "/dev/bus/usb")
                self.assertEqual(mod.verify_limit(euid=0), mod.VERIFY_S)
        finally:
            os.open = real_open
            os.environ.pop("GS_USB_RESET_SYSFS", None)
            os.environ.pop("GS_USB_RESET_DEVFS", None)
            os.environ.pop("GS_USB_RESET_VERIFY_S", None)

    def test_failed_config_is_not_ok(self):
        with tempfile.TemporaryDirectory() as root:
            usb, node = make_mic(root, config="")
            proc = run_helper(root, "Plus")
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "refused: config failed\n")
            self.assertNotIn("ok", proc.stdout)
            self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")
            self.assertEqual(open(node, "rb").read(), b"USBDEVFS_RESET\n")

    def test_non_mic_and_unknown_args_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            usb, node = make_mic(root, vendor="1234", product_id="5678", card_id="PCH",
                                 product="Keyboard", manufacturer="Generic")
            proc = run_helper(root, "PCH")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "refused: not speakerphone\n")
            self.assertEqual(open(os.path.join(usb, "authorized")).read(), "1")
            self.assertEqual(open(node, "rb").read(), b"")
            for args in ([], ["Plus", "extra"], ["../Plus"], ["card0"], ["100"]):
                proc = run_helper(root, *args)
                self.assertNotEqual(proc.returncode, 0, args)
                self.assertTrue(proc.stdout.startswith("refused: "), proc.stdout)
                self.assertNotIn("ok", proc.stdout)


class NightAndStuck(unittest.TestCase):
    def test_night_reset_is_off_unless_chosen_after_this_release(self):
        import grok_wake as gw
        import grok_admin as ga
        old = os.environ.get("WAKE_MIC_RESET_NIGHT")
        os.environ["WAKE_MIC_RESET_NIGHT"] = "1"
        try:
            with tempfile.TemporaryDirectory() as d:
                missing = os.path.join(d, "missing.json")
                self.assertFalse(gw.mic_reset_flags(missing)["night"])
                self.assertTrue(gw.mic_reset_flags(missing)["button"])
                legacy = os.path.join(d, "mic-reset.json")
                with open(legacy, "w") as fh:
                    fh.write('{"button": true, "night": true, "drought": true}\n')
                self.assertFalse(gw.mic_reset_flags(legacy)["night"])
                os.environ["WAKE_MIC_RESET_FILE"] = legacy
                try:
                    self.assertFalse(ga.mic_reset_public()["night"])
                finally:
                    os.environ.pop("WAKE_MIC_RESET_FILE", None)
                chosen = os.path.join(d, "chosen.json")
                with open(chosen, "w") as fh:
                    fh.write('{"button": true, "night": true, "drought": true, "night_choice": "0.13.42"}\n')
                self.assertTrue(gw.mic_reset_flags(chosen)["night"])
                off = os.path.join(d, "off.json")
                with open(off, "w") as fh:
                    fh.write('{"night": false, "night_choice": "0.13.42"}\n')
                self.assertFalse(gw.mic_reset_flags(off)["night"])
        finally:
            if old is None:
                os.environ.pop("WAKE_MIC_RESET_NIGHT", None)
            else:
                os.environ["WAKE_MIC_RESET_NIGHT"] = old

    def test_no_card_does_not_retry_every_minute_and_capture_resumes(self):
        import grok_wake as gw
        logs, resets = [], []
        clock = {"t": 1000.0}
        card = {"name": None}
        base = datetime.datetime(2026, 11, 12, 3, 30)

        def local(_ts):
            return base + datetime.timedelta(seconds=clock["t"] - 1000.0)

        rec = gw.MicRecovery(
            lambda name: resets.append(name),
            now=lambda: clock["t"],
            local=local,
            flags=lambda: {"button": True, "night": True, "drought": True},
            card=lambda: card["name"],
            busy=lambda: False,
            kick=lambda: None,
            log=logs.append,
        )
        for i in range(30):
            clock["t"] = 1000.0 + i * 60
            rec.poll()
        no_card = [line for line in logs if "no card" in line]
        self.assertEqual(len(no_card), 1, logs)
        self.assertEqual(resets, [])
        card["name"] = "Plus"
        clock["t"] += 60
        rec.poll()
        self.assertEqual(resets, [])
        self.assertFalse(rec._wait_card)
        # Seeing the ALSA card is not the capture reopen. That line is logged
        # by run_capture_rounds when it opens the device.
        self.assertFalse(any("mic back: capture resumes" in line for line in logs), logs)

    def test_config_failed_does_not_schedule_another_night(self):
        import grok_wake as gw
        logs, resets = [], []
        clock = {"t": 1000.0}
        base = datetime.datetime(2026, 11, 12, 3, 30)

        def local(_ts):
            return base + datetime.timedelta(seconds=clock["t"] - 1000.0)

        def reset(name):
            resets.append(name)
            return "refused: config failed"

        rec = gw.MicRecovery(
            reset,
            now=lambda: clock["t"],
            local=local,
            flags=lambda: {"button": True, "night": True, "drought": True},
            card=lambda: "Plus",
            busy=lambda: False,
            kick=lambda: None,
            log=logs.append,
        )
        rec.poll()
        self.assertEqual(resets, ["Plus"])
        self.assertTrue(rec._wait_card)
        self.assertEqual(rec.last_night, base.date())
        for i in range(1, 12):
            clock["t"] = 1000.0 + i * 60
            rec.poll()
        self.assertEqual(resets, ["Plus"], logs)

    def test_missing_capture_is_logged_on_transition_then_every_ten_minutes(self):
        import grok_wake as gw
        logs = []
        clock = {"t": 0.0}
        finds = {"n": 0}

        class _Eof:
            def __init__(self):
                self.stdout = self

            def read(self, _n):
                return b""

            def kill(self):
                return None

        def find():
            finds["n"] += 1
            if finds["n"] <= 4:
                return None
            return "plughw:CARD=Plus,DEV=0"

        def sleep(seconds):
            clock["t"] += 600 if finds["n"] == 3 else seconds

        gw.run_capture_rounds(
            find, lambda cap: _Eof(), lambda buf, guard: None, lambda: False,
            rounds=5, sleep=sleep, now=lambda: clock["t"], log=logs.append, stall_s=30.0,
        )
        missing = [line for line in logs if "no capture device" in line]
        self.assertEqual(len(missing), 2, logs)
        self.assertEqual(sum("mic back: capture resumes" in line for line in logs), 1)

    def test_unconfigured_device_says_power_cycle(self):
        import pygame
        pygame.display.init()
        pygame.font.init()
        import grok_station as gs
        import mic_usb
        with tempfile.TemporaryDirectory() as root:
            make_mic(root, config="", with_card=False)
            found = mic_usb.unconfigured_speakerphone(os.path.join(root, "sys"))
            self.assertEqual(found["detail"], "Mic needs a power cycle")
            try:
                got = gs.check_mic(1000.0, lambda: None, "", sysfs=os.path.join(root, "sys"))
            except TypeError:
                self.fail("check_mic does not detect an unconfigured microphone")
            self.assertEqual(got["detail"], "Mic needs a power cycle")
            self.assertIs(got["ok"], False)
            # A configured device, and a different device, are not this state.
            ok_root = os.path.join(root, "ok")
            make_mic(ok_root, config="1\n", with_card=True)
            self.assertIsNone(mic_usb.unconfigured_speakerphone(os.path.join(ok_root, "sys")))
            other = os.path.join(root, "other")
            make_mic(other, vendor="1234", product_id="5678", config="", with_card=False,
                     product="Keyboard", manufacturer="Generic")
            self.assertIsNone(mic_usb.unconfigured_speakerphone(os.path.join(other, "sys")))
            gs.STATUS["mic"] = {"ok": False, "detail": "Mic needs a power cycle"}
            try:
                surf = pygame.Surface((1280, 800))
                gs.draw_idle(surf, gs.Layout(1280, 800), time_now(), _Wx(), home_band=False)
                notice = gs.LAST_MIC_NOTICE
                self.assertIsNotNone(notice)
                self.assertGreater(notice.w, 40)
                self.assertGreater(notice.h, 8)
                keep = os.environ.get("GS_ARTIFACTS", "")
                if keep:
                    os.makedirs(keep, exist_ok=True)
                    pygame.image.save(surf, os.path.join(keep, "mic-notice-1280x800.png"))
            finally:
                gs.STATUS["mic"] = {"ok": None, "detail": ""}
        page = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        self.assertIn("Mic needs a power cycle", page)
        self.assertIn("Unplug the speakerphone or switch it off and on.", page)


def time_now():
    return __import__("time").strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


if __name__ == "__main__":
    unittest.main()
