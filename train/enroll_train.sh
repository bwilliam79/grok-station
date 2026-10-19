#!/bin/bash
# enroll_train.sh <inbox session dir>: one real-voice training job (started by enroll_watch.sh under nice 10 + caffeinate).
# Uses the training host's openWakeWord setup (venv, gen.py, mkcfg.py, sweep.sh, data/). See docs/enroll.md.
set -euo pipefail
cd "$(dirname "$0")" && source venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES PYTORCH_ENABLE_MPS_FALLBACK=1 OMP_NUM_THREADS=6 OWW_NCPU=4 OWW_WORKERS=4 OWW_PREFETCH=8 GEN_THREADS=6
export OWW_BG_MIN_SNR=-3 OWW_BG_MAX_SNR=20
S=$1; m=$(basename "$(dirname "$S")"); ses=$(basename "$S"); tag=enr${ses//-/}
persona=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['persona'])" "$S/READY")
J=enroll/jobs/$m-$ses; mkdir -p "$J"
say(){ echo "$(date '+%F %T %Z') [$m $ses] $*" | tee -a enroll/enroll.log; }
say "JOB start (persona $persona, $(ls "$S"/[0-9][0-9].wav | wc -l | tr -d ' ') clips)"
grep -q "\"$m\"" phrases.py || { say "phrase $m missing from phrases.py"; exit 2; }
if [ ! -f runs/$m/.gen_done ]; then
  say "GEN synthetic set"
  for k in "pos_train 25000" "pos_test 1000" "neg_train 12000" "neg_test 1000"; do set -- $k
    GEN_DEVICE=mps GEN_BATCH=64 GEN_SRC=lib python gen.py $m $1 $2 >> "$J/gen.log" 2>&1; done
  for k in "pos_train 2500" "pos_test 250" "neg_train 1500" "neg_test 150"; do set -- $k
    GEN_SRC=stk GEN_TAG=stk0 python gen.py $m $1 $2 >> "$J/gen.log" 2>&1; done
  touch runs/$m/.gen_done; say "GEN done"
fi
if [ ! -f runs/$m/positive_features_train.npy ]; then
  say "FEATURES synthetic"; NEGW=3000 python mkcfg.py $m >/dev/null
  python openWakeWord/openwakeword/train.py --training_config configs/$m.yml --augment_clips > "$J/feat.log" 2>&1
fi
say "FEATURES real"; python enroll_feats.py "$S" "$J" "${ROUNDS:-200}" >> "$J/feat_real.log" 2>&1
python enroll_merge.py runs/$m "$J" runs/${m}_$tag >> "$J/feat_real.log" 2>&1
rm -f "$J/real_features_train.npy"
say "TRAIN"; FEAT=${m}_$tag ./sweep.sh $m $tag:100000:3000:64:128:128 >> "$J/train.log" 2>&1
say "EVAL real held-out"
set +e; python enroll_eval.py $m runs_sw/${m}__$tag.onnx eval_sw/${m}__$tag/report.json "$J" enroll/published "$persona" >> "$J/eval.log" 2>&1; rc=$?; set -e
rm -rf runs/${m}_$tag  # merged features are big; the model + report stay
say "RESULT $(tail -2 "$J/eval.log" | head -1)"
[ $rc = 0 ] && { touch "$S/DONE"; say "JOB DONE published"; } || { touch "$S/DONE"; say "JOB DONE below the gate, not published"; }
