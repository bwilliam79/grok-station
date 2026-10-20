#!/usr/bin/env python3
"""Build an unsigned release manifest and check its shape with release.py.

issued_at is now (UTC). expires is 30 days later. There is no signature.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "relay"))
import release  # noqa: E402


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_now(text):
    if not text:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return release._coerce_now(text).replace(microsecond=0)


def build(version, floor, tarball_path, images, station_min_relay, relay_min_station, now):
    parsed = {}
    for item in images:
        if not isinstance(item, str) or item.count("=") != 1:
            raise release.ManifestError("bad-format", "image")
        name, digest = item.split("=", 1)
        if name in parsed:
            raise release.ManifestError("bad-format", "image")
        parsed[name] = digest
    name = os.path.basename(tarball_path)
    doc = {
        "version": version,
        "min_version_floor": floor,
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": {
            "station": {"tarball": name, "sha256": sha256_file(tarball_path)},
            "relay": {"images": parsed},
        },
        "compat": {
            "station_min_relay": station_min_relay,
            "relay_min_station": relay_min_station,
        },
    }
    # Shape and the 30-day window. No signature is produced or required.
    release.parse_manifest(doc, now=now)
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write an unsigned release manifest")
    parser.add_argument("--version", required=True)
    parser.add_argument("--floor", required=True)
    parser.add_argument("--station-tarball", required=True)
    parser.add_argument("--image", action="append", required=True)
    parser.add_argument("--station-min-relay", required=True)
    parser.add_argument("--relay-min-station", required=True)
    parser.add_argument("--output", default="-")
    parser.add_argument("--now", default=None)
    args = parser.parse_args(argv)
    try:
        now = parse_now(args.now)
        doc = build(
            args.version, args.floor, args.station_tarball, args.image,
            args.station_min_relay, args.relay_min_station, now,
        )
    except release.ManifestError as err:
        print(err.reason, file=sys.stderr)
        return 1
    except OSError:
        print("bad-format", file=sys.stderr)
        return 1
    text = json.dumps(doc, sort_keys=True, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
