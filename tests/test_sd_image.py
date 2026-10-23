#!/usr/bin/env python3
"""SD image builder: dry-run plan, published-checksum selection, no secrets, first-boot unit."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "build-image")
SCRIPT = os.path.join(TOOL, "build-image.sh")
FIRSTBOOT = os.path.join(TOOL, "firstboot.sh")
UNIT = os.path.join(TOOL, "grok-station-firstboot.service")

H_NEW = "ab" * 32
H_OLD = "cd" * 32
H_EXT = "12" * 32
H_EXT_OLD = "34" * 32
H_OTHER = "ef" * 32
NEW_URL = (
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
    "raspios_lite_arm64-2025-10-01/2025-10-01-raspios-trixie-arm64-lite.img.xz"
)
OLD_URL = (
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
    "raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz"
)


def lite(url, sha, ext, date):
    return {
        "name": "Raspberry Pi OS Lite (64-bit)",
        "url": url,
        "image_download_sha256": sha,
        "extract_sha256": ext,
        "release_date": date,
    }


class SdImage(unittest.TestCase):
    def run_script(self, *args, env=None, cwd=None):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            ["bash", SCRIPT, *args],
            capture_output=True, text=True, timeout=30, env=merged, cwd=cwd,
        )

    def select(self, doc):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "os-list.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            return self.run_script("--select-only", path)

    def test_bash_syntax(self):
        for path in (SCRIPT, FIRSTBOOT):
            proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IXUSR, path)

    def test_shellcheck_when_available(self):
        shellcheck = shutil.which("shellcheck")
        if not shellcheck:
            self.skipTest("shellcheck not installed")
        for path in (SCRIPT, FIRSTBOOT):
            proc = subprocess.run(
                [shellcheck, "-s", "bash", "-S", "warning", path],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_dry_run_prints_actions_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            blockers = os.path.join(d, "bin")
            os.makedirs(blockers)
            for name in ("curl", "losetup", "mount", "umount", "xz", "sha256sum"):
                path = os.path.join(blockers, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\necho blocked-%s >&2\nexit 99\n" % name)
                os.chmod(path, 0o755)
            out = os.path.join(d, "nested", "card.img")
            env = {"PATH": blockers + os.pathsep + os.environ.get("PATH", "")}
            proc = self.run_script("--dry-run", "--output", out, env=env, cwd=d)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(os.path.join(d, "nested")))
            self.assertNotIn("blocked-", proc.stdout)
            self.assertNotIn("blocked-", proc.stderr)
            text = proc.stdout
            for needle in (
                "https://downloads.raspberrypi.com/os_list_imagingutility_v4.json",
                "raspios_lite_arm64",
                "image_download_sha256",
                "extract_sha256",
                "sha256sum -c",
                "losetup --find --show --partscan",
                "git archive",
                "grok-station-firstboot.service",
                "/var/lib/grok-station-firstboot",
                "needs root",
                "dry-run: nothing downloaded, mounted, or written",
                "id_rsa",
                ".git",
            ):
                self.assertIn(needle, text, needle)
            commit = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
            self.assertIn(commit, text)

    def test_dry_run_xz_names_the_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "card.img.xz")
            proc = self.run_script("--dry-run", "--xz", "--output", out, cwd=d)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("xz --compress", proc.stdout)
            self.assertIn(out, proc.stdout)
            self.assertFalse(os.path.exists(out))

    def test_refuses_device_node_and_existing_file(self):
        proc = self.run_script("--dry-run", "--output", "/dev/sda")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("device node", proc.stderr)
        self.assertNotIn("sha256sum", proc.stdout)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "card.img")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("x")
            proc = self.run_script("--dry-run", "--output", out)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already exists", proc.stderr)

    def test_checksum_verification_and_no_secrets(self):
        parts = [open(path, encoding="utf-8").read() for path in (SCRIPT, FIRSTBOOT, UNIT)]
        blob = "\n".join(parts)
        script = parts[0]
        self.assertIn("set -euo pipefail\n", script)
        self.assertIn("set -euo pipefail\n", parts[1])
        self.assertIn("sha256sum -c", script)
        self.assertIn("image_download_sha256", script)
        self.assertIn("extract_sha256", script)
        self.assertIn("https://downloads.raspberrypi.com/os_list_imagingutility_v4.json", script)
        self.assertIn("https://downloads.raspberrypi.com/raspios_lite_arm64/", script)
        self.assertIn("--max-filesize", script)
        self.assertIn("SECRET_PATH_RE", script)
        self.assertIn("git archive", script)
        self.assertIn("--partscan", script)
        self.assertNotIn("http://", blob)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", blob)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", blob)
        self.assertNotRegex(blob, r"ssh-(rsa|ed25519) AAAA")
        # The only Wi-Fi secret is the opt-in --wifi-from-env placeholder, filled from the environment.
        self.assertNotRegex(blob, r"(?i)\b(pass(word)?|psk|secret)\b\s*[:=]\s*\S+")
        self.assertIn('"$esc_ssid" "$esc_psk" > ', script)
        self.assertNotRegex(script, r"printf[^\n]*GS_WIFI_PSK[^\n]*>&2")
        self.assertNotIn("wpa_supplicant", blob)
        self.assertNotIn("userconf.txt", blob)
        self.assertNotIn("nmcli", script)
        self.assertNotRegex(parts[1], r"nmcli (?!connection up grok-station-wifi)")
        self.assertNotRegex(blob, r"grok-station-[0-9A-F]{4}")
        self.assertNotIn("hearth-", blob)

    def test_firstboot_unit_content(self):
        unit = open(UNIT, encoding="utf-8").read()
        boot = open(FIRSTBOOT, encoding="utf-8").read()
        script = open(SCRIPT, encoding="utf-8").read()
        self.assertIn("ConditionPathExists=/var/lib/grok-station-firstboot\n", unit)
        self.assertIn("ExecStart=/usr/local/src/grok-station/tools/build-image/firstboot.sh\n", unit)
        self.assertIn("After=network-online.target userconfig.service\n", unit)
        self.assertIn("Wants=network-online.target\n", unit)
        self.assertIn("Type=oneshot\n", unit)
        self.assertIn("WantedBy=multi-user.target\n", unit)
        self.assertIn("TimeoutStartSec=90min\n", unit)
        self.assertNotIn("User=", unit)
        self.assertNotIn("Restart=", unit)
        self.assertIn('sh "$SRC/install.sh" --no-reboot\n', boot)
        self.assertIn("GROK_STATION_NONINTERACTIVE", boot)
        self.assertIn("hotspot_loop", boot)
        self.assertIn("systemctl reboot", boot)
        self.assertLess(boot.index('sh "$SRC/install.sh" --no-reboot'), boot.index('rm -f -- "$STAMP"'))
        self.assertLess(boot.index('rm -f -- "$STAMP"'), boot.index("systemctl reboot"))
        self.assertIn("/var/lib/grok-station-firstboot", script)
        self.assertIn("multi-user.target.wants", script)
        self.assertIn("ln -s ../grok-station-firstboot.service", script)
        install = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        self.assertNotIn("grok-station-firstboot", install)

    def test_selects_newest_official_lite_arm64(self):
        sept = (
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
            "raspios_lite_arm64-2025-09-01/2025-09-01-raspios-trixie-arm64-lite.img.xz"
        )
        doc = {
            "os_list": [
                {
                    "name": "Raspberry Pi OS (64-bit)",
                    "url": "https://downloads.raspberrypi.com/raspios_arm64/images/raspios_arm64-2025-10-01/desktop.img.xz",
                    "image_download_sha256": H_OTHER,
                    "extract_sha256": H_EXT,
                    "release_date": "2026-01-01",
                },
                {
                    "name": "Raspberry Pi OS Lite (32-bit)",
                    "url": "https://downloads.raspberrypi.com/raspios_lite_armhf/images/x/lite.img.xz",
                    "image_download_sha256": H_OTHER,
                    "extract_sha256": H_EXT,
                    "release_date": "2026-02-01",
                },
                {
                    "name": "Other",
                    "subitems": [
                        lite(OLD_URL, H_OLD, H_EXT_OLD, "2024-11-19"),
                        lite(sept, H_OLD, H_EXT_OLD, "2025-9-01"),
                        lite(NEW_URL, H_NEW, H_EXT, "2025-10-01"),
                        lite(
                            "https://evil.example/raspios_lite_arm64/images/x/evil-arm64-lite.img.xz",
                            H_OTHER, H_EXT, "2026-06-01",
                        ),
                        {
                            "name": "Raspberry Pi OS Lite (64-bit)",
                            "url": NEW_URL,
                            "image_download_sha256": "abcd",
                            "extract_sha256": H_EXT,
                            "release_date": "2026-07-01",
                        },
                    ],
                },
            ]
        }
        proc = self.select(doc)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("url=" + NEW_URL + "\n", proc.stdout)
        self.assertIn("image_download_sha256=" + H_NEW + "\n", proc.stdout)
        self.assertIn("extract_sha256=" + H_EXT + "\n", proc.stdout)
        self.assertNotIn("evil.example", proc.stdout)
        self.assertNotIn(OLD_URL, proc.stdout)
        self.assertNotIn("armhf", proc.stdout)
        self.assertNotIn(sept, proc.stdout)

    def test_select_rejects_ambiguous_empty_and_off_list(self):
        alt = (
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
            "raspios_lite_arm64-2025-10-01/2025-10-01-raspios-trixie-arm64-lite-other.img.xz"
        )
        proc = self.select({"os_list": [lite(NEW_URL, H_NEW, H_EXT, "2025-10-01"), lite(alt, H_OLD, H_EXT_OLD, "2025-10-01")]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ambiguous", proc.stderr)
        proc = self.select({"os_list": []})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no Raspberry Pi OS Lite", proc.stderr)
        proc = self.run_script("--select-only", "/no/such/os-list.json")
        self.assertNotEqual(proc.returncode, 0)

    def test_select_allows_missing_download_hash(self):
        # The live OS list (Oct 2026) publishes extract_sha256 only; the
        # .img.xz.sha256 sibling covers the download in that case.
        url = (
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
            "raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz"
        )
        entry = {"name": "Raspberry Pi OS Lite (64-bit)", "url": url,
                 "extract_sha256": H_EXT, "release_date": "2026-10-06"}
        doc = {"os_list": [{"name": "Raspberry Pi OS (other)", "subitems": [entry]}]}
        proc = self.select(doc)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("url=" + url + "\n", proc.stdout)
        self.assertIn("image_download_sha256=\n", proc.stdout)
        bad = dict(entry, image_download_sha256="nothex")
        self.assertNotEqual(self.select({"os_list": [bad]}).returncode, 0)
        script = open(SCRIPT, encoding="utf-8").read()
        self.assertIn("no published download checksum", script)

    def test_firstboot_restores_git_clone(self):
        fb = open(FIRSTBOOT, encoding="utf-8").read()
        self.assertLess(fb.index("install.sh"), fb.index("git -C"))
        self.assertIn("fetch --quiet origin main", fb)
        self.assertLess(fb.index("reset --quiet --mixed"), fb.index('rm -f -- "$STAMP"'))

    def test_url_allowlist(self):
        proc = self.run_script("--check-image-url", NEW_URL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")
        rejected = [
            "http://downloads.raspberrypi.com/raspios_lite_arm64/images/a/a-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com.evil/raspios_lite_arm64/images/a/a-arm64-lite.img.xz",
            "https://evil.example/raspios_lite_arm64/images/a/a-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/../x-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com/raspios_arm64/images/a/desktop-arm64-lite.img.xz",
            "https://user:pass@downloads.raspberrypi.com/raspios_lite_arm64/images/a/a-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com/raspios_lite_armhf/images/a/a-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/a/b/c-arm64-lite.img.xz",
            "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-10-01/desktop.img.xz",
            NEW_URL + "?dl=1",
            "https://downloads.raspberrypi.com/os_list_imagingutility_v4.json",
        ]
        for url in rejected:
            proc = self.run_script("--check-image-url", url)
            self.assertNotEqual(proc.returncode, 0, url)
        proc = self.run_script("--check-url", "https://downloads.raspberrypi.com/os_list_imagingutility_v4.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_parse_published_checksum_file(self):
        with tempfile.TemporaryDirectory() as d:
            only = os.path.join(d, "only.sha256")
            with open(only, "w", encoding="utf-8") as fh:
                fh.write(H_NEW + "\n")
            proc = self.run_script("--parse-sums", only, "image.img.xz")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), H_NEW)

            named = os.path.join(d, "named.sha256")
            base = "2025-10-01-raspios-trixie-arm64-lite.img.xz"
            with open(named, "w", encoding="utf-8") as fh:
                fh.write("%s  %s\n" % (H_NEW.upper(), base))
            proc = self.run_script("--parse-sums", named, base)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), H_NEW)

            sums = os.path.join(d, "SHA256SUMS")
            with open(sums, "w", encoding="utf-8") as fh:
                fh.write(H_OLD + "  other.img.xz\n" + H_NEW + " *" + base + "\n")
            proc = self.run_script("--parse-sums", sums, base)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), H_NEW)

            clash = os.path.join(d, "clash.sha256")
            with open(clash, "w", encoding="utf-8") as fh:
                fh.write(H_NEW + "  image.img.xz\n" + H_OLD + "  image.img.xz\n")
            proc = self.run_script("--parse-sums", clash, "image.img.xz")
            self.assertNotEqual(proc.returncode, 0)

            missing = os.path.join(d, "missing.sha256")
            with open(missing, "w", encoding="utf-8") as fh:
                fh.write(H_OLD + "  other.img.xz\n")
            proc = self.run_script("--parse-sums", missing, "image.img.xz")
            self.assertNotEqual(proc.returncode, 0)

    def test_help_and_usage(self):
        proc = self.run_script("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("docs/sd-image.md", proc.stdout)
        proc = self.run_script("--not-a-flag")
        self.assertEqual(proc.returncode, 2)
        proc = self.run_script("--dry-run", "--select-only", "os-list.json")
        self.assertEqual(proc.returncode, 2)

    def test_docs(self):
        doc = open(os.path.join(ROOT, "docs", "sd-image.md"), encoding="utf-8").read()
        self.assertIn("## Validation on the Pi", doc)
        self.assertIn("--dry-run", doc)
        self.assertIn("downloads.raspberrypi.com", doc)
        self.assertIn("sha256", doc.lower())
        self.assertIn("losetup", doc)
        self.assertIn("hotspot", doc.lower())
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        self.assertIn("docs/sd-image.md", readme)


if __name__ == "__main__":
    unittest.main()


class ImageInstallParity(unittest.TestCase):
    """An image-built station must get every install.sh fix by running install.sh itself."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.fb = open(FIRSTBOOT, encoding="utf-8").read()
        self.install = open(os.path.join(root, "install.sh"), encoding="utf-8").read()
        self.boot = open(os.path.join(root, "bootstrap.sh"), encoding="utf-8").read()
        self.build = open(SCRIPT, encoding="utf-8").read()

    def test_firstboot_runs_install_sh_and_nothing_of_its_own(self):
        self.assertIn('sh "$SRC/install.sh" --no-reboot', self.fb)
        for own in ("apt-get", "pip ", "useradd", "systemctl enable", "amixer", "powersave",
                    "journald", "/opt/grok-station", "/etc/grok-station"):
            self.assertNotIn(own, self.fb, own)

    def test_build_does_not_bake_install_steps(self):
        for own in ("/opt/grok-station", "/etc/grok-station", "apt-get", "chroot", "powersave"):
            self.assertNotIn(own, self.build, own)

    def test_install_sh_has_every_package_bootstrap_installs(self):
        pk = re.search(r"st_packages\(\) \{(.*?)\n\}", self.install, re.S).group(1)
        for line in re.findall(r"apt-get install[^;}]*", self.boot):
            for name in re.findall(r"\b([a-z0-9][a-z0-9.+-]+)\b", line.split("--no-install-recommends", 1)[-1]):
                self.assertIn(" " + name, pk, name)
