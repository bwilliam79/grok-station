#!/usr/bin/env python3
"""0.13.14: Noor bots-row-fix SPEC rev1 (Updates switch is one button in a fixed slot) and Nix Pi findings PI-01..05.

No browser: app.js functions run under node with a tiny fake DOM. Every test fails on 0.13.13 (63142a4).
"""
import configparser
import importlib.machinery
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
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
NOT_ROOT = unittest.skipIf(os.geteuid() == 0, "root ignores directory modes")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\(", JS)
    if not m:
        raise AssertionError("no function " + name)
    nxt = JS.find("\nfunction ", m.start() + 1)
    nxt2 = JS.find("\nasync function ", m.start() + 1)
    ends = [x for x in (nxt, nxt2) if x > 0]
    return JS[m.start():min(ends)]


def _const(name):
    i = JS.find("const " + name + " = ")
    if i < 0:
        return ""
    return JS[i:JS.index(";\n", i) + 1]


FAKE_DOM = r"""
class Node { constructor(tag){ this.tagName=tag; this.children=[]; this.attrs={}; this._text=""; this.className=""; this.parentNode=null; }
  setAttribute(k,v){ this.attrs[k]=String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k){ delete this.attrs[k]; }
  appendChild(c){ this.children.push(c); c.parentNode=this; return c; }
  set textContent(v){ this._text=String(v); this.children=[]; } get textContent(){ return this._text + this.children.map(c=>c.textContent).join(""); }
  set innerHTML(v){ throw new Error("innerHTML used"); }
  get style(){ throw new Error("inline style used"); }
  querySelector(sel){ const cls = sel.replace(/^\./, ""); const walk = n => { for (const c of n.children) { if ((" " + c.className + " ").indexOf(" " + cls + " ") >= 0) return c; const r = walk(c); if (r) return r; } return null; }; return walk(this); }
}
const document = { createElement: t => new Node(t) };
function dump(n){ return {tag:n.tagName, cls:n.className, attrs:n.attrs, text:n._text, kids:n.children.map(dump)}; }
"""


def _node(prog):
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def _switch(b, failed=False):
    src = _const("UPD_SAVING") + "\n" + _const("UPD_FAIL") + "\n" + _fn("updatesRuleLine") + _fn("updatesSwitchEl")
    return _node(FAKE_DOM + src + "\nprocess.stdout.write(JSON.stringify(dump(updatesSwitchEl(%s, %s))));"
                 % (json.dumps(b), "true" if failed else "false"))


class BotsRowSwitch(unittest.TestCase):
    B = {"slug": "otto", "name": "Otto", "feed_updates": {"on": True, "rule": ""}}

    def test_switch_and_word_are_one_button_named_for_the_bot(self):
        got = _switch(self.B)
        btn = got["kids"][0]
        self.assertEqual((btn["tag"], btn["cls"]), ("button", "updbtn"))
        self.assertEqual(btn["attrs"]["role"], "switch")
        self.assertNotIn("aria-label", btn["attrs"])
        self.assertEqual(btn["attrs"]["aria-describedby"], "updrule-otto")
        track, word, vh = btn["kids"]
        self.assertEqual((track["tag"], track["cls"], track["attrs"].get("aria-hidden")), ("span", "tog on", "true"))
        self.assertEqual(word["text"], "Updates")
        self.assertEqual((vh["cls"], vh["text"]), ("vh", " for Otto"))
        self.assertEqual(len(got["kids"]), 2)  # no separate aria-hidden label any more

    def test_line_is_a_polite_status_tied_to_the_button(self):
        line = _switch(self.B)["kids"][1]
        self.assertEqual(line["attrs"]["id"], "updrule-otto")
        self.assertEqual(line["attrs"]["role"], "status")
        self.assertEqual(line["attrs"]["aria-live"], "polite")
        self.assertEqual(line["text"], "")  # 0.13.15 rev3: empty at rest

    def test_off_copy(self):
        got = _switch({"slug": "hank", "name": "Hank", "feed_updates": {"on": False}})
        self.assertEqual(got["kids"][1]["text"], "")  # 0.13.15 rev3: no "Updates off" caption
        self.assertEqual(got["kids"][0]["kids"][0]["cls"], "tog")
        self.assertNotIn("Off: nothing shows on the station", JS)

    def test_failed_line_survives_a_repaint(self):
        got = _switch(self.B, failed=True)
        self.assertEqual(got["kids"][1]["text"], "⚠ Couldn't save. Try again.")
        self.assertIn("err", got["kids"][1]["cls"].split())

    def _flip(self, ok):
        src = (_const("UPD_SAVING") + "\n" + _const("UPD_FAIL") + "\n" + _fn("updatesRuleLine") + _fn("updatesSwitchEl")
               + _fn("updatesSwitchShow") + _fn("updatesSwitchFlip"))
        prog = FAKE_DOM + """
const seen = []; let toasts = 0; let painted = 0;
function toast(){ toasts++; }
function $(){ return null; }
function paintBots(){ painted++; }
let release;
function api(m, p, body){ seen.push(snap()); return new Promise(r => { release = () => r(%s); }); }
const b = {slug:"otto", name:"Otto", feed_updates:{on:false, rule:""}};
const node = updatesSwitchEl(b, false); const sw = node.children[0]; const el = {_bots:[b]};
function snap(){ return {checked: sw.getAttribute("aria-checked"), busy: sw.getAttribute("aria-busy"),
  dis: sw.getAttribute("aria-disabled"), track: sw.children[0].className, line: node.children[1].textContent}; }
""" % ("{ok:true}" if ok else "{ok:false, data:{error:'x'}}") + src + """
const p = updatesSwitchFlip(el, b, sw);
updatesSwitchFlip(el, b, sw);  // a second click while saving is ignored
release(); p.then(() => process.stdout.write(JSON.stringify({during: seen, calls: seen.length, after: snap(),
  toasts, painted, fail: !!(el._updFail && el._updFail.otto), on: b.feed_updates.on})));
"""
        return _node(prog)

    def test_saving_flips_right_away_and_is_busy(self):
        got = self._flip(True)
        self.assertEqual(got["calls"], 1)
        self.assertEqual(got["during"][0], {"checked": "true", "busy": "true", "dis": "true", "track": "tog on",
                                            "line": "Saving…"})
        self.assertTrue(got["on"])
        self.assertEqual(got["painted"], 1)

    def test_failed_save_reverts_and_says_so_without_a_toast(self):
        got = self._flip(False)
        self.assertEqual(got["after"]["checked"], "false")
        self.assertEqual(got["after"]["track"], "tog")
        self.assertIsNone(got["after"]["busy"])
        self.assertEqual(got["after"]["line"], "⚠ Couldn't save. Try again.")
        self.assertEqual(got["toasts"], 0)
        self.assertTrue(got["fail"])
        self.assertFalse(got["on"])

    def test_slot_on_every_connected_row(self):
        row = _fn("paintBots")
        self.assertIn('<div class="updcol">${su ? su.action : `<span class="updslot" data-updslot="${esc(b.slug)}"></span>`}</div>', row)
        self.assertNotIn("${feed ?", row)
        fill = _fn("fillUpdateSwitches")
        self.assertIn("b.webhook && b.webhook.configured", fill)  # no webhook: empty slot, same size

    def test_css_grid_scoped_and_old_rules_gone(self):
        self.assertIn(".botrow .cfg:has(.updslot){display:grid;grid-template-columns:auto 176px;", CSS)
        self.assertIn(".updsw{display:grid;grid-template-rows:32px 20px;", CSS)
        self.assertIn(".updbtn:focus-visible{outline:2px solid var(--gs-teal);outline-offset:2px}", CSS)
        self.assertIn(".updsw{grid-template-rows:44px 20px}", CSS)
        self.assertIn(".updbtn{min-height:44px;", CSS)
        for gone in (".updlab", "max-width:16rem", "#blist .botrow .cfg .updslot{flex:1 1 0", "flex:1 0 100%"):
            self.assertNotIn(gone, CSS)
        self.assertNotRegex(CSS, r"\.botrow \.cfg\{display:grid")  # Grok row lock keeps its place

    def test_no_inline_styles_or_html_in_switch(self):
        for name in ("updatesSwitchEl", "updatesSwitchFlip", "updatesSwitchShow", "fillUpdateSwitches"):
            src = _fn(name)
            for bad in ("innerHTML", ".style", "insertAdjacentHTML", "toast("):
                self.assertNotIn(bad, src, name)


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


class SafeMkdirsLeaf(unittest.TestCase):
    def test_leaf_chmods_only_the_last_component(self):
        sm = _load("tools/safe-mkdirs.py", "gs_sm1314")
        sm.ROOT_UID = os.getuid()
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base)
        os.chmod(base, 0o755)
        os.makedirs(os.path.join(base, "wake", "models"))
        os.chmod(os.path.join(base, "wake"), 0o711)
        os.chmod(os.path.join(base, "wake", "models"), 0o711)
        sm.mkdirs(base, ["wake/models/enrolled"], os.getuid(), os.getgid(), mode=0o750, leaf=True)
        mode = lambda *p: stat.S_IMODE(os.stat(os.path.join(base, *p)).st_mode)
        self.assertEqual(mode("wake"), 0o711)
        self.assertEqual(mode("wake", "models"), 0o711)
        self.assertEqual(mode("wake", "models", "enrolled"), 0o750)

    def test_leaf_never_creates_parents(self):
        sm = _load("tools/safe-mkdirs.py", "gs_sm1314b")
        sm.ROOT_UID = os.getuid()
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base)
        os.chmod(base, 0o755)
        with self.assertRaises(sm.Refused):
            sm.mkdirs(base, ["wake/models/enrolled"], os.getuid(), os.getgid(), leaf=True)
        self.assertEqual(os.listdir(base), [])

    def test_cli_parses_leaf(self):
        self.assertIn('["--leaf"]', _read("tools/safe-mkdirs.py"))


class Pi01CodeIsRootOwned(unittest.TestCase):
    def test_installers_put_code_as_root(self):
        for rel in ("install.sh", "tools/install-files.sh"):
            text = _read(rel)
            self.assertNotRegex(text, r"--put grok\b", rel)
            self.assertNotRegex(text, r"-I - grok \"?\$?[A-Za-z/{}-]*\"? wake wake/models", rel)
            self.assertIn("--leaf grok", text, rel)
        self.assertRegex(_read("tools/install-files.sh"), r"-I - root /opt/grok-station wake wake/models <")
        self.assertRegex(_read("install.sh"), r'-I - root "\$O" wake wake/models <')
        self.assertIn('--put root /opt/grok-station wake/lines', _read("tools/install-files.sh"))

    def test_wake_unit_writes_no_bytecode_and_has_a_state_dir(self):
        u = _read("systemd/grok-station-wake.service")
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", u)
        self.assertIn("StateDirectory=grok-station-wake", u)

    def test_timers_move_out_of_wake(self):
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        src = _read("wake/grok_wake.py")
        self.assertIn('TIMERS_FILE = E("WAKE_TIMERS", "/var/lib/grok-station-wake/timers.json")', src)

    def test_legacy_timers_are_read_once(self):
        src = _read("wake/grok_wake.py")
        i = src.index("def timers_load():")
        body = src[i:src.index("\ndef ", i + 5)]
        self.assertIn("TIMERS_LEGACY", body)
        self.assertIn("FileNotFoundError", body)

    def test_chime_failure_is_not_fatal(self):
        src = _read("wake/grok_wake.py")
        i = src.index("def main():")
        body = src[i:i + 600]
        self.assertRegex(body, r"try:[^\n]*\n\s+make_chime\(CHIME\)\n\s+except OSError")

    def test_installer_chime_matches_listener_chime(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy")
        src = _read("wake/grok_wake.py")
        i = src.index("def make_chime(path):")
        code = src[i:src.index("\n\n\n", i)]
        ns = {"os": os, "np": np, "wave": wave}
        exec(code, ns)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        a, b = os.path.join(d, "a.wav"), os.path.join(d, "b.wav")
        ns["make_chime"](a)
        subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tools", "make-chime.py"), b], check=True)
        with wave.open(a) as wa, wave.open(b) as wb:
            self.assertEqual(wa.getparams(), wb.getparams())
            fa, fb = wa.readframes(wa.getnframes()), wb.readframes(wb.getnframes())
        diff = max(abs(x - y) for x, y in zip(memoryview(fa).cast("h"), memoryview(fb).cast("h")))
        self.assertLessEqual(diff, 1)

    def test_installers_write_the_chime(self):
        self.assertIn('make-chime.py" "$chd/chime.wav"', _read("tools/install-files.sh"))


class Pi03WakeCodePerms(unittest.TestCase):
    def setUp(self):
        self.t = _load("tools/wake-code-perms.py", "gs_wcp1314")
        self.t.ROOT_UID = os.getuid()
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.w = os.path.join(self.base, "wake")
        os.makedirs(os.path.join(self.w, "venv", "bin"))
        open(os.path.join(self.w, "venv", "bin", "python"), "w").close()
        os.makedirs(os.path.join(self.w, "__pycache__"))
        open(os.path.join(self.w, "__pycache__", "fewshot.cpython-313.pyc"), "w").close()
        os.chmod(self.w, 0o755)
        self.unit = os.path.join(self.base, "wake.service")
        with open(self.unit, "w") as f:
            f.write("ExecStart=/opt/grok-station/wake-venv/bin/python /opt/grok-station/wake/grok_wake.py\n")

    def run_tool(self):
        self.assertEqual(self.t.main(["--unit", self.unit, self.base]), 0)

    def test_removes_old_root_venv_and_pycache(self):
        self.run_tool()
        self.assertEqual(os.listdir(self.w), [])

    def test_keeps_venv_the_unit_still_uses(self):
        with open(self.unit, "w") as f:
            f.write("ExecStart=/opt/grok-station/wake/venv/bin/python x\n")
        self.run_tool()
        self.assertTrue(os.path.isdir(os.path.join(self.w, "venv")))

    def test_never_follows_a_venv_symlink(self):
        shutil.rmtree(os.path.join(self.w, "venv"))
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        open(os.path.join(outside, "keep"), "w").close()
        os.symlink(outside, os.path.join(self.w, "venv"))
        self.run_tool()
        self.assertTrue(os.path.islink(os.path.join(self.w, "venv")))
        self.assertEqual(os.listdir(outside), ["keep"])

    def test_keeps_venv_when_wake_is_writable_by_others(self):
        os.chmod(self.w, 0o775)
        self.run_tool()
        self.assertTrue(os.path.isdir(os.path.join(self.w, "venv")))

    def test_pycache_symlink_target_untouched(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        open(os.path.join(outside, "x.pyc"), "w").close()
        shutil.rmtree(os.path.join(self.w, "__pycache__"))
        os.symlink(outside, os.path.join(self.w, "__pycache__"))
        self.run_tool()
        self.assertEqual(os.listdir(outside), ["x.pyc"])

    def test_rmtree_failure_does_not_abort(self):
        real = self.t.shutil.rmtree
        def boom(*a, **k):
            raise PermissionError("x")
        boom.avoids_symlink_attacks = True
        self.t.shutil.rmtree = boom
        self.addCleanup(setattr, self.t.shutil, "rmtree", real)
        self.run_tool()

    def test_install_runs_it_after_the_unit_is_installed(self):
        text = _read("tools/install-files.sh")
        self.assertLess(text.index("systemctl daemon-reload"), text.index("wake-code-perms.py"))
        self.assertIn("tools/wake-code-perms.py", _read("install.sh"))


def _unit(rel):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(_read(rel))
    return cp["Service"]


class Pi02Hardening(unittest.TestCase):
    def test_all_three_units_are_sandboxed(self):
        for rel in ("systemd/grok-station.service", "systemd/grok-station-wake.service",
                    "systemd/grok-station-admin.service"):
            s = _unit(rel)
            self.assertEqual(s.get("ProtectSystem"), "strict", rel)
            self.assertEqual(s.get("PrivateTmp"), "yes", rel)
            self.assertTrue(s.get("ReadWritePaths"), rel)

    def test_protect_home_where_nothing_needs_home(self):
        self.assertEqual(_unit("systemd/grok-station.service").get("ProtectHome"), "yes")
        self.assertEqual(_unit("systemd/grok-station-wake.service").get("ProtectHome"), "yes")
        self.assertIsNone(_unit("systemd/grok-station-admin.service").get("ProtectHome"))  # helper git in the clone

    def test_no_new_privileges_only_where_no_sudo(self):
        self.assertEqual(_unit("systemd/grok-station.service").get("NoNewPrivileges"), "yes")
        self.assertIsNone(_unit("systemd/grok-station-wake.service").get("NoNewPrivileges"))  # sudo gs-usb-reset
        self.assertIsNone(_unit("systemd/grok-station-admin.service").get("NoNewPrivileges"))  # sudo helper
        self.assertIn('["sudo", "-n", helper, card]', _read("wake/grok_wake.py"))

    def test_write_paths_cover_every_write(self):
        rw = lambda rel: _unit(rel).get("ReadWritePaths").replace("-/", "/").split()
        wake = rw("systemd/grok-station-wake.service")
        for p in ("/opt/grok-station/wake/models/enrolled", "/opt/grok-station/portraits", "/var/lib/grok-station/enroll",
                  "/var/lib/grok-station/late", "/run/gs-usb-reset.lock"):
            self.assertIn(p, wake)
        adm = rw("systemd/grok-station-admin.service")
        for p in ("/var/lib/grok-station", "/etc/grok-station", "/etc/hosts", "/etc/NetworkManager/system-connections"):
            self.assertIn(p, adm)
        st = rw("systemd/grok-station.service")
        self.assertEqual(sorted(st), ["/run/grok-station", "/var/cache/grok-station", "/var/lib/grok-station-display"])
        h = _read("admin/grok-station-helper")
        self.assertIn('"/etc/NetworkManager/system-connections"', h)
        self.assertIn('open("/etc/hosts", "w")', h)

    def test_usb_reset_lock_exists_before_the_sandbox(self):
        u = _read("systemd/grok-station-wake.service")
        self.assertIn("ExecStartPre=+/bin/sh -c 'umask 077; [ -e /run/gs-usb-reset.lock ] || : > /run/gs-usb-reset.lock'", u)
        self.assertIn('"/run/gs-usb-reset.lock"', _read("scripts/gs-usb-reset"))

    def test_admin_clone_drop_in(self):
        text = _read("install.sh")
        self.assertIn("grok-station-admin.service.d/source-dir.conf", text)
        i = text.index("st_web() {")
        self.assertLess(text.index("source-dir.conf", i), text.index("tools/install-files.sh", i))


class Pi04NoAdm(unittest.TestCase):
    def test_admin_unit_uses_systemd_journal(self):
        g = _unit("systemd/grok-station-admin.service").get("SupplementaryGroups").split()
        self.assertNotIn("adm", g)
        self.assertIn("systemd-journal", g)

    def test_installer_drops_adm(self):
        text = _read("tools/install-files.sh")
        self.assertNotRegex(text, r"-G adm\b")
        self.assertIn("gpasswd -d grok-admin adm", text)
        self.assertIn("if getent group systemd-journal >/dev/null; then usermod -aG systemd-journal grok-admin; fi", text)

    def test_admin_never_reads_var_log(self):
        self.assertNotIn("/var/log", _read("admin/grok_admin.py"))
        self.assertIn('"journalctl"', _read("admin/grok_admin.py"))


class Pi05GainCandidates(unittest.TestCase):
    def _helper(self):
        return _load("admin/grok-station-helper", "gs_helper_1314")

    def _seen(self, env):
        h = self._helper()
        seen = []
        real = h.os.path.isfile
        h.os.path.isfile = lambda p: seen.append(p) or False
        self.addCleanup(setattr, h.os.path, "isfile", real)
        old = os.environ.pop("GROK_STATION_HELPER_TEST", None)
        if env:
            os.environ["GROK_STATION_HELPER_TEST"] = "1"
        try:
            h.station_gain.__dict__.pop("_mod", None)
            h.station_gain()
        finally:
            os.environ.pop("GROK_STATION_HELPER_TEST", None)
            if old is not None:
                os.environ["GROK_STATION_HELPER_TEST"] = old
        return seen

    def test_repo_copy_not_a_candidate_without_the_test_flag(self):
        self.assertEqual(self._seen(False), ["/opt/grok-station/gain.py"])

    @NOT_ROOT
    def test_repo_copy_only_under_the_test_flag(self):
        seen = self._seen(True)
        self.assertEqual(seen[-1], "/opt/grok-station/gain.py")
        self.assertTrue(seen[0].endswith(os.path.join("wake", "gain.py")))


if __name__ == "__main__":
    unittest.main()


class Nix131401SoundsCandidates(unittest.TestCase):
    """NIX-1314-01: as root (or without the test flag) the helper loads only /opt/grok-station/sounds.py."""

    def _seen(self, flag, env_py=None):
        h = _load("admin/grok-station-helper", "gs_helper_1314_sounds")
        seen = []
        real = h.os.path.isfile
        h.os.path.isfile = lambda p: seen.append(p) or False
        self.addCleanup(setattr, h.os.path, "isfile", real)
        keys = ("GROK_STATION_HELPER_TEST", "GROK_STATION_SOUNDS_PY")
        old = {k: os.environ.pop(k, None) for k in keys}
        if flag:
            os.environ["GROK_STATION_HELPER_TEST"] = "1"
        if env_py:
            os.environ["GROK_STATION_SOUNDS_PY"] = env_py
        try:
            h.station_sounds.__dict__.pop("_mod", None)
            h.station_sounds()
        finally:
            for k in keys:
                os.environ.pop(k, None)
                if old[k] is not None:
                    os.environ[k] = old[k]
        return seen

    def test_env_and_repo_copy_ignored_without_the_test_flag(self):
        self.assertEqual(self._seen(False, "/tmp/evil-sounds.py"), ["/opt/grok-station/sounds.py"])

    @NOT_ROOT
    def test_env_and_repo_copy_only_under_the_test_flag(self):
        seen = self._seen(True, "/tmp/test-sounds.py")
        self.assertEqual(seen[0], "/tmp/test-sounds.py")
        self.assertTrue(seen[1].endswith("sounds.py") and seen[1] != "/opt/grok-station/sounds.py")
        self.assertEqual(seen[-1], "/opt/grok-station/sounds.py")
