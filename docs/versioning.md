# Versions

`VERSION` at the repo root is the station's version, `MAJOR.MINOR.PATCH`.

- **Patch** — fixes only. Safe to install over the previous version.
- **Minor** — new features. Still installs over the previous version.
- **Major** — breaking changes. Expect a reinstall, not a quiet update.

Bump `VERSION` in every change that ships to `main`. The number only goes up.

Before shipping, run:

```bash
python3 tools/check-version.py
```

It fails unless `HEAD`'s `VERSION` is strictly newer than `HEAD~1`'s. The first commit that adds `VERSION` passes. The updater also refuses to install `origin/main` when that version is equal to or lower than the one already on the station (equal means up to date). A station whose installed version is still a short commit id can move onto the first semantic version.

The installed copy is `/opt/grok-station/VERSION`, written by the installer from this file. The Updates page compares it with `origin/main`'s `VERSION` after a fetch. A short commit id may be shown beside the version as a detail. The installer log stays on the station; the page shows one line if an update fails.

## 0.2.0

Minor feature: a cached "Asking <name>…" clip plays as soon as a handoff is known. See [handoff.md](handoff.md).

## 0.3.0

Minor feature: named station timers (up to eight, countdown rings, an amber ringing screen). See [timers.md](timers.md).

## 0.4.0

Minor: the display can dim from a light sensor, a clock schedule, or sunrise and sunset computed on the station. Settings are on the Display page. See [dimming.md](dimming.md).
