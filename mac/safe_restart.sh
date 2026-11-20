#!/bin/bash
# Restart the recorder safely.  Run on the Mac mini:
#   ~/EPG-Manager/mac/safe_restart.sh            (waits if it has to, then restarts)
#   ~/EPG-Manager/mac/safe_restart.sh --pull     (also git pull first)
#   ~/EPG-Manager/mac/safe_restart.sh --check    (just say whether a restart is safe right now)
#   ~/EPG-Manager/mac/safe_restart.sh --timeout 7200   (give up after 2 hours, exit 2)
#
# A recorder at version SURVIVE_MIN_AGENT or later keeps its recordings going through a restart: each ffmpeg
# runs in its own session and the new recorder takes it over.  For such a recorder a restart is safe whenever no
# recording is being converted or filed and none starts within a couple of minutes, EVEN WHILE RECORDINGS ARE
# RUNNING.  An older recorder stops its recordings when it is restarted, so for that one the script waits until
# nothing is recording or finishing and nothing starts within 90 seconds (EPG_SOON_SECONDS to change).
# An ffmpeg you started by hand (a test recording, a conversion) never holds a restart up.
# If the server cannot be reached the state is unknown, so it keeps waiting.
# When it has to wait it says what for and when a quiet window looks likely.
SURVIVE_MIN_AGENT="v20261120b"
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
LABEL="com.epg.recording-agent"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"

# The process ids of ffmpeg processes started by the recorder (descendants of its process), one per line.
# With "survive" = 1 an ffmpeg that is writing a .ts file is a recording, which a restart does not disturb,
# so only the others (a conversion, a damage check) are listed.
recorder_ffmpeg_pids() {
  local survive="${1:-0}" roots
  roots="$(pgrep -f recording_agent.py 2>/dev/null | tr '\n' ' ')"
  [ -n "$roots" ] || return 0
  ps -A -o pid=,ppid=,stat=,args= 2>/dev/null | awk -v roots="$roots" -v survive="$survive" '
    BEGIN { n = split(roots, r, " "); for (i = 1; i <= n; i++) isroot[r[i]] = 1 }
    { parent[$1] = $2; state[$1] = $3; name[$1] = $4; last[$1] = $NF }
    END {
      for (p in name) {
        if (name[p] !~ /ffmpeg/ || state[p] ~ /^Z/) continue      # not ffmpeg, or already finished (zombie)
        if (survive == 1 && last[p] ~ /[.]ts$/) continue          # a recording: it keeps running through a restart
        q = p; depth = 0
        while ((q in parent) && q > 1 && depth < 30) {
          if (q in isroot) { print p; break }
          q = parent[q]; depth++
        }
      }
    }'
}

# Say which ffmpeg that is: its process id, how long it has run and its output file (any address hidden).
describe_recorder_ffmpeg() {
  local pid line out
  for pid in $(recorder_ffmpeg_pids "${1:-0}"); do
    line="$(ps -o etime=,args= -p "$pid" 2>/dev/null | sed -E 's#https?://[^ ]+#<address>#g')"
    out="${line##* }"
    printf 'ffmpeg %s (running %s, writing %s) ' "$pid" "$(printf '%s' "$line" | awk '{print $1}')" "$out"
  done
}

# Prints one line: "BUSY|why a restart is NOT safe right now" or "IDLE|a note (may be empty)".
assess() {
  local body survives=0 proc="" detail rc
  body="$(curl -fsS -m 15 "$SERVER/epg-web/api/recordings/overview" 2>/dev/null)" \
    || { echo "BUSY|dashboard server not reachable (state unknown)"; return 0; }
  survives="$(printf '%s' "$body" | "$PY" -c '
import sys, json
try:
    v = (json.load(sys.stdin).get("agent") or {}).get("version") or ""
except Exception:
    v = ""
print(1 if v >= sys.argv[1] else 0)
' "$SURVIVE_MIN_AGENT" 2>/dev/null)"
  [ "$survives" = 1 ] || survives=0
  if [ -n "$(recorder_ffmpeg_pids "$survives")" ]; then
    proc="the recorder has an ffmpeg running: $(describe_recorder_ffmpeg "$survives")"
  fi
  detail="$(printf '%s' "$body" | SURVIVES="$survives" "$PY" -c '
import sys, json, time, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable answer from the server"); sys.exit(0)
if not d.get("ok"):
    print("server says not ok"); sys.exit(0)
survives = os.environ.get("SURVIVES") == "1"
now = d.get("now", time.time())
clock = lambda ts: time.strftime("%H:%M", time.localtime(ts))
upcoming = sorted(d.get("upcoming", []), key=lambda e: e["start_ts"])
def window(free_at):
    # The next quiet window: after the running recordings end (plus about ten minutes to finish them) and
    # between bookings with a gap of at least four minutes.  Only a projection: bookings can change.
    free = free_at + 600
    for e in upcoming:
        if e["start_ts"] - free >= 240:
            return "; a quiet window looks likely from about %s to %s" % (clock(free), clock(e["start_ts"]))
        free = max(free, e.get("stop_ts", e["start_ts"] + 3600) + 600)
    return "; after that nothing else is booked, so it should be quiet from about %s" % clock(free)
recording = d.get("recording") or []
if recording and not survives:
    ends = [e.get("stop_ts", now) for e in recording]
    print("recording now: " + ", ".join("%s (until %s)" % (e["title"], clock(e.get("stop_ts", now))) for e in recording)
          + window(max(ends))); sys.exit(0)
if d.get("finishing"):
    print("still finishing: " + ", ".join(e["title"] for e in d["finishing"]) + window(now - 600)); sys.exit(0)
limit = int(os.environ.get("EPG_SOON_SECONDS", "150" if survives else "90"))
soon = [e for e in upcoming if e["start_ts"] - now < limit]
if soon:
    print("starts within %d s: " % limit + ", ".join(e["title"] for e in soon)); sys.exit(0)
if recording and survives:
    print("safe now: %d recording(s) will keep running through the restart: %s"
          % (len(recording), ", ".join(e["title"] for e in recording)))
sys.exit(1)
')"; rc=$?
  if [ "$rc" = 0 ]; then
    [ -n "$proc" ] && detail="$detail [$proc]"
    echo "BUSY|$detail"; return 0
  fi
  if [ -n "$proc" ]; then echo "BUSY|$proc"; return 0; fi
  echo "IDLE|$detail"
}

MODE=restart; PULL=0; TIMEOUT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --pull) PULL=1 ;;
    --timeout) shift; TIMEOUT="${1:-0}" ;;
    *) echo "unknown option: $1"; exit 64 ;;
  esac
  shift
done

if [ "$MODE" = check ]; then
  verdict="$(assess)"
  case "$verdict" in
    BUSY\|*) echo "BUSY: ${verdict#BUSY|}"; exit 1 ;;
    *) note="${verdict#IDLE|}"; echo "IDLE${note:+: $note}"; exit 0 ;;
  esac
fi

started=$(date +%s)
idle_in_a_row=0
while true; do
  verdict="$(assess)"
  case "$verdict" in
    BUSY\|*)
      idle_in_a_row=0
      echo "$(date +%H:%M:%S) waiting: ${verdict#BUSY|}"
      if [ "$TIMEOUT" -gt 0 ] && [ $(( $(date +%s) - started )) -ge "$TIMEOUT" ]; then
        echo "gave up after ${TIMEOUT}s: a restart was never safe; nothing was restarted"
        exit 2
      fi
      sleep 30
      ;;
    *)
      note="${verdict#IDLE|}"
      idle_in_a_row=$((idle_in_a_row + 1))
      echo "$(date +%H:%M:%S) safe to restart ($idle_in_a_row/3)${note:+: $note}"
      [ "$idle_in_a_row" -ge 3 ] && break
      sleep 20
      ;;
  esac
done

if [ "$PULL" = 1 ]; then
  (cd "$DIR" && git pull --ff-only) || { echo "git pull failed - not restarting"; exit 1; }
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "recorder restarted"
