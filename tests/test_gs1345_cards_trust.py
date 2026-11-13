"""0.13.45: the helper executes only a trusted cards.py, and an install does not fall back."""
import hashlib, importlib.machinery, importlib.util, io, os, shutil, stat, sys, tempfile, unittest
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "gs-seal-reply")
COMMON = os.path.join(ROOT, "scripts", "gs_seal_common.py")
CARDS = os.path.join(ROOT, "app", "cards.py")


def _tighten(path):
    os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)


def _load(path):
    name = "gs_cards_trust_" + hashlib.sha256(path.encode()).hexdigest()[:12]
    sys.path.insert(0, os.path.dirname(path))
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    try:
        loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def _refuse(mod):
    err = io.StringIO()
    with redirect_stderr(err):
        try:
            mod.load_show_cards()
        except SystemExit as exc:
            text = err.getvalue()
            if exc.code not in (1, None):
                raise AssertionError("exit %s %r" % (exc.code, text))
            return text
    raise AssertionError("load_show_cards returned\n" + err.getvalue())


class CardsTrust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gs1345-cards-")
        _tighten(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self):
        bind = os.path.join(self.tmp, "bin")
        os.makedirs(bind)
        _tighten(bind)
        script = os.path.join(bind, "gs-seal-reply")
        shutil.copy(SCRIPT, script)
        shutil.copy(COMMON, os.path.join(bind, "gs_seal_common.py"))
        _tighten(script)
        _tighten(os.path.join(bind, "gs_seal_common.py"))
        return script

    def _place(self, name, data, mode):
        path = os.path.join(self.tmp, "bin", name)
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, mode)
        return path

    def test_a_world_writable_file_is_refused(self):
        script = self._install()
        self._place("cards.py", open(CARDS, "rb").read(), 0o666)
        text = _refuse(_load(script))
        self.assertIn("refused: helper cards.py untrusted", text)

    def test_a_symlink_is_refused(self):
        script = self._install()
        real = self._place("real-cards.py", open(CARDS, "rb").read(), 0o600)
        os.symlink(real, os.path.join(self.tmp, "bin", "cards.py"))
        text = _refuse(_load(script))
        self.assertIn("refused: helper cards.py untrusted", text)

    def test_a_missing_install_file_does_not_use_the_repo_copy(self):
        script = self._install()
        app = os.path.join(self.tmp, "app")
        os.makedirs(app)
        _tighten(app)
        shutil.copy(CARDS, os.path.join(app, "cards.py"))
        _tighten(os.path.join(app, "cards.py"))
        text = _refuse(_load(script))
        self.assertIn("refused: helper cards.py untrusted", text)
        self.assertNotIn("validate_card", text)

    def test_a_hash_mismatch_is_refused(self):
        script = self._install()
        self._place("cards.py", b"TRUSTED = False\n", 0o600)
        text = _refuse(_load(script))
        self.assertIn("refused: helper cards.py untrusted", text)

    def test_a_good_file_is_accepted(self):
        script = self._install()
        data = open(CARDS, "rb").read()
        self._place("cards.py", data, 0o600)
        mod = _load(script)
        self.assertEqual(mod.CARDS_SHA256, hashlib.sha256(data).hexdigest())
        loaded = mod.load_show_cards()
        self.assertTrue(callable(loaded.validate_card))

    def test_a_checkout_uses_app_cards_and_not_a_missing_neighbor(self):
        root = os.path.join(self.tmp, "repo")
        for rel in ("scripts", "app", "tools", "relay"):
            os.makedirs(os.path.join(root, rel))
            _tighten(os.path.join(root, rel))
        _tighten(root)
        for rel, src in (
            ("VERSION", None),
            ("tools/check-version.py", None),
            ("relay/relay.py", None),
        ):
            path = os.path.join(root, rel)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x\n")
            _tighten(path)
        shutil.copy(CARDS, os.path.join(root, "app", "cards.py"))
        _tighten(os.path.join(root, "app", "cards.py"))
        script = os.path.join(root, "scripts", "gs-seal-reply")
        shutil.copy(SCRIPT, script)
        shutil.copy(COMMON, os.path.join(root, "scripts", "gs_seal_common.py"))
        _tighten(script)
        _tighten(os.path.join(root, "scripts", "gs_seal_common.py"))
        self.assertFalse(os.path.exists(os.path.join(root, "scripts", "cards.py")))
        mod = _load(script)
        loaded = mod.load_show_cards()
        self.assertTrue(callable(loaded.validate_card))


if __name__ == "__main__":
    unittest.main()
