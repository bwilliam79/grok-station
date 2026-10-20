#!/bin/bash
# enroll_train.sh <inbox session dir>: one real-voice training job (started by enroll_watch.sh under nice 10 + caffeinate).
# Uses the training host's openWakeWord setup (venv, gen.py, mkcfg.py, sweep.sh, data/). See docs/enroll.md.
set -euo pipefail
cd "$(dirname "$0")" && source venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES PYTORCH_ENABLE_MPS_FALLBACK=1 OMP_NUM_THREADS=6 OWW_NCPU=4 OWW_WORKERS=4 OWW_PREFETCH=8 GEN_THREADS=6
export OWW_BG_MIN_SNR=-3 OWW_BG_MAX_SNR=20
S=$1; m=$(basename "$(dirname "$S")"); ses=$(basename "$S"); tag=enr${ses//-/}
persona=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['persona'])" "$S/READY")
# reduced-clip check sessions (READY has check_n + held_from): train on fewer clips, never publish
eval "$(python3 -c "import json,sys,shlex;j=json.load(open(sys.argv[1]));print('CHECK_N=%s HELD_FROM=%s' % (shlex.quote(str(j.get('check_n',''))), shlex.quote(j.get('held_from',''))))" "$S/READY")"
PUB=enroll/published
J=enroll/jobs/$m-$ses; mkdir -p "$J"; [ -n "$CHECK_N" ] && PUB=$J/nopub && mkdir -p "$PUB"
say(){ echo "$(date '+%F %T %Z') [$m $ses] $*" | tee -a enroll/enroll.log; }
# status.json state for this session. Check runs are "failed" (never published). A gate miss is failed_quality.
write_outcome(){
  python3 - "$S" "$1" <<'PY'
import json, os, sys
s, state = sys.argv[1], sys.argv[2]
scores = {}
rp = os.path.join(s, "result.json")
if os.path.isfile(rp):
    try:
        j = json.load(open(rp))
    except Exception:
        j = {}
    if isinstance(j, dict):
        for k in ("recall_real", "fa_h"):
            if k in j and not isinstance(j[k], bool):
                scores[k] = j[k]
        scores["gate_recall"] = 0.8
        scores["gate_fa_h"] = 1.0
tmp = os.path.join(s, ".outcome.json.%s.tmp" % os.getpid())
with open(tmp, "w") as f:
    json.dump({"state": state, "scores": scores}, f)
os.replace(tmp, os.path.join(s, ".outcome.json"))
PY
}
on_err(){
  trap - ERR
  write_outcome failed || true
  touch "$S/FAILED" || true
  say "JOB FAILED" || true
  exit 1
}
trap on_err ERR
say "JOB start (persona $persona, $(ls "$S"/[0-9][0-9].wav | wc -l | tr -d ' ') clips)"
grep -q "\"$m\"" phrases.py || { say "phrase $m missing from phrases.py"; exit 2; }
if [ ! -f runs/$m/.gen_done ]; then
  say "GEN synthetic set"
  for k in "pos_train 25000" "pos_test 1000" "neg_train 12000" "neg_test 1000"; do set -- $k
    GEN_DEVICE=${GEN_DEVICE:-mps} GEN_BATCH=64 GEN_SRC=lib python gen.py $m $1 $2 >> "$J/gen.log" 2>&1; done
  for k in "pos_train 2500" "pos_test 250" "neg_train 1500" "neg_test 150"; do set -- $k
    GEN_SRC=stk GEN_TAG=stk0 python gen.py $m $1 $2 >> "$J/gen.log" 2>&1; done
  touch runs/$m/.gen_done; say "GEN done"
fi
if [ ! -f runs/$m/positive_features_train.npy ]; then
  say "FEATURES synthetic"; NEGW=3000 python mkcfg.py $m >/dev/null
  python openWakeWord/openwakeword/train.py --training_config configs/$m.yml --augment_clips > "$J/feat.log" 2>&1
fi
say "FEATURES real${CHECK_N:+ (check: first $CHECK_N clips, held-out from $HELD_FROM)}"
TRAIN_N=$CHECK_N HELD_FILE=$HELD_FROM python enroll_feats.py "$S" "$J" "${ROUNDS:-200}" >> "$J/feat_real.log" 2>&1
python enroll_merge.py runs/$m "$J" runs/${m}_$tag >> "$J/feat_real.log" 2>&1
rm -f "$J/real_features_train.npy"
say "TRAIN"; FEAT=${m}_$tag ./sweep.sh $m $tag:100000:3000:64:128:128 >> "$J/train.log" 2>&1
say "EVAL real held-out"
set +e; python enroll_eval.py $m runs_sw/${m}__$tag.onnx eval_sw/${m}__$tag/report.json "$J" "$PUB" "$persona" >> "$J/eval.log" 2>&1; rc=$?; set -e
rm -rf runs/${m}_$tag  # merged features are big; the model + report stay
say "RESULT $(tail -2 "$J/eval.log" | head -1)"
[ -f "$J/result.json" ] && cp "$J/result.json" "$S/.result.tmp" && mv "$S/.result.tmp" "$S/result.json"  # for GET .../status
if [ -n "$CHECK_N" ]; then
  write_outcome failed
  touch "$S/DONE"
  say "JOB DONE check ($([ $rc = 0 ] && echo pass || echo fail)), not published"
  exit 0
fi
if [ $rc = 0 ]; then
  write_outcome published
  touch "$S/DONE"
  say "JOB DONE published"
else
  write_outcome failed_quality
  touch "$S/DONE"
  say "JOB DONE below the gate, not published"
fi
