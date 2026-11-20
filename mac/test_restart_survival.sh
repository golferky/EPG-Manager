#!/bin/bash
# Checks that a process started in its own session survives "launchctl kickstart -k" of the launchd job that
# started it.  That is exactly what a recording's ffmpeg must do for a recorder restart not to cut it short.
#
#   ~/EPG-Manager/mac/test_restart_survival.sh             test the way the recorder starts ffmpeg
#   ~/EPG-Manager/mac/test_restart_survival.sh --abandon   the same, with launchd's AbandonProcessGroup set
#
# It touches only its own throw-away job (com.epg.survival-test) and removes everything it made.
# It never touches the recorder.  It takes about 15 seconds.
LABEL="com.epg.survival-test"
UIDN="$(id -u)"
DOMAIN="gui/$UIDN"
ABANDON=0
[ "$1" = "--abandon" ] && ABANDON=1
WORK="$(mktemp -d)"
PLIST="$WORK/$LABEL.plist"
PIDS="$WORK/child.pids"
PY="$(command -v python3 || echo /usr/bin/python3)"

cleanup() {
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
  if [ -f "$PIDS" ]; then
    while read -r pid; do kill "$pid" >/dev/null 2>&1; done < "$PIDS"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/parent.py" <<'PY'
import os, subprocess, time
child = subprocess.Popen(["/bin/sleep", "600"], start_new_session=True, stdin=subprocess.DEVNULL)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "child.pids"), "a") as handle:
    handle.write(str(child.pid) + "\n")
time.sleep(600)
PY

EXTRA=""
[ "$ABANDON" = 1 ] && EXTRA="<key>AbandonProcessGroup</key><true/>"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$WORK/parent.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  $EXTRA
</dict></plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
launchctl bootstrap "$DOMAIN" "$PLIST" || { echo "could not start the test job"; exit 1; }

for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$PIDS" ] && break
  sleep 1
done
[ -s "$PIDS" ] || { echo "the test job never started its process"; exit 1; }
FIRST="$(head -1 "$PIDS")"
kill -0 "$FIRST" 2>/dev/null || { echo "the test process was not running before the restart"; exit 1; }
echo "test process $FIRST is running; restarting its parent job the way the recorder is restarted ..."

launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
sleep 4

if kill -0 "$FIRST" 2>/dev/null; then
  echo "PASS: process $FIRST survived the restart of its parent."
  echo "A recording's ffmpeg will keep running through a recorder restart."
  exit 0
fi
echo "FAIL: process $FIRST was stopped by the restart."
if [ "$ABANDON" = 0 ]; then
  echo "Run this again with --abandon. If that passes, the recorder's launchd job needs AbandonProcessGroup set"
  echo "(tell me and I will walk you through it)."
else
  echo "Even with AbandonProcessGroup the process was stopped, so recordings cannot survive a restart on this Mac."
  echo "Tell me and I will look for another way."
fi
exit 1
