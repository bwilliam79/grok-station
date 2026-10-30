"""Default bot avatars: stable index, shipped SVGs, and the display fallback. No network."""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
import avatars
import grok_station as gs
import pygame


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _bytes(surf):
    return pygame.image.tobytes(surf, "RGBA")


class IndexTests(unittest.TestCase):
    def test_index_is_stable_and_in_range(self):
        import hashlib
        seen = set()
        for slug in ("nix", "kai", "aa", "ab", "kitchen-bot", "a" * 16):
            a = avatars.default_avatar_index(slug)
            self.assertEqual(a, avatars.default_avatar_index(slug))
            self.assertGreaterEqual(a, 1)
            self.assertLessEqual(a, 18)
            self.assertEqual(a, int(hashlib.sha256(slug.encode()).hexdigest(), 16) % 18 + 1)
            seen.add(a)
        vals = [avatars.default_avatar_index(s) for s in ("aa", "ab", "ac", "ad", "ae", "af", "ag", "ah")]
        self.assertGreater(len(set(vals)), 1)
        self.assertGreater(len(seen), 1)

    def test_eighteen_svgs_parse(self):
        root = os.path.join(ROOT, "assets", "avatars")
        names = sorted(os.listdir(root))
        self.assertEqual(names, ["default-%02d.svg" % i for i in range(1, 19)])
        for name in names:
            tree = ET.parse(os.path.join(root, name))
            self.assertTrue(tree.getroot().tag.endswith("svg"), name)
        with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("function defaultAvatarIndex", js)
        self.assertIn("% 18", js)
        self.assertIn("/avatars/default-", js)
        self.assertIn('addEventListener("error", onAvatarError, true)', js)  # 0.13.9: no inline onerror (CSP)
        self.assertIn("avatarOnError", js)


class DisplayFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_file = gs.PERSONAS_FILE
        self.old_dirs = list(gs.PORTRAIT_DIRS)
        self.old_avatar = gs.AVATAR_DIR
        self.old_personas = gs._personas.copy()
        personas = os.path.join(self.tmp, "personas.json")
        with open(personas, "w") as fh:
            fh.write('{"personas":[{"slug":"nix","name":"Nix"}]}')
        gs.PERSONAS_FILE = personas
        empty = os.path.join(self.tmp, "portraits")
        os.makedirs(empty)
        gs.PORTRAIT_DIRS = [empty]
        gs._personas["mtime"] = None
        gs._photo_cache.clear()

    def tearDown(self):
        gs.PERSONAS_FILE = self.old_file
        gs.PORTRAIT_DIRS[:] = self.old_dirs
        gs.AVATAR_DIR = self.old_avatar
        gs._personas.clear()
        gs._personas.update(self.old_personas)
        gs._photo_cache.clear()

    def _default(self, photo_ok=True):
        gs._photo_cache.clear()
        return gs._circle_photo("Nix", 48, 2, photo_ok)

    def test_missing_photo_uses_the_default_surface(self):
        got = self._default(True)
        empty = os.path.join(self.tmp, "no-svg")
        os.makedirs(empty)
        gs.AVATAR_DIR = empty
        mono = self._default(True)
        gs.AVATAR_DIR = self.old_avatar
        self.assertNotEqual(_bytes(got), _bytes(mono))
        side = 96
        img = gs._load_svg(avatars_path(), side)
        self.assertIsNotNone(img)
        expect = pygame.Surface((side, side), pygame.SRCALPHA)
        expect.blit(img, (0, 0))
        mask = pygame.Surface((side, side), pygame.SRCALPHA)
        pygame.draw.circle(mask, (255, 255, 255, 255), (side // 2, side // 2), side // 2)
        expect.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        self.assertEqual(_bytes(got), _bytes(expect))

    def test_photo_ok_false_for_a_known_bot_uses_the_default(self):
        photo = os.path.join(self.tmp, "portraits", "nix.png")
        surf = pygame.Surface((8, 8))
        surf.fill((255, 0, 0))
        pygame.image.save(surf, photo)
        plain = self._default(False)
        gs.PORTRAIT_DIRS = [os.path.join(self.tmp, "none")]
        os.makedirs(gs.PORTRAIT_DIRS[0])
        missing = self._default(True)
        self.assertEqual(_bytes(plain), _bytes(missing))
        gs.PORTRAIT_DIRS = [os.path.join(self.tmp, "portraits")]
        real = self._default(True)
        self.assertNotEqual(_bytes(real), _bytes(missing))

    def test_too_big_and_unreadable_fall_back(self):
        missing = self._default(True)
        big = os.path.join(self.tmp, "portraits", "nix.png")
        with open(big, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"x" * (2 << 20))
        self.assertEqual(_bytes(self._default(True)), _bytes(missing))
        with open(big, "wb") as fh:
            fh.write(b"not an image")
        self.assertEqual(_bytes(self._default(True)), _bytes(missing))

    def test_station_persona_keeps_the_mark(self):
        gs._photo_cache.clear()
        gs._MARK_CACHE.clear()
        mark = gs._circle_photo(gs.VOICE_NAME, 48, 2, True)
        quiet = gs._circle_photo(gs.VOICE_NAME, 48, 2, False)
        bot = self._default(False)
        self.assertNotEqual(_bytes(mark), _bytes(bot))
        self.assertNotEqual(_bytes(quiet), _bytes(mark))
        self.assertNotEqual(_bytes(quiet), _bytes(bot))


def avatars_path():
    return os.path.join(gs.AVATAR_DIR, "default-%02d.svg" % avatars.default_avatar_index("nix"))


if __name__ == "__main__":
    unittest.main()
