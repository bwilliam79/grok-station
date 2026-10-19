# Training-host scripts for enroll mode

Copied into the openWakeWord training directory on the training host (next to gen.py, mkcfg.py, sweep.sh,
evaluate2.py, venv/ and data/). See ../docs/enroll.md.

- `enroll_watch.sh`: launchd job (every 60 s); starts one training job per ready session and is its watchdog.
- `enroll_train.sh <inbox session>`: synthetic set (generated once per phrase) + real clips → train → gate → publish.
- `enroll_feats.py`, `enroll_merge.py`, `enroll_eval.py`: real-clip features, merged positives, real held-out gate.
