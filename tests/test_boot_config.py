#!/usr/bin/env python3
"""tools/boot-config.py: cursor and quiet-boot options in cmdline.txt, backup once, re-runs change nothing."""
import importlib.util, os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "boot-config.py")
spec = importlib.util.spec_from_file_location("boot_config", TOOL)
bc = importlib.util.module_from_spec(spec); spec.loader.exec_module(bc)
STOCK = "# stock\ndtparam=audio=on\ncamera_auto_detect=1\n\n[all]\ndisable_splash=1\n"


class BootConfig(unittest.TestCase):
    def run_tool(self, d):
        return subprocess.run([sys.executable, TOOL, "--boot", d], capture_output=True, text=True).stdout.strip()

    def test_idempotent_with_backup(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "config.txt"), "w").write(STOCK)
        open(os.path.join(d, "cmdline.txt"), "w").write("console=tty1 root=PARTUUID=x rootwait\n")
        self.assertEqual(self.run_tool(d), "reboot")
        cmd = open(os.path.join(d, "cmdline.txt")).read()
        self.assertIn("vt.global_cursor_default=0", cmd)
        self.assertIn("consoleblank=0", cmd)
        self.assertEqual(cmd.count("\n"), 1)
        self.assertEqual(open(os.path.join(d, "cmdline.txt.grok-station.bak")).read(), "console=tty1 root=PARTUUID=x rootwait\n")
        self.assertEqual(self.run_tool(d), "unchanged")
        self.assertEqual(open(os.path.join(d, "cmdline.txt")).read(), cmd)
        self.assertEqual(open(os.path.join(d, "config.txt")).read(), STOCK)  # config.txt untouched

    def test_cmdline_keeps_existing_values(self):
        self.assertEqual(bc.cmdline_txt("a consoleblank=60\n").split().count("consoleblank=60"), 1)

    def test_display_unit_hides_cursor(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station.service")).read()
        self.assertIn("setterm --cursor off", unit)


if __name__ == "__main__":
    unittest.main()
