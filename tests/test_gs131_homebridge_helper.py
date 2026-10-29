"""The root helper's homebridge-set/get/delete. Subprocess, not ga.helper.

Stdout of set and delete is {"ok": true} only. The password is never on stdout
except from homebridge-get, which the admin process reads and does not log.
"""
import json, os, pwd, stat, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "admin", "grok-station-helper")
PY = sys.executable
SECRET = "hb-secret-pass"


def run(cmd, stdin=None, env=None):
    proc = subprocess.run([PY, HELPER, cmd], input=json.dumps(stdin).encode() if stdin is not None else None,
                          capture_output=True, env=env, timeout=10)
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


class HelperCreds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gs131-hb-helper-")
        self.path = os.path.join(self.tmp, "secrets", "homebridge.json")
        self.env = os.environ.copy()
        self.env["GS_HOMEBRIDGE_CREDS"] = self.path
        self.env["GS_SECRET_SKIP_CHOWN"] = "1"

    def tearDown(self):
        for dirpath, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.unlink(os.path.join(dirpath, name))
            os.rmdir(dirpath)

    def test_set_get_delete_and_no_secret_on_set_stdout(self):
        code, out, err = run("homebridge-set", {"username": "grok-station", "password": SECRET}, self.env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertNotIn(SECRET, out)
        self.assertNotIn(SECRET, err)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(json.load(open(self.path, encoding="utf-8")),
                         {"username": "grok-station", "password": SECRET})

        code, out, err = run("homebridge-get", env=self.env)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"username": "grok-station", "password": SECRET})

        code, out, err = run("homebridge-delete", env=self.env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertNotIn(SECRET, out)
        self.assertFalse(os.path.exists(self.path))
        # Idempotent. No backup appears.
        code, out, err = run("homebridge-delete", env=self.env)
        self.assertEqual(code, 0, out + err)
        names = []
        for dirpath, _dirs, files in os.walk(self.tmp):
            names.extend(files)
        self.assertEqual(names, [])

        code, out, err = run("homebridge-get", env=self.env)
        self.assertNotEqual(code, 0)
        self.assertNotIn(SECRET, out + err)

    def test_bad_input_writes_nothing_and_does_not_echo(self):
        for body in (
            {"username": "grok station", "password": SECRET},
            {"username": "grok-station", "password": "short"},
            {"username": "grok-station", "password": "has\nnewline-pass"},
            {"username": "grok-station", "password": SECRET, "base": "http://homebridge.local"},
        ):
            code, out, err = run("homebridge-set", body, self.env)
            self.assertNotEqual(code, 0, body)
            self.assertNotIn(SECRET, out)
            self.assertNotIn(SECRET, err)
            self.assertFalse(os.path.exists(self.path))

    def test_path_must_be_homebridge_json(self):
        self.env["GS_HOMEBRIDGE_CREDS"] = os.path.join(self.tmp, "secrets", "other.json")
        code, out, err = run("homebridge-set", {"username": "grok-station", "password": SECRET}, self.env)
        self.assertNotEqual(code, 0)
        self.assertIn("bad path", out)
        self.assertNotIn(SECRET, out + err)
        self.env["GS_HOMEBRIDGE_CREDS"] = os.path.join(self.tmp, "..", "homebridge.json")
        code, out, _err = run("homebridge-set", {"username": "grok-station", "password": SECRET}, self.env)
        self.assertNotEqual(code, 0)
        self.assertIn("bad path", out)

    def test_replace_does_not_follow_a_symlink(self):
        other = os.path.join(self.tmp, "other.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(other, "w", encoding="utf-8") as fh:
            fh.write('{"username":"kept","password":"unchanged-value"}\n')
        os.symlink(other, self.path)
        code, out, err = run("homebridge-set", {"username": "grok-station", "password": SECRET}, self.env)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.islink(self.path))
        self.assertIn("unchanged-value", open(other, encoding="utf-8").read())
        self.assertNotIn(SECRET, open(other, encoding="utf-8").read())
        self.assertEqual(json.load(open(self.path, encoding="utf-8"))["password"], SECRET)

    def test_missing_owner_leaves_no_file(self):
        env = self.env.copy()
        del env["GS_SECRET_SKIP_CHOWN"]
        try:
            pwd.getpwnam("grok-relay")
            have_user = True
        except KeyError:
            have_user = False
        code, out, err = run("homebridge-set", {"username": "grok-station", "password": SECRET}, env)
        self.assertNotIn(SECRET, out + err)
        if not have_user or code != 0:
            self.assertNotEqual(code, 0)
            self.assertTrue("no owner" in out or "couldn't save" in out)
            self.assertFalse(os.path.exists(self.path))
            leftovers = []
            for dirpath, _dirs, files in os.walk(self.tmp):
                leftovers.extend(files)
            self.assertFalse(any(name.startswith(".hb.") for name in leftovers))
