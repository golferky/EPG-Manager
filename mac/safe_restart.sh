#!/bin/bash
# Restart the recorder only when it is truly idle.  Run on the Mac mini:
#   ~/EPG-Manager/mac/safe_restart.sh            (waits, then restarts)
#   ~/EPG-Manager/mac/safe_restart.sh --pull     (also git pull first)
#   ~/EPG-Manager/mac/safe_restart.sh --check    (just say if it is idle)
#   ~/EPG-Manager/mac/safe_restart.sh --timeout 7200   (give up after 2 hours, exit 2)
# "Idle" means the dashboard server reports nothing recording or finishing,
# no recording starts in the next 90 seconds (EPG_SOON_SECONDS to change), and no
# ffmpeg is running here.  A restart takes a few seconds, so that is enough margin.
# If the server cannot be reached the state is unknown, so it keeps waiting.
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
LABEL="com.epg.recording-agent"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
export EPG_SOON_SECONDS="${EPG_SOON_SECONDS:-90}"

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
    print("starts within %d s: " % limit + "" + ", ".join(e["title"] for e in soon)); sys.exit(0)
sys.exit(1)
' && return 0
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
