"""0.13.7 (pip-audit, Nix): untrusted portrait bytes go through Pillow. Malformed, truncated and
bomb-sized pictures are refused cleanly (415 or 413, no exception), and the relay pins the fixed Pillow."""
import io, os, re, struct, sys, unittest, zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png_header(w, h):
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))


def _good_png(size=(64, 48)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (9, 8, 7)).save(buf, format="PNG")
    return buf.getvalue()


def _good_jpeg(size=(64, 48)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (9, 8, 7)).save(buf, format="JPEG")
    return buf.getvalue()


class Malformed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import relay
        cls.relay = relay

    def go(self, raw):
        png, status = self.relay.transcode_avatar(raw)
        return png, status

    def test_good_picture_still_works(self):
        png, status = self.go(_good_png())
        self.assertIsNone(status)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_truncated_png_and_jpeg_are_415(self):
        for raw in (_good_png()[: len(_good_png()) // 2], _good_jpeg()[: len(_good_jpeg()) // 2], _png_header(16, 16)):
            self.assertEqual(self.go(raw), (None, 415))

    def test_garbage_and_bad_crc_are_415(self):
        self.assertEqual(self.go(b"\x89PNG\r\n\x1a\n" + os.urandom(64)), (None, 415))
        bad = bytearray(_good_png())
        bad[29] ^= 0xFF  # inside the IHDR crc
        self.assertEqual(self.go(bytes(bad)), (None, 415))
        self.assertEqual(self.go(b"P5\n0 1\n255\n"), (None, 415))  # zero-width PGM

    def test_bomb_sized_header_is_413_without_decoding(self):
        bomb = _png_header(40000, 40000) + _chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + _chunk(b"IEND", b"")
        self.assertLess(len(bomb), 200)
        self.assertEqual(self.go(bomb), (None, 413))

    def test_gif_with_oversized_later_frame_is_415(self):
        gif = bytes.fromhex(
            "47494638396101000100800000000000ffffff"
            "2c000000000100010000" "02024401" "00"
            "2c00000000409c409c00"
            "2c000000000100010000" "02024401" "00" "3b")
        self.assertEqual(self.go(gif), (None, 415))


    def _encode(self, fmt, **kw):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (1, 2, 3)).save(buf, format=fmt, **kw)
        return buf.getvalue()

    def test_only_png_jpeg_webp_are_opened(self):
        # Nix P1: Image.open(..., formats=["PNG", "JPEG", "WEBP"]); every other decoder stays unreachable.
        for fmt in ("TIFF", "GIF", "BMP", "PPM"):
            self.assertEqual(self.go(self._encode(fmt)), (None, 415), fmt)
        for fmt in ("PNG", "JPEG", "WEBP"):
            png, status = self.go(self._encode(fmt))
            self.assertIsNone(status, fmt)

    def test_open_names_the_formats(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        self.assertIn('Image.open(io.BytesIO(raw), formats=["PNG", "JPEG", "WEBP"])', src)


class Pins(unittest.TestCase):
    def test_relay_pins_the_fixed_versions(self):
        for name in ("requirements.in", "requirements.txt"):
            text = open(os.path.join(ROOT, "relay", name)).read()
            self.assertRegex(text, r"(?m)^pillow==13\.0\.0\b", name)
            self.assertRegex(text, r"(?m)^cryptography==50\.0\.0\b", name)
            self.assertRegex(text, r"(?m)^pynacl==1\.6\.2\b", name)
        lock = open(os.path.join(ROOT, "scripts", "vendor-lock.txt")).read()
        self.assertRegex(lock, r"(?m)^pynacl==1\.6\.2 ")
        self.assertNotRegex(lock, r"(?m)^pynacl==1\.5\.0")


    def test_install_home_pins_every_helper_file_it_fetches(self):
        # install-home.sh fetches these from GitHub at COMMIT and checks each hash, so every HASH_* must be the
        # file at COMMIT, not the work tree (0.13.7 L1: COMMIT still named 0.13.0 under 0.13.7 hashes).
        import hashlib
        import subprocess
        ih = open(os.path.join(ROOT, "scripts", "install-home.sh")).read()
        commit = re.search(r"(?m)^COMMIT=([0-9a-f]{40})$", ih).group(1)
        for var, path in (("HASH_SETUP", "scripts/gs-seal-setup"), ("HASH_REPLY", "scripts/gs-seal-reply"),
                          ("HASH_COMMON", "scripts/gs_seal_common.py"), ("HASH_LOCK", "scripts/vendor-lock.txt")):
            want = re.search(r"(?m)^%s=([0-9a-f]{64})$" % var, ih).group(1)
            blob = subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (commit, path)], capture_output=True, check=True).stdout
            self.assertEqual(want, hashlib.sha256(blob).hexdigest(), "%s at %s" % (var, commit[:12]))


    def test_helper_version_bumped_with_the_vendor_lock(self):
        # 0.13.7: the vendor lock moved to PyNaCl 1.6.2, so the helper release moves too and existing home
        # computers are offered the update (gs-update.sh installs when its helper is older than the station's).
        common = open(os.path.join(ROOT, "scripts", "gs_seal_common.py")).read()
        admin = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        manifest = open(os.path.join(ROOT, "scripts", "update-manifest.txt")).read()
        self.assertRegex(common, r'(?m)^HELPER_VERSION = "0\.13\.7"$')
        self.assertRegex(admin, r'(?m)^HELPER_RELEASE = "0\.13\.7"  #')
        self.assertRegex(manifest, r"(?m)^helper 0\.13\.7$")
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import test_admin  # sets the admin test env before grok_admin is first imported
        ga = test_admin.ga
        self.assertTrue(ga.helper_view({"key_mode": "home", "helper_version": "0.13.0"})["out_of_date"])
        self.assertFalse(ga.helper_view({"key_mode": "home", "helper_version": "0.13.7"})["out_of_date"])


if __name__ == "__main__":
    unittest.main()
