#!/bin/bash
# Restart the recorder only when it is truly idle.  Run on the Mac mini:
#   ~/EPG-Manager/mac/safe_restart.sh            (waits, then restarts)
#   ~/EPG-Manager/mac/safe_restart.sh --pull     (also git pull first)
#   ~/EPG-Manager/mac/safe_restart.sh --check    (just say if it is idle)
#   ~/EPG-Manager/mac/safe_restart.sh --timeout 7200   (give up after 2 hours, exit 2)
# "Idle" means the dashboard server reports nothing recording or finishing,
# no recording starts in the next 90 seconds (EPG_SOON_SECONDS to change), and the
# recorder has no ffmpeg of its own running.  An ffmpeg you started by hand (a test
# recording, a conversion) is not the recorder's and does not hold the restart up.
# A restart takes a few seconds, so that is enough margin.
# If the server cannot be reached the state is unknown, so it keeps waiting.
# When it is busy it says what it is waiting for and when a quiet window looks likely.
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
LABEL="com.epg.recording-agent"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
export EPG_SOON_SECONDS="${EPG_SOON_SECONDS:-90}"

# True when an ffmpeg started by the recorder (a descendant of its process) is running.
recorder_ffmpeg_running() {
  local roots
  roots="$(pgrep -f recording_agent.py 2>/dev/null | tr '\n' ' ')"
  [ -n "$roots" ] || return 1
  ps -A -o pid=,ppid=,stat=,comm= 2>/dev/null | awk -v roots="$roots" '
    BEGIN { n = split(roots, r, " "); for (i = 1; i <= n; i++) isroot[r[i]] = 1 }
    { parent[$1] = $2; state[$1] = $3; name[$1] = $4 }
    END {
      for (p in name) {
        if (name[p] !~ /ffmpeg/ || state[p] ~ /^Z/) continue      # not ffmpeg, or already finished (zombie)
        q = p; depth = 0
        while ((q in parent) && q > 1 && depth < 30) {
          if (q in isroot) exit 0
          q = parent[q]; depth++
        }
      }
      exit 1
    }'
}

busy_reason() {
  local proc="" body detail
  if recorder_ffmpeg_running; then proc="the recorder has an ffmpeg running (a recording or its finishing step)"; fi
  body="$(curl -fsS -m 15 "$SERVER/epg-web/api/recordings/overview" 2>/dev/null)" \
    || { echo "${proc:-dashboard server not reachable (state unknown)}"; return 0; }
  if detail="$(printf '%s' "$body" | "$PY" -c '
import sys, json, time, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable answer from the server"); sys.exit(0)
if not d.get("ok"):
    print("server says not ok"); sys.exit(0)
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
if d.get("recording"):
    ends = [e.get("stop_ts", now) for e in d["recording"]]
    print("recording now: " + ", ".join("%s (until %s)" % (e["title"], clock(e.get("stop_ts", now))) for e in d["recording"])
          + window(max(ends))); sys.exit(0)
if d.get("finishing"):
    print("still finishing: " + ", ".join(e["title"] for e in d["finishing"]) + window(now - 600)); sys.exit(0)
limit = int(os.environ.get("EPG_SOON_SECONDS", "180"))
soon = [e for e in upcoming if e["start_ts"] - now < limit]
if soon:
    print("starts within %d s: " % limit + "" + ", ".join(e["title"] for e in soon)); sys.exit(0)
sys.exit(1)
')"; then
    echo "$detail"; return 0
  fi
  if [ -n "$proc" ]; then echo "$proc"; return 0; fi
  return 1
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
  if r="$(busy_reason)"; then echo "BUSY: $r"; exit 1; fi
  echo "IDLE"; exit 0
fi

started=$(date +%s)
idle_in_a_row=0
while true; do
  if r="$(busy_reason)"; then
    idle_in_a_row=0
    echo "$(date +%H:%M:%S) waiting: $r"
    if [ "$TIMEOUT" -gt 0 ] && [ $(( $(date +%s) - started )) -ge "$TIMEOUT" ]; then
      echo "gave up after ${TIMEOUT}s: the recorder never went idle; nothing was restarted"
      exit 2
    fi
    sleep 30
  else
    idle_in_a_row=$((idle_in_a_row + 1))
    echo "$(date +%H:%M:%S) idle ($idle_in_a_row/3)"
    [ "$idle_in_a_row" -ge 3 ] && break
    sleep 20
  fi
done

if [ "$PULL" = 1 ]; then
  (cd "$DIR" && git pull --ff-only) || { echo "git pull failed - not restarting"; exit 1; }
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "recorder restarted"
