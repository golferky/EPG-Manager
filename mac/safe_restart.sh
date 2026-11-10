#!/bin/bash
# Restart the recorder only when it is truly idle.  Run on the Mac mini:
#   ~/EPG-Manager/mac/safe_restart.sh            (waits, then restarts)
#   ~/EPG-Manager/mac/safe_restart.sh --pull     (also git pull first)
#   ~/EPG-Manager/mac/safe_restart.sh --check    (just say if it is idle)
# "Idle" means the dashboard server reports nothing recording or finishing,
# no recording starts in the next 3 minutes (EPG_SOON_SECONDS to change), and no
# ffmpeg is running here.
# If the server cannot be reached the state is unknown, so it keeps waiting.
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
LABEL="com.epg.recording-agent"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
export EPG_SOON_SECONDS="${EPG_SOON_SECONDS:-180}"

busy_reason() {
  if pgrep -x ffmpeg >/dev/null 2>&1; then echo "ffmpeg is running here"; return 0; fi
  local body
  body="$(curl -fsS -m 15 "$SERVER/epg-web/api/recordings/overview" 2>/dev/null)" \
    || { echo "dashboard server not reachable (state unknown)"; return 0; }
  printf '%s' "$body" | "$PY" -c '
import sys, json, time, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable answer from the server"); sys.exit(0)
if not d.get("ok"):
    print("server says not ok"); sys.exit(0)
if d.get("recording"):
    print("recording now: " + ", ".join(e["title"] for e in d["recording"])); sys.exit(0)
if d.get("finishing"):
    print("still finishing: " + ", ".join(e["title"] for e in d["finishing"])); sys.exit(0)
now = d.get("now", time.time())
limit = int(os.environ.get("EPG_SOON_SECONDS", "180"))
soon = [e for e in d.get("upcoming", []) if e["start_ts"] - now < limit]
if soon:
    print("starts within %d min: " % (limit // 60) + "" + ", ".join(e["title"] for e in soon)); sys.exit(0)
sys.exit(1)
' && return 0
  return 1
}

case "$1" in
  --check)
    if r="$(busy_reason)"; then echo "BUSY: $r"; exit 1; fi
    echo "IDLE"; exit 0 ;;
esac

idle_in_a_row=0
while true; do
  if r="$(busy_reason)"; then
    idle_in_a_row=0
    echo "$(date +%H:%M:%S) waiting: $r"
    sleep 30
  else
    idle_in_a_row=$((idle_in_a_row + 1))
    echo "$(date +%H:%M:%S) idle ($idle_in_a_row/3)"
    [ "$idle_in_a_row" -ge 3 ] && break
    sleep 20
  fi
done

if [ "$1" = "--pull" ]; then
  (cd "$DIR" && git pull --ff-only) || { echo "git pull failed - not restarting"; exit 1; }
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "recorder restarted"
