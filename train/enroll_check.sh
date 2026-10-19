#!/bin/bash
# enroll_check.sh <inbox session dir> [N=10]: queue a reduced-clip check of a finished session. The watcher trains it
# like any session (nice 10, one at a time, watchdog) on only the first N clips, evaluated on the full run's held-out
# clips, and never publishes it. Result: enroll/enroll.log + enroll/jobs/<phrase>-<session>c<N>/result.json.
set -euo pipefail
cd "$(dirname "$0")"
S=${1%/}; N=${2:-10}; m=$(basename "$(dirname "$S")"); ses=$(basename "$S")
H=enroll/jobs/$m-$ses/heldout.txt
[ -f "$S/DONE" ] && [ -f "$H" ] || { echo "full run of $S not done yet"; exit 1; }
C=$(dirname "$S")/${ses}c$N; mkdir -p "$C"
for f in "$S"/[0-9][0-9].wav; do ln -sf "$PWD/$f" "$C/"; done
python3 -c "import json,sys;j=json.load(open(sys.argv[1]));j.update(check_n=int(sys.argv[2]),held_from=sys.argv[3]);json.dump(j,open(sys.argv[4],'w'))" \
  "$S/READY" "$N" "$H" "$C/READY.tmp" && mv "$C/READY.tmp" "$C/READY"
echo "queued $C"
