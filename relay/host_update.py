"""Host-side relay update. The container never runs docker.

`update-run` reads the request file (version only), fetches the manifest again,
and pulls and pins images by the digests in that manifest. The request cannot
supply a digest. Health is GET /healthz on loopback; failure rolls back through
release.Updater. `check-update` does not fetch when autocheck is off.
"""

import http.client
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

import release

PROJECT = "grok-relay"
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8790
HEALTH_PATH = "/healthz"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+\Z")


def settings_path(paths):
    return os.path.join(paths.config, "update.json")


def installed_path(paths):
    return os.path.join(paths.state, "installed.json")


def request_path(paths):
    return os.path.join(paths.state, "update-request.json")


def override_path(paths):
    return os.path.join(paths.config, "compose.override.yaml")


def compose_path(paths):
    return os.path.join(paths.config, "compose.yaml")


def busy_path(paths):
    return os.path.join(paths.state, "voice-busy")


def read_settings(system, paths):
    """Missing file, or a file without a boolean autocheck, is off."""
    raw = system.read_bytes(settings_path(paths))
    if not raw:
        return {"autocheck": False}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"autocheck": False}
    if not isinstance(doc, dict) or "autocheck" not in doc:
        return {"autocheck": False}
    if not isinstance(doc.get("autocheck"), bool):
        raise ValueError("autocheck")
    return {"autocheck": doc["autocheck"]}


def read_installed(system, paths):
    relay_v, station_v = "v0.0.0", "v0.0.0"
    raw = system.read_bytes(installed_path(paths))
    if not raw:
        return relay_v, station_v
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return relay_v, station_v
    if not isinstance(doc, dict):
        return relay_v, station_v
    if isinstance(doc.get("relay"), str) and _VERSION.fullmatch(doc["relay"]):
        relay_v = doc["relay"]
    elif isinstance(doc.get("version"), str) and _VERSION.fullmatch(doc["version"]):
        relay_v = doc["version"]
    if isinstance(doc.get("station"), str) and _VERSION.fullmatch(doc["station"]):
        station_v = doc["station"]
    return relay_v, station_v


def service_name(image):
    """Manifest image `caddy` is the compose service `tls`."""
    if image == "caddy":
        return "tls"
    return image


def compose_override(manifest):
    """YAML whose image digests come only from manifest['components']['relay']['images']."""
    images = manifest["components"]["relay"]["images"]
    if not isinstance(images, dict) or not images:
        raise ValueError("images")
    lines = ["services:"]
    for name in sorted(images):
        digest = images[name]
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise ValueError("image")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("digest")
        lines.append("  " + service_name(name) + ":")
        lines.append('    image: "' + name + "@" + digest + '"')
    lines.append("")
    return "\n".join(lines)


def image_refs(manifest):
    images = manifest["components"]["relay"]["images"]
    return [name + "@" + images[name] for name in sorted(images)]


def loopback_health(port=HEALTH_PORT, path=HEALTH_PATH):
    """GET http://127.0.0.1 only. No redirects, no other host."""
    conn = http.client.HTTPConnection(HEALTH_HOST, port, timeout=3)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read(256)
        return response.status
    except Exception:
        return 0
    finally:
        conn.close()


def _wait_health(get_status, sleep, mono, limit=60):
    """Poll until 200, the deadline, or a clock that does not move. Never loops forever."""
    start = mono()
    for _ in range(int(limit) + 2):
        try:
            code = get_status()
        except Exception:
            code = 0
        if code == 200:
            return True
        if mono() - start >= limit:
            return False
        before = mono()
        sleep(1)
        if mono() <= before:
            return False
    return False


class DockerOps:
    """stage / switch / health / rollback for release.Updater. Docker argv only, no shell."""

    def __init__(self, system, paths, manifest, installed_relay, health_get=None, now=None,
                 sleep=None, mono=None):
        self.system = system
        self.paths = paths
        self.manifest = manifest
        self.installed_relay = installed_relay
        self.health_get = health_get or loopback_health
        self.now = now
        self.sleep = sleep or time.sleep
        self.mono = mono or time.monotonic
        self._prev = None
        self._had_override = False
        self._compose_rc = 0

    def clock(self):
        if isinstance(self.now, datetime):
            if self.now.tzinfo is None:
                return self.now.replace(tzinfo=timezone.utc)
            return self.now
        if self.now is None:
            return datetime.now(timezone.utc)
        return self.now

    def is_busy(self):
        raw = self.system.read_bytes(busy_path(self.paths))
        return bool(raw and raw.strip() not in (b"", b"0"))

    def disk_free_mb(self):
        fn = getattr(self.system, "disk_free_mb", None)
        if callable(fn):
            return fn()
        for path in (self.paths.state, self.paths.config, "/"):
            if not path:
                continue
            try:
                return shutil.disk_usage(path).free / (1024 * 1024)
            except OSError:
                continue
        return 0

    def stage(self, component, spec):
        if component != "relay" or not isinstance(spec, dict):
            return {"slot": "", "digests": {}}
        images = spec.get("images")
        if not isinstance(images, dict):
            return {"slot": "", "digests": {}}
        for name in sorted(images):
            digest = images[name]
            result = self.system.run(["docker", "pull", name + "@" + digest])
            if result.returncode != 0:
                return {"slot": "pull-failed", "digests": {}}
        return {"slot": "relay-" + self.manifest["version"], "digests": dict(images)}

    def switch(self, component, slot):
        del slot
        if component != "relay":
            return
        path = override_path(self.paths)
        previous = self.system.read_bytes(path)
        self._had_override = previous is not None
        self._prev = previous
        text = compose_override(self.manifest)
        self.system.write_file(path, text.encode("utf-8"))
        argv = ["docker", "compose", "-p", PROJECT, "-f", compose_path(self.paths), "-f", path, "up", "-d"]
        self._compose_rc = self.system.run(argv).returncode

    def health(self, component):
        del component
        if self._compose_rc != 0:
            return {"ok": False, "gate": "healthz"}
        ok = _wait_health(self.health_get, self.sleep, self.mono, 60)
        return {"ok": ok, "gate": "healthz"}

    def rollback(self, component):
        del component
        path = override_path(self.paths)
        compose = compose_path(self.paths)
        if self._had_override and self._prev is not None:
            self.system.write_file(path, self._prev)
            argv = ["docker", "compose", "-p", PROJECT, "-f", compose, "-f", path, "up", "-d"]
        else:
            self.system.remove_file(path)
            argv = ["docker", "compose", "-p", PROJECT, "-f", compose, "up", "-d"]
        self.system.run(argv)
        return self.installed_relay


def apply_relay_only(updater, installed_relay, manifest, now):
    """Run the relay component through Updater, including its rollback."""
    updater._events = []
    updater._lines = []
    updater._marked_now = set()
    updater._rolled = False
    updater._reason = None
    release._validate_release_shape(manifest)
    release._validate_release_time(manifest, release._coerce_now(now))
    release._version_tuple(installed_relay)
    if not updater._wait_idle():
        return updater._finish(False, "deferred", "busy")
    try:
        free = float(updater.ops.disk_free_mb())
    except (TypeError, ValueError):
        free = 0
    if free < release.DISK_MIN_MB:
        updater._emit("-", "update-aborted", "disk")
        return updater._finish(False, "aborted", "disk")
    ok = updater._component("relay", installed_relay, manifest)
    if not ok:
        status = "rolled-back" if updater._rolled else "aborted"
        return updater._finish(False, status, updater._reason)
    return updater._finish(True, "applied", None)


def _write_installed(system, paths, manifest, station_v):
    doc = {
        "version": manifest["version"],
        "relay": manifest["version"],
        "station": station_v,
        "signed_tag": None,
        "compat": {
            "station_min_relay": manifest["compat"]["station_min_relay"],
            "relay_min_station": manifest["compat"]["relay_min_station"],
        },
    }
    system.write_file(installed_path(paths), (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8"))


def run_check(system, paths, fetcher, audit, now=None):
    """Scheduled check. Autocheck off (the default) does not fetch."""
    try:
        settings = read_settings(system, paths)
    except ValueError:
        print("check failed")
        return 1
    if settings.get("autocheck") is not True:
        print("autocheck off")
        return 0
    installed, _station = read_installed(system, paths)
    try:
        body = release.fetch_manifest(fetcher)
        result = release.scheduled_check(settings, installed, body, now=now)
    except release.ManifestError as err:
        if err.reason == "expired":
            audit("update-check-expired", "-")
            print(release.EXPIRED_TITLE)
            print(release.EXPIRED_MESSAGE)
            return 0
        print("check failed")
        return 1
    if result.reason == "expired":
        audit("update-check-expired", "-")
        print(release.EXPIRED_TITLE)
        print(release.EXPIRED_MESSAGE)
        return 0
    if result.update and isinstance(result.version, str):
        print("update " + result.version)
        return 0
    print("current")
    return 0


def run_update(system, paths, fetcher, audit, health_get=None, now=None, sleep=None, mono=None):
    """Pick up one request. Digests used for pull and the override come from the manifest."""
    raw = system.read_bytes(request_path(paths))
    if not raw:
        return 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        audit("update-rejected", "bad-args")
        system.remove_file(request_path(paths))
        return 1
    installed, station_v = read_installed(system, paths)
    # Shape only. A bad request must not fetch (same rule as the control route).
    stub = {"version": installed, "min_version_floor": "v0.0.0"}
    try:
        probe = release.validate_update_request(text, installed, stub)
    except release.ManifestError:
        audit("update-rejected", "bad-args")
        system.remove_file(request_path(paths))
        return 1
    if not probe.ok and probe.reason == "bad-args":
        audit("update-rejected", "bad-args")
        system.remove_file(request_path(paths))
        return 1
    try:
        body = release.fetch_manifest(fetcher)
        manifest = release.parse_manifest(body, now=now)
    except release.ManifestError as err:
        audit("update-rejected", err.reason if err.reason in ("expired", "future", "bad-format", "fetch-failed") else "fetch-failed")
        if err.reason in ("expired", "bad-format", "future"):
            system.remove_file(request_path(paths))
        return 1
    decision = release.validate_update_request(text, installed, manifest)
    if not decision.ok:
        audit("update-rejected", decision.reason or "bad-args")
        system.remove_file(request_path(paths))
        return 1
    ops = DockerOps(
        system, paths, manifest, installed,
        health_get=health_get, now=now, sleep=sleep, mono=mono,
    )
    updater = release.Updater(ops)
    try:
        result = apply_relay_only(updater, installed, manifest, now)
    except Exception:
        audit("update-rejected", "fetch-failed")
        return 1
    if result.status == "deferred":
        audit("update-deferred", "busy")
        return 0
    if result.ok:
        _write_installed(system, paths, manifest, station_v)
        system.remove_file(request_path(paths))
        audit("update-applied", manifest["version"])
        return 0
    if result.reason == "health":
        system.remove_file(request_path(paths))
        audit("update-rejected", "health")
        return 1
    if result.reason == "disk":
        audit("update-rejected", "disk")
        return 1
    audit("update-rejected", result.reason if isinstance(result.reason, str) and re.fullmatch(r"[a-z0-9-]{1,32}", result.reason) else "fetch-failed")
    return 1
