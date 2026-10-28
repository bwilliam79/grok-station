#!/usr/bin/env python3
"""tools/boot-config.py: cursor and quiet-boot options in cmdline.txt, no backup, refuses unsafe writes, re-runs change nothing."""
import importlib.util, os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "boot-config.py")
spec = importlib.util.spec_from_file_location("boot_config", TOOL)
bc = importlib.util.module_from_spec(spec); spec.loader.exec_module(bc)
STOCK = "# stock\ndtparam=audio=on\ncamera_auto_detect=1\n\n[all]\ndisable_splash=1\n"


class BootConfig(unittest.TestCase):
    def run_tool(self, d):
        return subprocess.run([sys.executable, TOOL, "--boot", d], capture_output=True, text=True).stdout.strip()

    def test_idempotent_without_backup(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "config.txt"), "w").write(STOCK)
        open(os.path.join(d, "cmdline.txt"), "w").write("console=tty1 root=PARTUUID=x rootwait\n")
        self.assertEqual(self.run_tool(d), "reboot")
        cmd = open(os.path.join(d, "cmdline.txt")).read()
        self.assertIn("vt.global_cursor_default=0", cmd)
        self.assertIn("consoleblank=0", cmd)
        self.assertEqual(cmd.count("\n"), 1)
        self.assertFalse(os.path.exists(os.path.join(d, "cmdline.txt.grok-station.bak")))  # 0.12.15: no backup copy
        self.assertEqual(self.run_tool(d), "unchanged")
        self.assertEqual(open(os.path.join(d, "cmdline.txt")).read(), cmd)
        self.assertEqual(open(os.path.join(d, "config.txt")).read(), STOCK)  # config.txt untouched

    def test_stale_backup_removed(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "cmdline.txt"), "w").write(bc.cmdline_txt("console=tty1\n"))
        open(os.path.join(d, "cmdline.txt.grok-station.bak"), "w").write("old\n")
        self.assertEqual(self.run_tool(d), "unchanged")
        self.assertEqual(sorted(os.listdir(d)), ["cmdline.txt"])

    def test_cmdline_keeps_existing_values(self):
        self.assertEqual(bc.cmdline_txt("a consoleblank=60\n").split().count("consoleblank=60"), 1)

    # 0.12.15 (Nix Low): no backup, so refuse any write that could leave the Pi unbootable.
    def refuse_case(self, content):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        open(path, "w").write(content)
        r = subprocess.run([sys.executable, TOOL, "--boot", d], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("reboot", r.stdout)
        self.assertTrue(r.stderr.strip())
        self.assertEqual(open(path).read(), content)  # untouched
        self.assertEqual(sorted(os.listdir(d)), ["cmdline.txt"])

    def test_refuses_empty_file(self):
        self.refuse_case("")

    def test_refuses_missing_root(self):
        self.refuse_case("console=tty1 rootwait\n")

    def test_refuses_truncated_input(self):
        self.refuse_case("console=serial0,115200 console=tty1 roo")

    def test_keeps_words_and_mode(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        orig = "console=serial0,115200 console=tty1 root=PARTUUID=abc-02 rootfstype=ext4 fsck.repair=yes rootwait\n"
        open(path, "w").write(orig)
        os.chmod(path, 0o640)
        self.assertEqual(self.run_tool(d), "reboot")
        words = open(path).read().split()
        self.assertEqual(words[:len(orig.split())], orig.split())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(d)), ["cmdline.txt"])

    # 0.12.15 (Nix Info on 4186d28)
    def test_planted_tmp_symlink_not_followed(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        open(path, "w").write("console=tty1 root=PARTUUID=x rootwait\n")
        victim = os.path.join(tempfile.mkdtemp(), "victim")
        open(victim, "w").write("keep\n")
        os.symlink(victim, path + ".grok-station.tmp")
        self.assertEqual(self.run_tool(d), "reboot")
        self.assertEqual(open(victim).read(), "keep\n")
        self.assertIn("consoleblank=0", open(path).read())
        self.assertFalse(os.path.islink(path))

    def fsync_dir_error(self, err):
        import errno
        from unittest import mock
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        open(path, "w").write("x\n")
        real = os.fsync
        calls = []
        def fake(fd):
            calls.append(fd)
            if len(calls) == 2:  # the folder fsync after the rename
                raise OSError(err, os.strerror(err))
            return real(fd)
        with mock.patch.object(bc.os, "fsync", fake):
            bc.write_durable(path, "new\n")
        self.assertEqual(open(path).read(), "new\n")
        self.assertEqual(sorted(os.listdir(d)), ["cmdline.txt"])

    def test_dir_fsync_einval_skipped(self):
        import errno
        self.fsync_dir_error(errno.EINVAL)

    def test_dir_fsync_unexpected_errno_raises(self):
        import errno
        with self.assertRaises(OSError):
            self.fsync_dir_error(errno.EIO)

    def test_refuses_multiline(self):
        self.refuse_case("console=tty1 root=PARTUUID=x rootwait\nextra=1\n")

    def test_trailing_blank_lines_ok(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "cmdline.txt"), "w").write("console=tty1 root=PARTUUID=x rootwait \n\n  \n")
        self.assertEqual(self.run_tool(d), "reboot")

    # 0.12.15 (Nix Info on 751fd74)
    def test_mode_set_with_fchmod_on_fd(self):
        from unittest import mock
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        open(path, "w").write("x\n")
        os.chmod(path, 0o640)
        seen = []
        real = os.fchmod
        def fake(fd, mode):
            seen.append(mode)
            return real(fd, mode)
        with mock.patch.object(bc.os, "fchmod", fake), \
                mock.patch.object(bc.os, "chmod", side_effect=AssertionError("path chmod used")):
            bc.write_durable(path, "new\n")
        self.assertEqual(seen, [0o640])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)

    def test_post_rename_failure_still_signals_reboot(self):
        import contextlib, errno, io
        from unittest import mock
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cmdline.txt")
        open(path, "w").write("console=tty1 root=PARTUUID=x rootwait\n")
        real = os.fsync
        calls = []
        def fake(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(errno.EIO, "io")
            return real(fd)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bc.os, "fsync", fake), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            bc.main(["--boot", d])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "reboot")
        self.assertIn("consoleblank=0", open(path).read())
        self.assertTrue(err.getvalue().strip())

    def test_install_sets_flag_on_reboot_with_error_exit(self):
        """install.sh's st_boot line, run under set -e with a tool that prints reboot and exits 1."""
        line = [l.strip() for l in open(os.path.join(ROOT, "install.sh"))
                if "tools/boot-config.py)" in l and "FLAGS/reboot" in l]
        self.assertEqual(len(line), 1)
        d = tempfile.mkdtemp()
        stub = os.path.join(d, "python3")
        open(stub, "w").write("#!/bin/sh\necho reboot\nexit 1\n")
        os.chmod(stub, 0o755)
        script = "set -e\nFLAGS=%s\nf() {\n%s\n}\nf\necho done\n" % (d, line[0])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=dict(os.environ, PATH=d + ":" + os.environ["PATH"]))
        self.assertTrue(os.path.exists(os.path.join(d, "reboot")))
        self.assertIn("done", r.stdout)

    def test_display_unit_hides_cursor(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station.service")).read()
        self.assertIn("setterm --cursor off", unit)


if __name__ == "__main__":
    unittest.main()
