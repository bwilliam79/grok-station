"""One-time deploy installer from admin/web/app.js.

Node loads deployScript through the module.exports guard (boot() is skipped;
it needs a browser). The generated sh runs with HOME in a temp directory and
a stub directory at the front of PATH. curl serves git blobs for the pinned
commit, or the working tree when that commit is not in this clone (the pins
passed to deployScript are then the hashes of those bytes).
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONA = "sol"
STATION = "https://station.example"
PIN = "ab" * 32
STATION_PUB = base64.b64encode(b"\x11" * 32).decode("ascii")
# Deterministic SigningKey(bytes(range(32))). Used only if the real keygen
# cannot import PyNaCl. Not a key anyone uses.
FALLBACK_SEED = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
FALLBACK_PUB = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
HOME_SEED = "known-home-seed-value"
HOME_PUB = "home-public-key-visible"

CLOUD_BOT = (
    ("tools/grok-station-reply", "grok-station-reply", 0o700),
    ("scripts/vendor-lock.txt", "vendor-lock.txt", 0o600),
)
CLOUD_HOME_ROLE = (
    ("tools/grok-station-reply", "grok-station-reply", 0o700),
)
HOME_FILES = (
    ("scripts/gs-seal-setup", "gs-seal-setup", 0o700),
    ("scripts/gs-seal-reply", "gs-seal-reply", 0o700),
    ("scripts/gs_seal_common.py", "gs_seal_common.py", 0o600),
    ("scripts/vendor-lock.txt", "vendor-lock.txt", 0o600),
)

LOADER_JS = r"""
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const root = process.argv[2];
const mode = process.argv[3];
const pinsPath = process.argv[4];
const persona = process.argv[5] || "";
const station = process.argv[6] || "";
const pin = process.argv[7] || "";
const pub = process.argv[8] || "";
const text = fs.readFileSync(pinsPath, "utf8");
let pins;
if (pinsPath.endsWith(".js")) {
  const box = {};
  vm.runInNewContext(text, box, {filename: pinsPath});
  pins = box.DEPLOY_PINS;
} else {
  pins = JSON.parse(text);
}
let src = fs.readFileSync(path.join(root, "admin", "web", "app.js"), "utf8");
if (src.indexOf("module.exports") < 0) {
  console.error("app.js has no module.exports guard");
  process.exit(2);
}
// boot() runs before the export and needs a document. Drop that call only.
src = src.replace(/\nboot\(\);\r?\n/, "\n");
const sandbox = {
  console: console,
  URL: URL,
  module: {exports: {}},
  exports: {},
  document: {addEventListener: function () {}, querySelector: function () { return null; }, getElementById: function () { return null; }},
  window: {addEventListener: function () {}},
  location: {host: "station.example"},
  fetch: async function () { return {ok: false, status: 0, json: async function () { return {}; }}; },
  setTimeout: function () { return 0; },
  clearTimeout: function () {},
  setInterval: function () { return 0; },
  clearInterval: function () {}
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {filename: "app.js"});
const fn = sandbox.module.exports.deployScript;
if (typeof fn !== "function") {
  console.error("deployScript was not exported");
  process.exit(3);
}
process.stdout.write(fn(mode, pins, persona, station, pin, pub));
"""

# gs-seal-setup is #!/usr/bin/python3 -I. The installed bytes stay the pinned
# blob; this only redirects that exec so the test does not run keygen for real.
INTERCEPT_C = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int execve(const char *pathname, char *const argv[], char *const envp[]) {
    static int (*real_execve)(const char *, char *const [], char *const []) = 0;
    const char *stub;
    int argc, i;
    char **nav;
    if (!real_execve)
        real_execve = (int (*)(const char *, char *const [], char *const []))dlsym(RTLD_NEXT, "execve");
    stub = getenv("GS_SETUP_STUB");
    if (stub && pathname && strstr(pathname, "gs-seal-setup") && !strstr(pathname, "setup-stub")) {
        argc = 0;
        while (argv && argv[argc])
            argc++;
        if (argc < 1)
            argc = 1;
        nav = calloc((size_t)argc + 1, sizeof(char *));
        if (!nav)
            return -1;
        nav[0] = (char *)stub;
        for (i = 1; i < argc; i++)
            nav[i] = argv[i];
        nav[argc] = 0;
        return real_execve(stub, nav, envp);
    }
    return real_execve(pathname, argv, envp);
}
"""

SETUP_STUB = r"""#!/bin/sh
set -eu
cmd=${1:-}
persona=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--persona" ]; then
    persona=$a
  fi
  prev=$a
done
case "$cmd" in
  vendor-install)
    exit 0
    ;;
  init)
    if [ "${GS_FAIL_INIT:-}" = "1" ]; then
      printf '%s\n' "init failed" >&2
      exit 1
    fi
    exit 0
    ;;
  keygen)
    dir="$HOME/.config/grok-station/keys"
    mkdir -p "$dir"
    chmod 700 "$HOME/.config" "$HOME/.config/grok-station" "$dir"
    printf '%s\n' "$GS_HOME_SEED" > "$dir/$persona.key"
    chmod 600 "$dir/$persona.key"
    printf '%s\n' "$GS_HOME_PUB"
    exit 0
    ;;
  *)
    printf '%s\n' "unexpected setup command" >&2
    exit 1
    ;;
esac
"""

CURL_STUB = r"""#!/bin/sh
set -eu
log=${CURL_LOG:?}
{
  printf '%s\n' CALL
  for a in "$@"; do
    printf '%s\n' "$a"
  done
} >> "$log"
out=""
prev=""
url=""
wfmt=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then
    out=$a
  elif [ "$prev" = "-w" ]; then
    wfmt=$a
  else
    case "$a" in
      -*) ;;
      *) [ "$prev" = "--max-redirs" ] || url=$a ;;
    esac
  fi
  prev=$a
done
if [ "${CURL_MODE:-ok}" = "404" ]; then
  [ -n "$wfmt" ] && printf '%s' 404
  exit 0
fi
if [ -z "$out" ] || [ -z "$url" ]; then
  printf '%s\n' "stub curl: missing -o or url" >&2
  exit 1
fi
prefix="https://raw.githubusercontent.com/bwilliam79/grok-station/"
case "$url" in
  "$prefix"*)
    tail=${url#"$prefix"}
    rel=${tail#*/}
    ;;
  *)
    printf '%s\n' "stub curl: unexpected url" >&2
    exit 1
    ;;
esac
src="${FIXTURE_DIR:?}/$rel"
if [ ! -f "$src" ]; then
  printf '%s\n' "stub curl: no fixture for $rel" >&2
  exit 1
fi
cp "$src" "$out"
[ -n "$wfmt" ] && printf '%s' 200
exit 0
"""

UNAME_STUB = r"""#!/bin/sh
if [ "${FAKE_UNAME_S:-}" != "" ] && [ "${1:-}" = "-s" ]; then
  printf '%s\n' "$FAKE_UNAME_S"
  exit 0
fi
exec /usr/bin/uname "$@"
"""

PYTHON_STUB = r'''#!/usr/bin/python3
import os
import subprocess
import sys

REAL = @@REAL@@

def record(path, argv):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("CALL\n")
        for arg in argv:
            fh.write(arg + "\n")

def is_pip(argv):
    for i, arg in enumerate(argv):
        if arg == "-m" and i + 1 < len(argv) and argv[i + 1] == "pip":
            return True
    return False

def outside_home(path, home):
    path = os.path.abspath(path)
    home = os.path.abspath(home)
    return path != home and not path.startswith(home + os.sep)

def complaint_paths(text):
    found = []
    for line in text.splitlines():
        for label in ("not owned by you or root: ", "group or world writable: ", "refusing ", "cannot stat "):
            if label in line:
                found.append(line.split(label, 1)[1].strip())
                break
    return found

args = sys.argv[1:]
if is_pip(args):
    record(os.environ.get("PIP_LOG", ""), args)
    sys.exit(0)

# The installer's tree walk uses `python3 - <bindir>`. Ancestors above HOME are
# not under the installer's control here (/tmp is world-writable; / is not uid 0).
if args and args[0] == "-":
    script = sys.stdin.buffer.read()
    proc = subprocess.run([REAL, "-", *args[1:]], input=script, capture_output=True)
    marker = b"not owned by you or root"
    if proc.returncode != 0 and marker in script:
        home = os.environ.get("HOME", "")
        paths = complaint_paths(proc.stderr.decode("utf-8", "replace"))
        if home and paths and all(outside_home(p, home) for p in paths):
            sys.exit(0)
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    sys.exit(proc.returncode)

if len(args) >= 2 and args[-1] == "keygen" and os.path.basename(args[0]) == "grok-station-reply":
    probe = subprocess.run([REAL, "-c", "import nacl.signing"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode == 0:
        os.execv(REAL, [REAL, *args])
    seed = os.environ.get("GS_CLOUD_SEED", "")
    pub = os.environ.get("GS_CLOUD_PUB", "")
    if not seed or not pub:
        sys.stderr.write("keygen needs PyNaCl or a fallback key\n")
        sys.exit(1)
    sys.stdout.write("GROK_STATION_BOT_KEY=%s\n" % seed)
    sys.stdout.write("public: %s\n" % pub)
    sys.exit(0)

os.execv(REAL, [REAL, *args])
'''


def _parse_pins(text):
    commit = re.search(r'commit:\s*"([0-9a-f]{40})"', text)
    if not commit:
        raise AssertionError("deploy-pins.js has no 40-character commit")
    files = dict(re.findall(r'"([^"]+)":\s*"([0-9a-f]{64})"', text))
    if not files:
        raise AssertionError("deploy-pins.js has no file hashes")
    return {"commit": commit.group(1), "files": files}


def _load_pins(tmp):
    with open(os.path.join(ROOT, "admin", "web", "deploy-pins.js"), encoding="utf-8") as fh:
        pins = _parse_pins(fh.read())
    blobs = {}
    for path in pins["files"]:
        try:
            blobs[path] = subprocess.check_output(
                ["git", "-C", ROOT, "show", "%s:%s" % (pins["commit"], path)])
        except subprocess.CalledProcessError:
            blobs = None
            break
    if blobs is not None:
        return pins, blobs, os.path.join(ROOT, "admin", "web", "deploy-pins.js")
    patched = {"commit": pins["commit"], "files": {}}
    blobs = {}
    for path in pins["files"]:
        with open(os.path.join(ROOT, path), "rb") as fh:
            data = fh.read()
        blobs[path] = data
        patched["files"][path] = hashlib.sha256(data).hexdigest()
    dest = os.path.join(tmp, "pins.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(patched, fh)
    return patched, blobs, dest


def _calls(text):
    found = []
    cur = None
    for line in text.splitlines():
        if line == "CALL":
            if cur is not None:
                found.append(cur)
            cur = []
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        found.append(cur)
    return found


def _has_short(argv, flag):
    for arg in argv:
        if arg.startswith("--") or not arg.startswith("-"):
            continue
        if flag in arg[1:]:
            return True
    return False


def _seal_common():
    path = os.path.join(ROOT, "scripts", "gs_seal_common.py")
    spec = importlib.util.spec_from_file_location("gs_seal_common_deploy_key", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DeployKeySetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gs-deploy-key-")
        cls.loader = os.path.join(cls.tmp, "load-deploy.js")
        with open(cls.loader, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(LOADER_JS)
        cls.pins, cls.blobs, cls.pins_path = _load_pins(cls.tmp)
        cls.scripts = {}
        cls.so = None
        cls.setup_stub = None
        cls.seal = None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _script(self, mode):
        if shutil.which("node") is None:
            self.skipTest("node is absent")
        if mode not in self.scripts:
            proc = subprocess.run(
                ["node", self.loader, ROOT, mode, self.pins_path, PERSONA, STATION, PIN, STATION_PUB],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(proc.stdout.startswith("#!/bin/sh\n"), proc.stdout[:80])
            self.scripts[mode] = proc.stdout
        return self.scripts[mode]

    def _ensure_setup_stub(self):
        if self.so:
            return
        c_path = os.path.join(self.tmp, "intercept.c")
        so_path = os.path.join(self.tmp, "intercept.so")
        with open(c_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(INTERCEPT_C)
        proc = subprocess.run(
            ["gcc", "-shared", "-fPIC", "-o", so_path, c_path, "-ldl"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stub = os.path.join(self.tmp, "setup-stub")
        with open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(SETUP_STUB)
        os.chmod(stub, 0o755)
        self.__class__.so = so_path
        self.__class__.setup_stub = stub

    def _prepare(self, mode, alter=None, preexist=None, script_text=None):
        stage = tempfile.mkdtemp(prefix="gs-stage-")
        home = tempfile.mkdtemp(prefix="gs-home-")
        os.chmod(home, 0o700)
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        stub = os.path.join(stage, "stub")
        os.mkdir(stub, 0o755)
        fix = os.path.join(stage, "fixtures")
        os.mkdir(fix, 0o755)
        for rel, data in self.blobs.items():
            body = data + (b"\xff" if rel == alter else b"")
            dest = os.path.join(fix, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(body)
        bins = {
            "curl": CURL_STUB,
            "uname": UNAME_STUB,
            "python3": PYTHON_STUB.replace("@@REAL@@", repr(sys.executable)),
        }
        for name, body in bins.items():
            path = os.path.join(stub, name)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        planted = {}
        if preexist:
            bin_dir = os.path.join(home, ".local", "share", "grok-station", "bin")
            os.makedirs(bin_dir, mode=0o700)
            for dirpath, _dirs, _files in os.walk(home):
                os.chmod(dirpath, 0o700)
            os.chmod(home, 0o700)
            for name, data in preexist.items():
                path = os.path.join(bin_dir, name)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                planted[name] = os.lstat(path).st_ino
        script = os.path.join(stage, "install.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self._script(mode) if script_text is None else script_text)
        os.chmod(script, 0o700)
        return {
            "stage": stage, "home": home, "stub": stub, "fix": fix, "script": script,
            "curl_log": os.path.join(stage, "curl.log"),
            "pip_log": os.path.join(stage, "pip.log"),
            "planted": planted,
        }

    def _run(self, mode, args=(), home_mode=False, curl_404=False, alter=None, upgrade=None, preexist=None, extra_env=None, script_text=None):
        ctx = self._prepare(mode, alter=alter, preexist=preexist, script_text=script_text)
        env = {
            "HOME": ctx["home"],
            "PATH": ctx["stub"] + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": "/tmp",
            "CURL_LOG": ctx["curl_log"],
            "PIP_LOG": ctx["pip_log"],
            "FIXTURE_DIR": ctx["fix"],
            "CURL_MODE": "404" if curl_404 else "ok",
            "GS_CLOUD_SEED": FALLBACK_SEED,
            "GS_CLOUD_PUB": FALLBACK_PUB,
        }
        if upgrade is not None:
            env["UPGRADE"] = upgrade
        if home_mode:
            self._ensure_setup_stub()
            env["FAKE_UNAME_S"] = "Darwin"
            env["LD_PRELOAD"] = self.so
            env["GS_SETUP_STUB"] = self.setup_stub
            env["GS_HOME_SEED"] = HOME_SEED
            env["GS_HOME_PUB"] = HOME_PUB
        # Callers override the Darwin default (Linux, FreeBSD) and GS_FAIL_INIT.
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["sh", ctx["script"], *args], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60)
        curl_log = ""
        pip_log = ""
        if os.path.exists(ctx["curl_log"]):
            with open(ctx["curl_log"], encoding="utf-8") as fh:
                curl_log = fh.read()
        if os.path.exists(ctx["pip_log"]):
            with open(ctx["pip_log"], encoding="utf-8") as fh:
                pip_log = fh.read()
        ctx.update(proc=proc, curl_log=curl_log, pip_log=pip_log)
        return ctx

    def _bin(self, home):
        return os.path.join(home, ".local", "share", "grok-station", "bin")

    def _assert_mode(self, path, mode, kind):
        st = os.lstat(path)
        self.assertFalse(stat.S_ISLNK(st.st_mode), path)
        if kind == "dir":
            self.assertTrue(stat.S_ISDIR(st.st_mode), path)
        else:
            self.assertTrue(stat.S_ISREG(st.st_mode), path)
        self.assertEqual(stat.S_IMODE(st.st_mode), mode, path)

    def _assert_tree(self, home):
        for rel in (".local", ".local/share", ".local/share/grok-station",
                    ".local/share/grok-station/bin"):
            self._assert_mode(os.path.join(home, rel), 0o700, "dir")

    def _assert_files(self, home, rows):
        bin_dir = self._bin(home)
        self._assert_tree(home)
        for rel, name, mode in rows:
            path = os.path.join(bin_dir, name)
            self._assert_mode(path, mode, "file")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), self.blobs[rel], name)
        self.assertEqual(self._stages(bin_dir), [])

    def _stages(self, bin_dir):
        if not os.path.isdir(bin_dir):
            return []
        return [name for name in os.listdir(bin_dir) if name.startswith(".stage.")]

    def _assert_absent(self, home, names):
        bin_dir = self._bin(home)
        for name in names:
            self.assertFalse(os.path.exists(os.path.join(bin_dir, name)), name)
        self.assertFalse(os.path.exists(os.path.join(home, ".local", "share", "grok-station", "bot-seed")))
        self.assertFalse(os.path.exists(os.path.join(home, ".local", "share", "grok-station", "keygen-done")))
        self.assertEqual(self._stages(bin_dir), [])

    def _seal(self):
        if self.seal is None:
            self.__class__.seal = _seal_common()
        return self.seal

    def test_happy_path_cloud_and_home(self):
        bot = self._run("cloud", ["bot"])
        self.assertEqual(bot["proc"].returncode, 0, bot["proc"].stderr)
        self._assert_files(bot["home"], CLOUD_BOT)
        self._assert_seed_hidden(bot["proc"], bot["home"])
        pip = _calls(bot["pip_log"])
        self.assertEqual(len(pip), 1, bot["pip_log"])
        for flag in ("--require-hashes", "--no-deps", "--only-binary", "-r"):
            self.assertIn(flag, pip[0], pip[0])

        role = self._run("cloud", ["home"])
        self.assertEqual(role["proc"].returncode, 0, role["proc"].stderr)
        self._assert_files(role["home"], CLOUD_HOME_ROLE)
        self.assertFalse(os.path.exists(os.path.join(self._bin(role["home"]), "vendor-lock.txt")))
        self.assertEqual(_calls(role["pip_log"]), [])
        self.assertFalse(os.path.exists(os.path.join(role["home"], ".local", "share", "grok-station", "bot-seed")))

        home = self._run("home", home_mode=True)
        self.assertEqual(home["proc"].returncode, 0, home["proc"].stderr)
        self._assert_files(home["home"], HOME_FILES)
        text = home["proc"].stdout + home["proc"].stderr
        self.assertIn(HOME_PUB, text)
        self.assertNotIn(HOME_SEED, text)
        key = os.path.join(home["home"], ".config", "grok-station", "keys", PERSONA + ".key")
        with open(key, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), HOME_SEED)
        self._assert_mode(key, 0o600, "file")
        self._assert_ownership_walk(home["home"], [row[1] for row in HOME_FILES])

    def _assert_seed_hidden(self, proc, home):
        seed_path = os.path.join(home, ".local", "share", "grok-station", "bot-seed")
        self._assert_mode(seed_path, 0o600, "file")
        mark = os.path.join(home, ".local", "share", "grok-station", "keygen-done")
        self._assert_mode(mark, 0o600, "file")
        with open(seed_path, encoding="utf-8") as fh:
            line = fh.read().strip()
        self.assertTrue(line.startswith("GROK_STATION_BOT_KEY="), line[:24])
        secret = line.split("=", 1)[1]
        self.assertTrue(secret)
        text = proc.stdout + proc.stderr
        self.assertNotIn(secret, text)
        self.assertNotIn(line, text)
        pubs = [ln.split(" ", 1)[1].strip() for ln in proc.stdout.splitlines() if ln.startswith("public: ")]
        self.assertEqual(len(pubs), 1, proc.stdout)
        self.assertIn(pubs[0], text)
        self.assertNotEqual(pubs[0], secret)
        self.assertRegex(proc.stdout, r"fingerprint: [0-9a-f]{4}( [0-9a-f]{4}){3}")
        try:
            from nacl.signing import SigningKey
        except Exception:
            SigningKey = None
        if SigningKey is not None:
            derived = base64.b64encode(bytes(SigningKey(base64.b64decode(secret)).verify_key)).decode("ascii")
            self.assertEqual(pubs[0], derived)

    def _assert_ownership_walk(self, home, names):
        mod = self._seal()
        bin_dir = self._bin(home)
        uid = os.geteuid()
        self.assertTrue(mod._stdlib_chain_ok(bin_dir, uid, home), bin_dir)
        try:
            mod._lstat_owned(bin_dir, uid, "dir")
            for name in names:
                mod._lstat_owned(os.path.join(bin_dir, name), uid, "file")
        except mod.Refused:
            self.fail("ownership/write-bit walk refused under %s" % bin_dir)

    def test_failed_setup_leaves_no_partial_install(self):
        out = self._run("home", home_mode=True, extra_env={"GS_FAIL_INIT": "1"})
        self.assertNotEqual(out["proc"].returncode, 0)
        self.assertIn("partial install; re-run with UPGRADE=1", out["proc"].stderr)
        self._assert_absent(out["home"], [row[1] for row in HOME_FILES])

    def test_embedded_script_under_20kb(self):
        for mode in ("cloud", "home"):
            script = self._script(mode)
            self.assertLess(len(script.encode("utf-8")), 20 * 1024, mode)

    def test_hash_mismatch_installs_nothing(self):
        out = self._run("cloud", ["bot"], alter="tools/grok-station-reply")
        self.assertNotEqual(out["proc"].returncode, 0, out["proc"].stdout)
        self.assertIn("checksum mismatch", out["proc"].stderr)
        self._assert_absent(out["home"], [row[1] for row in CLOUD_BOT])

    def test_404_and_curl_refuses_redirects(self):
        out = self._run("cloud", ["bot"], curl_404=True)
        self.assertNotEqual(out["proc"].returncode, 0, out["proc"].stdout)
        self._assert_absent(out["home"], [row[1] for row in CLOUD_BOT])
        self.assertIn("pinned file unavailable (repo private or commit gone)", out["proc"].stderr)
        calls = _calls(out["curl_log"])
        self.assertTrue(calls, out["curl_log"])
        for argv in calls:
            self.assertIn("--proto", argv)
            self.assertIn("=https", argv)
            self.assertIn("--tlsv1.2", argv)
            self.assertIn("--max-redirs", argv)
            self.assertEqual(argv[argv.index("--max-redirs") + 1], "0")
            self.assertNotIn("-L", argv)
            self.assertNotIn("--location", argv)
            self.assertFalse(_has_short(argv, "L"), argv)
            urls = [arg for arg in argv if arg.startswith("https://")]
            self.assertTrue(urls, argv)
            for url in urls:
                self.assertIn("/" + self.pins["commit"] + "/", url)
                self.assertNotIn("token", url.lower())

    def test_existing_file_and_upgrade(self):
        old = b"OLD\n"
        refused = self._run("cloud", ["bot"], preexist={"grok-station-reply": old})
        self.assertNotEqual(refused["proc"].returncode, 0, refused["proc"].stdout)
        self.assertIn("already exists", refused["proc"].stderr)
        self.assertEqual(_calls(refused["curl_log"]), [])
        path = os.path.join(self._bin(refused["home"]), "grok-station-reply")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), old)
        self.assertEqual(os.lstat(path).st_ino, refused["planted"]["grok-station-reply"])

        upgraded = self._run("cloud", ["bot"], preexist={"grok-station-reply": old}, upgrade="1")
        self.assertEqual(upgraded["proc"].returncode, 0, upgraded["proc"].stderr)
        self.assertTrue(_calls(upgraded["curl_log"]))
        path = os.path.join(self._bin(upgraded["home"]), "grok-station-reply")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.blobs["tools/grok-station-reply"])
        self._assert_mode(path, 0o700, "file")
        self._assert_seed_hidden(upgraded["proc"], upgraded["home"])

        mismatch = self._run(
            "cloud", ["bot"], preexist={"grok-station-reply": old}, upgrade="1",
            alter="tools/grok-station-reply")
        self.assertNotEqual(mismatch["proc"].returncode, 0, mismatch["proc"].stdout)
        self.assertIn("checksum mismatch", mismatch["proc"].stderr)
        path = os.path.join(self._bin(mismatch["home"]), "grok-station-reply")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), old)
        self.assertEqual(os.lstat(path).st_ino, mismatch["planted"]["grok-station-reply"])
        self.assertTrue(_calls(mismatch["curl_log"]))
        self.assertEqual(self._stages(self._bin(mismatch["home"])), [])
        self.assertFalse(os.path.exists(os.path.join(self._bin(mismatch["home"]), "vendor-lock.txt")))

    def test_pip_sudo_and_pinned_urls(self):
        cloud = self._script("cloud")
        home = self._script("home")
        for script in (cloud, home):
            syntax = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            self.assertIsNone(re.search(r"\bsudo\b", script, re.I))
            self.assertNotIn("--location", script)
            # `[ ! -L` tests for a symlink. Only the curl invocation must omit -L.
            curl_lines = [ln for ln in script.splitlines() if "curl --proto" in ln]
            self.assertTrue(curl_lines)
            for line in curl_lines:
                self.assertIn("--proto =https", line)
                self.assertIn("--tlsv1.2", line)
                self.assertIn("--max-redirs 0", line)
                self.assertNotRegex(line, r"(^|\s)-L(\s|$)")
            urls = re.findall(r"'(https://[^']+)'", script)
            raw = [u for u in urls if u.startswith("https://raw.githubusercontent.com/")]
            self.assertTrue(raw)
            self.assertRegex(self.pins["commit"], r"^[0-9a-f]{40}$")
            for url in urls:
                self.assertNotIn("token", url.lower())
                self.assertNotIn("*", url)
            for url in raw:
                self.assertIn("/" + self.pins["commit"] + "/", url)
            for line in script.splitlines():
                if "pip install" not in line:
                    continue
                for flag in ("--require-hashes", "--no-deps", "--only-binary", "-r"):
                    self.assertIn(flag, line, line)
                self.assertIn("python3 -m pip install", line)
        cloud_pips = [ln for ln in cloud.splitlines() if "pip install" in ln]
        home_pips = [ln for ln in home.splitlines() if "pip install" in ln]
        self.assertEqual(len(cloud_pips), 1, cloud_pips)
        self.assertEqual(home_pips, [])
        self.assertEqual(cloud.count("pip install"), 1)

    def _install_home_text(self):
        path = os.path.join(ROOT, "scripts", "install-home.sh")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _home_args(self):
        return ["--persona", PERSONA, "--station", STATION, "--pin-sha256", PIN, "--station-pub", STATION_PUB]

    def _secret_hidden(self, proc):
        if HOME_SEED in proc.stdout or HOME_SEED in proc.stderr:
            self.fail("seed appeared in install output")

    def test_home_script_allows_linux_and_refuses_other_os(self):
        linux = self._run("home", home_mode=True, extra_env={"FAKE_UNAME_S": "Linux"})
        self.assertEqual(linux["proc"].returncode, 0, linux["proc"].stderr)
        self.assertEqual(linux["proc"].stdout.strip(), HOME_PUB)
        self._secret_hidden(linux["proc"])
        script = self._script("home")
        self.assertIn("macOS or Linux", script)
        self.assertNotIn("Home mode runs on macOS only.", script)
        other = self._run("home", home_mode=True, extra_env={"FAKE_UNAME_S": "FreeBSD"})
        self.assertNotEqual(other["proc"].returncode, 0)
        self.assertIn("Home mode runs on macOS or Linux.", other["proc"].stderr)
        self.assertFalse(os.path.isdir(self._bin(other["home"])))

    def test_install_home_sh_writes_a_wrapper_and_only_the_public_key(self):
        text = self._install_home_text()
        syntax = subprocess.run(["sh", "-n", os.path.join(ROOT, "scripts", "install-home.sh")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn("df9221e6a62d9b7af99be12036315ba2ce760aa6", text)
        for rel in ("scripts/gs-seal-setup", "scripts/gs-seal-reply", "scripts/gs_seal_common.py", "scripts/vendor-lock.txt"):
            self.assertIn(self.pins["files"][rel], text)
        self.assertNotIn("ln -s", text)
        ran = self._run("home", args=self._home_args(), home_mode=True, script_text=text,
                        extra_env={"FAKE_UNAME_S": "Linux"})
        self.assertEqual(ran["proc"].returncode, 0, ran["proc"].stderr)
        self.assertEqual(ran["proc"].stdout, HOME_PUB + "\n")
        self._secret_hidden(ran["proc"])
        self._assert_files(ran["home"], HOME_FILES)
        wrap = os.path.join(ran["home"], ".local", "bin", "gs-seal-reply")
        self._assert_mode(wrap, 0o700, "file")
        with open(wrap, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines, [
            "#!/bin/sh",
            "exec '%s' \"$@\"" % os.path.join(self._bin(ran["home"]), "gs-seal-reply"),
        ])
        key = os.path.join(ran["home"], ".config", "grok-station", "keys", PERSONA + ".key")
        with open(key, "rb") as fh:
            stored = fh.read()
        if stored != (HOME_SEED + "\n").encode():
            self.fail("key file was not written")
        refused = self._run("home", args=self._home_args(), home_mode=True, script_text=text,
                            extra_env={"FAKE_UNAME_S": "FreeBSD"})
        self.assertNotEqual(refused["proc"].returncode, 0)
        self.assertIn("Home mode runs on macOS or Linux.", refused["proc"].stderr)
        bad = self._run("home", args=["--persona", "ka$i", "--station", STATION, "--pin-sha256", PIN, "--station-pub", STATION_PUB],
                        home_mode=True, script_text=text, extra_env={"FAKE_UNAME_S": "Linux"})
        self.assertNotEqual(bad["proc"].returncode, 0)
        self.assertIn("refusing unsafe characters", bad["proc"].stderr)
        self.assertEqual(bad["curl_log"], "")

    def test_pins_match_git_show(self):
        commit = self.pins["commit"]
        probe = subprocess.run(
            ["git", "-C", ROOT, "cat-file", "-e", commit + "^{commit}"],
            capture_output=True)
        if probe.returncode != 0:
            self.skipTest("commit object missing")
        for path, digest in self.pins["files"].items():
            data = subprocess.check_output(["git", "-C", ROOT, "show", "%s:%s" % (commit, path)])
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, path)

    def test_step3_label_and_note_have_no_internal_names(self):
        with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        step3 = js.split("const step3 = ", 1)[1].split("const readLive = ", 1)[0]
        shown = []
        shown += re.findall(r'class="lbl">(.*?)</div>', step3)
        shown += re.findall(r'class="help">(.*?)</p>', step3)
        shown += re.findall(r'class="note[^"]*">(.*?)</p>', step3)
        self.assertTrue(shown)
        blob = "\n".join(shown)
        self.assertIn("Deploy bot key setup on system running Grok Bot", blob)
        low = blob.lower()
        self.assertNotIn("seal", low)
        self.assertNotIn("homehost", low)
        self.assertNotIn("mailbox", low)


if __name__ == "__main__":
    unittest.main()


class DeployPinsReleaseCheck(unittest.TestCase):
    def test_pins_match_pinned_commit_and_head(self):
        import importlib.util
        path = os.path.join(ROOT, "tools", "check-deploy-pins.py")
        spec = importlib.util.spec_from_file_location("check_deploy_pins", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        commit, _files = mod.load_pins()
        if subprocess.run(["git", "-C", ROOT, "cat-file", "-e", commit + "^{commit}"]).returncode != 0:
            self.skipTest("pinned commit not in this clone")
        _c, _f, problems, notes = mod.check(remote=False)
        self.assertEqual(problems, [])
        for note in notes:
            self.assertTrue(
                "differs at" in note or "not pinned yet" in note or "missing at" in note,
                note)
        gen_path = os.path.join(ROOT, "tools", "gen-deploy-pins.py")
        spec = importlib.util.spec_from_file_location("gen_deploy_pins_files", gen_path)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        self.assertEqual(tuple(gen.FILES), tuple(mod.FILES))
        self.assertIn("scripts/install-home.sh", mod.FILES)
        sample = next(iter(_f))
        raw = (
            "var DEPLOY_PINS = {\n  commit: \"%s\",\n  files: {\n    \"%s\": \"%s\"\n  }\n};\n"
            % (_c, sample, "ab" * 32)
        )
        fd, dest = tempfile.mkstemp(suffix="-pins.js")
        os.close(fd)
        self.addCleanup(lambda: os.remove(dest))
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(raw)
        _c2, _f2, hard, _pending = mod.check(remote=False, pins_path=dest)
        self.assertTrue(any("pin does not match" in item for item in hard), hard)


class SealShow(unittest.TestCase):
    def test_seal_show_then_post_show(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "grok-station-reply")).read()
        self.assertIn('sub.add_parser("seal-show")', src)
        self.assertIn('"/voice/show" if args.show else "/voice/reply"', src)
