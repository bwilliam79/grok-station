#!/bin/bash
# enroll_watch.sh: run every 60 s by launchd. Starts one enroll_train.sh job per READY inbox session (one at a time)
# and is the job watchdog: a job that died without DONE, or whose log has been idle 30 min, is killed and restarted
# once, then marked FAILED. Everything is logged to enroll/enroll.log; status in enroll/status.json.
cd "$(dirname "$0")"; mkdir -p enroll/inbox enroll/published enroll/jobs
LOG=enroll/enroll.log; LOCK=enroll/.job
say(){ echo "$(date '+%F %T %Z') [watch] $*" >> $LOG; }
start(){ s=$1; n=$(( $(cat "$s/.tries" 2>/dev/null || echo 0) + 1 )); echo $n > "$s/.tries"
  nohup $(command -v caffeinate >/dev/null && echo caffeinate -i) nice -n 10 ./enroll_train.sh "$s" >> enroll/jobs/stdout.log 2>&1 &
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
  # oldest READY first (FIFO), so the station's "Nth in line" matches the order jobs actually run
  for r in $(ls -1tr enroll/inbox/hey_*/*/READY 2>/dev/null); do [ -f "$r" ] || continue; s=$(dirname "$r")
    [ -f "$s/DONE" ] || [ -f "$s/FAILED" ] || { start "$s"; break; }; done
fi
python3 - <<'P' 2>/dev/null
import glob, json, os
STATES = ("recording", "processing", "published", "failed_quality", "failed")
def one(d):
    ses = d.split("inbox/", 1)[-1]
    op = os.path.join(d, ".outcome.json")
    if os.path.isfile(op):
        try:
            j = json.load(open(op))
        except Exception:
            j = {}
        st = j.get("state") if isinstance(j, dict) else None
        if st in STATES:
            row = {"session": ses, "state": st}
            if st == "failed_quality" and isinstance(j.get("scores"), dict):
                row["scores"] = j["scores"]
            return row
    if os.path.exists(d + "/FAILED"):
        return {"session": ses, "state": "failed"}
    if os.path.exists(d + "/DONE"):
        phrase, stamp = ses.split("/", 1) if "/" in ses else ("", os.path.basename(d))
        if not phrase:
            phrase = os.path.basename(os.path.dirname(d))
        blob = ""
        try:
            blob = open("enroll/enroll.log", errors="replace").read()
        except OSError:
            blob = ""
        lines = [ln for ln in blob.splitlines() if f"[{phrase} {stamp}]" in ln or f"[{os.path.basename(os.path.dirname(d))} {os.path.basename(d)}]" in ln]
        text = "\n".join(lines)
        if "below the gate" in text:
            st = "failed_quality"
        elif any("JOB DONE published" in ln and "not published" not in ln for ln in lines):
            st = "published"
        else:
            st = "failed"  # never "done", including a check run ("not published")
        return {"session": ses, "state": st}
    lock = ""
    try:
        lock = open("enroll/.job").read()
    except OSError:
        lock = ""
    if d in lock or os.path.exists(d + "/READY"):
        return {"session": ses, "state": "processing"}
    return {"session": ses, "state": "recording"}
rows = [one(d) for d in sorted(glob.glob("enroll/inbox/hey_*/*")) if os.path.isdir(d)]
try:
    published = sorted(os.listdir("enroll/published"))
except OSError:
    published = []
json.dump({"sessions": rows[-20:], "published": published}, open("enroll/status.json", "w"), indent=1)
P
