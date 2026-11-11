#!/bin/bash
# Restart the recorder onto the code on disk, at a quiet moment.  Run by launchd at 4 AM
# (see install_nightly_restart.sh).  It never interrupts a recording: it waits for the
# recorder to be idle, and gives up at the end of the window (default 2 hours).
#   - already running the version on disk: nothing to do
#   - otherwise waits for idle, then restarts
# It does not pull new code; pull it yourself, and the next quiet night puts it live.
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)"
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
WINDOW="${EPG_NIGHTLY_WINDOW_SECONDS:-7200}"
PY="$(command -v python3 || echo /usr/bin/python3)"

disk="$(sed -n 's/^AGENT_VERSION *= *"\(.*\)".*/\1/p' "$DIR/recording_agent.py" | head -1)"
running="$(curl -fsS -m 15 "$SERVER/epg-web/api/recordings/overview" 2>/dev/null | "$PY" -c '
import sys, json
try:
    a = json.load(sys.stdin).get("agent") or {}
    print(a.get("version") or "")
except Exception:
    print("")
')"
echo "$(date '+%F %T') nightly check: code on disk $disk, recorder running ${running:-unknown}"
if [ -n "$disk" ] && [ "$disk" = "$running" ]; then
  echo "$(date '+%F %T') already up to date; nothing to restart"
  exit 0
fi
"$HERE/safe_restart.sh" --timeout "$WINDOW"
rc=$?
echo "$(date '+%F %T') nightly restart finished with code $rc (0=restarted, 2=never idle, will try tomorrow night)"
exit $rc
