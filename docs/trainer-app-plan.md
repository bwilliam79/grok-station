# Trainer app plan (optional, not built)

Goal: custom wake words without training on the Pi. A small app on any Mac, Windows or Linux computer pulls the
station's recorded enroll clips, trains with openWakeWord, and pushes the finished model back.

## Station side (shipped in single-device)
- Enroll recording stays on the station: clips are saved to `/var/lib/grok-station/enroll/inbox/<phrase>/<session>/`.
- Models dropped into `/var/lib/grok-station/wakewords/` (`<phrase>.onnx` + `.json`, with the sha256 in the json)
  are picked up by the wake listener within 60 s.
- Future admin-page endpoints, all HTTPS:
  - `GET /api/trainer/sessions` lists the sessions that are ready.
  - `GET /api/trainer/clips/<phrase>/<session>.tar` downloads the clips.
  - `POST /api/trainer/model/<phrase>` uploads the onnx and json; the station verifies the sha256, the name
    pattern and the size before loading.
  - `POST /api/trainer/status` reports progress, which the admin page shows.

## Pairing the app
- The admin page shows a one-time code. The app trades it for a trainer token that is scoped to the trainer
  endpoints only, revocable, and stored as a hash on the station.
- The app pins the station's admin TLS certificate fingerprint on first use, and the admin page shows that
  fingerprint so it can be confirmed.
- LAN only, with no internet exposure.

## App
- Python package plus a small local web UI or CLI. Installers: a Homebrew/pipx path, a Windows zip with an
  embedded Python, and pipx on Linux.
- First run downloads the training data: about 21 GB (ACAV negative features 16 GB, background audio, validation
  features) plus piper voices (1.8 GB) and a venv of about 1.3 GB, so plan for ~27 GB free. It asks before
  downloading.
- Per job:
  1. Generate the synthetic set once per phrase, then cache it (~1 GB).
  2. Mix in the real clips (80/20 split).
  3. Train.
  4. Gate on recall ≥ 0.80 on the real held-out clips and ≤ 1.0 false wakes per hour.
  5. Push the model only if it passes.
- Measured on the current training host: 1.8 GB peak RAM and 5–6 min per job when the synthetic set is cached.
  On modest laptops expect 15–60 min (**est.**).
- GPU is optional: MPS on Apple, CUDA on Linux/Windows. Otherwise it runs on CPU.
- Reuses the `train/` scripts, ported off launchd/caffeinate to a cross-platform job runner.

## Open questions
- Should trainer pairing reuse the admin login or have its own token? Own token is proposed.
- Subsampling the negative features to ~4 GB to shrink the download needs testing against the gate.
- Who owns signing for the app installers? Unsigned for now, so the same the security reviewer conditions as station updates apply.
