#!/bin/bash
# enroll_watch.sh: run every 60 s by launchd. Starts one enroll_train.sh job per READY inbox session (one at a time)
# and is the job watchdog: a job that died without DONE, or whose log has been idle 30 min, is killed and restarted
# once, then marked FAILED. Everything is logged to enroll/enroll.log; status in enroll/status.json.
cd "$(dirname "$0")"; mkdir -p enroll/inbox enroll/published enroll/jobs
LOG=enroll/enroll.log; LOCK=enroll/.job
say(){ echo "$(date '+%F %T %Z') [watch] $*" >> $LOG; }
start(){ s=$1; n=$(( $(cat "$s/.tries" 2>/dev/null || echo 0) + 1 )); echo $n > "$s/.tries"
  nohup caffeinate -i nice -n 10 ./enroll_train.sh "$s" >> enroll/jobs/stdout.log 2>&1 &
  echo "$! $s" > $LOCK; say "started job pid $! for $s (try $n)"; }
if [ -f $LOCK ]; then read pid s < $LOCK
  if [ -f "$s/DONE" ]; then rm -f $LOCK
  elif ! kill -0 $pid 2>/dev/null || [ -z "$(find enroll/jobs logs runs -maxdepth 3 -mmin -30 2>/dev/null | head -1)" ]; then
    kill -0 $pid 2>/dev/null && { pkill -TERM -P $pid; kill $pid; say "STALL $s: log idle >30 min, killed"; } || say "STALL $s: job died without DONE"
    rm -f $LOCK
    if [ "$(cat "$s/.tries" 2>/dev/null)" -ge 2 ]; then touch "$s/FAILED"; say "FAILED $s after 2 tries"; else start "$s"; fi
  fi
fi
if [ ! -f $LOCK ]; then
  for r in enroll/inbox/hey_*/*/READY; do [ -f "$r" ] || continue; s=$(dirname "$r")
    [ -f "$s/DONE" ] || [ -f "$s/FAILED" ] || { start "$s"; break; }; done
fi
python3 - <<'P' 2>/dev/null
import glob, json, os
s=[{"session": d.split("inbox/")[1], "state": "done" if os.path.exists(d+"/DONE") else "failed" if os.path.exists(d+"/FAILED") else "ready" if os.path.exists(d+"/READY") else "recording"} for d in sorted(glob.glob("enroll/inbox/hey_*/*"))]
json.dump({"sessions": s[-20:], "published": sorted(os.listdir("enroll/published"))}, open("enroll/status.json", "w"), indent=1)
P
