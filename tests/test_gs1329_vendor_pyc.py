"""0.13.29: Python bytecode caches in the vendor tree do not lock the home helper out.

A foreign interpreter (any python3 run with the vendor folder on its path) writes
nacl/__pycache__/*.cpython-3XX.pyc. The exact-manifest check refused every post
after that, and the helper only printed "refused".

Rule: a regular file named <module>.<tag>[.opt-N].pyc directly inside a __pycache__
folder under vendor/ is left out of the exact-file-set comparison, unless the
manifest lists it. It is still walked (owner, no group or world write, no
symlink). Everything else still has to match the manifest exactly:
  - a .pyc outside __pycache__ refuses (Python imports a sourceless module.pyc);
  - any other file in __pycache__, or a folder inside it, refuses;
  - a missing or modified manifest file refuses.
load_nacl also points sys.pycache_prefix at a path that cannot exist, so a cached
file that was left out of the check is never read in place of the checked source.
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import marshal
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_common():
    loader = importlib.machinery.SourceFileLoader(
        "gs1329_common", os.path.join(ROOT, "scripts", "gs_seal_common.py"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gs1329_common", loader))
    loader.exec_module(mod)
    return mod


gsc = _load_common()
GOOD = b"MARKER = 'checked-source'\n"


def _write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _tree(files):
    """A trusted home with vendor files and an exact manifest for them."""
    home = tempfile.mkdtemp(prefix="gs1329-vendor-")
    vendor = os.path.join(home, ".config", "grok-station", "vendor")
    for rel, data in files.items():
        _write(os.path.join(vendor, *rel.split("/")), data)
    for dirpath, _dirs, _files in os.walk(home):
        os.chmod(dirpath, 0o700)
    manifest = os.path.join(home, ".config", "grok-station", "vendor-manifest.json")
    doc = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(doc, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(manifest, 0o600)
    return home, vendor


def _add(vendor, rel, data=b"\x00junk", mode=0o644):
    path = os.path.join(vendor, *rel.split("/"))
    _write(path, data, mode)
    d = os.path.dirname(path)
    while d.startswith(vendor):
        os.chmod(d, 0o700)
        d = os.path.dirname(d)
    return path


FILES = {"nacl/__init__.py": GOOD, "nacl/bindings/__init__.py": b"X = 1\n"}


class VendorBytecode(unittest.TestCase):
    def setUp(self):
        self.uid = os.geteuid()

    def test_cache_files_from_another_python_do_not_refuse(self):
        home, vendor = _tree(FILES)
        for rel in ("nacl/__pycache__/__init__.cpython-314.pyc",
                    "nacl/__pycache__/exceptions.cpython-314.pyc",
                    "nacl/__pycache__/signing.cpython-314.pyc",
                    "nacl/__pycache__/x.cpython-314.pyc",
                    "nacl/bindings/__pycache__/__init__.cpython-314.pyc",
                    "nacl/bindings/__pycache__/crypto_aead.cpython-39.opt-1.pyc"):
            _add(vendor, rel)
        gsc.assert_vendor_trusted(home, self.uid)  # must not raise

    def test_pyc_outside_pycache_still_refuses(self):
        # Python imports a sourceless nacl/foo.pyc as module nacl.foo, so it is code, not a cache.
        for rel in ("nacl/signing.pyc", "nacl/x.cpython-314.pyc", "x.pyc"):
            home, vendor = _tree(FILES)
            _add(vendor, rel)
            with self.assertRaises(gsc.Refused, msg=rel):
                gsc.assert_vendor_trusted(home, self.uid)

    def test_other_extra_files_still_refuse(self):
        for rel in ("nacl/extra.py", "nacl/__pycache__/notes.txt", "nacl/__pycache__/x.pyc.py",
                    "nacl/__pycache__/x.py", "nacl/__pycache__/sub/x.cpython-314.pyc",
                    "nacl/__pycache__/.cpython-314.pyc", "nacl/__pycache__/x.cpython-314.pyc.bak",
                    "__pycache__.pth"):
            home, vendor = _tree(FILES)
            _add(vendor, rel)
            with self.assertRaises(gsc.Refused, msg=rel):
                gsc.assert_vendor_trusted(home, self.uid)

    def test_an_untrusted_cache_file_still_refuses(self):
        home, vendor = _tree(FILES)
        _add(vendor, "nacl/__pycache__/x.cpython-314.pyc", mode=0o666)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)
        home, vendor = _tree(FILES)
        d = os.path.join(vendor, "nacl", "__pycache__")
        os.makedirs(d)
        os.symlink("/etc/hosts", os.path.join(d, "x.cpython-314.pyc"))
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)

    def test_modified_or_missing_manifest_file_still_refuses_with_caches_present(self):
        home, vendor = _tree(FILES)
        _add(vendor, "nacl/__pycache__/__init__.cpython-314.pyc")
        _write(os.path.join(vendor, "nacl", "__init__.py"), b"MARKER = 'changed'\n")
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)
        home, vendor = _tree(FILES)
        _add(vendor, "nacl/__pycache__/__init__.cpython-314.pyc")
        os.remove(os.path.join(vendor, "nacl", "bindings", "__init__.py"))
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)

    def test_a_cache_file_the_manifest_lists_is_still_digest_checked(self):
        files = dict(FILES)
        files["nacl/__pycache__/x.cpython-314.pyc"] = b"listed"
        home, vendor = _tree(files)
        gsc.assert_vendor_trusted(home, self.uid)
        _write(os.path.join(vendor, "nacl", "__pycache__", "x.cpython-314.pyc"), b"changed")
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)
        home, vendor = _tree(files)
        os.remove(os.path.join(vendor, "nacl", "__pycache__", "x.cpython-314.pyc"))
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, self.uid)

    def test_load_nacl_never_reads_a_planted_cache(self):
        """A cache whose header matches the checked source must not replace it at import."""
        home, vendor = _tree({"nacl/__init__.py": GOOD})
        src = os.path.join(vendor, "nacl", "__init__.py")
        st = os.stat(src)
        evil = compile("MARKER = 'planted-cache'\n", src, "exec")
        data = bytearray(importlib.util.MAGIC_NUMBER)
        data += (0).to_bytes(4, "little")
        data += (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
        data += (st.st_size & 0xFFFFFFFF).to_bytes(4, "little")
        data += marshal.dumps(evil)
        pyc = importlib.util.cache_from_source(src) if sys.pycache_prefix is None else None
        if pyc is None:
            self.skipTest("this interpreter already has a pycache prefix")
        _add(vendor, os.path.relpath(pyc, vendor).replace(os.sep, "/"), bytes(data))
        saved_path, saved_prefix = list(sys.path), sys.pycache_prefix
        saved_mods = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "nacl" or k.startswith("nacl.")}
        try:
            mod = gsc.load_nacl(home, self.uid)
            self.assertEqual(mod.MARKER, "checked-source")
            self.assertIsNotNone(sys.pycache_prefix)
            self.assertFalse(os.path.exists(sys.pycache_prefix))
        finally:
            for k in [k for k in sys.modules if k == "nacl" or k.startswith("nacl.")]:
                del sys.modules[k]
            sys.modules.update(saved_mods)
            sys.path[:] = saved_path
            sys.pycache_prefix = saved_prefix


if __name__ == "__main__":
    unittest.main()
