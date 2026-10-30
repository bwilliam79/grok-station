#!/usr/bin/env python3
"""M1: grok-relay must not exec the grok-owned wake/train_flow.py.

The one copy lives beside relay.py. Installed it is root:root 0644, and only
after the relay directory and its parents pass the same ownership check gain.py
uses. The loader refuses a symlink and a file that is not root-owned.
"""
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY_PY = os.path.join(ROOT, "relay", "relay.py")
FLOW = os.path.join(ROOT, "relay", "train_flow.py")


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TrainFlowLoad(unittest.TestCase):
    def test_load_path_is_beside_relay_not_under_wake(self):
        src = _src("relay/relay.py")
        self.assertNotIn("wake/train_flow.py", src)
        self.assertNotRegex(src, r"""["']wake["'],\s*["']train_flow\.py["']""")
        self.assertTrue(os.path.isfile(FLOW))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "wake", "train_flow.py")))
        wake = _src("wake/grok_wake.py")
        self.assertNotRegex(wake, r"(?m)^import train_flow\s*$")
        self.assertIn('"relay"', wake)
        self.assertIn('"train_flow.py"', wake)
        # The listener loads the same file the relay does, not a second copy under wake/.
        self.assertNotIn('join(os.path.dirname(os.path.abspath(__file__)), "train_flow.py")', wake)

    def test_installed_root_root_0644_with_parent_check(self):
        sh = _src("install.sh")
        self.assertIn("install -o root -g root -m644 relay/train_flow.py", sh)
        self.assertIn('_root_tree_ok "$O/relay"', sh)
        self.assertNotIn("wake/train_flow.py", sh)
        files = _src("tools/install-files.sh")
        self.assertNotIn("wake/train_flow.py", files)
        self.assertRegex(
            files,
            r'install -o root -g root -m644 "\$S/relay/train_flow\.py" '
            r'/opt/grok-station/relay/train_flow\.py\.new && '
            r'mv /opt/grok-station/relay/train_flow\.py\.new /opt/grok-station/relay/train_flow\.py')
        i = files.index("/opt/grok-station/relay/train_flow.py.new")
        self.assertIn("st.st_uid != 0 or st.st_mode & 0o022", files[:i])

    def test_check_refuses_grok_owned_file_and_symlink(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        os.environ.setdefault("RELAY_TOKEN", "test-token")
        os.environ.setdefault("XAI_API_KEY", "test-key")
        import relay
        self.addCleanup(sys.modules.pop, "relay", None)
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.isfile(path) and os.unlink(path))
        os.chmod(path, 0o644)
        st = os.lstat(path)
        self.assertFalse(stat.S_ISLNK(st.st_mode))
        self.assertNotEqual(st.st_uid, 0)  # this user stands in for grok: not root
        self.assertFalse(relay.code_file_ok(path))
        link = path + "-link"
        os.symlink(path, link)
        self.addCleanup(lambda: os.path.islink(link) and os.unlink(link))
        self.assertFalse(relay.code_file_ok(link))
        self.assertFalse(relay.code_file_ok(link, root_uid=os.getuid()))
        got = os.path.abspath(relay.train_flow_path())
        self.assertTrue(got.endswith(os.path.join("relay", "train_flow.py")))
        self.assertNotIn(os.sep + "wake" + os.sep, got)


if __name__ == "__main__":
    unittest.main()
