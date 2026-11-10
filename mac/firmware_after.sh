#!/bin/bash
# Run on the Mac mini AFTER the QNAP firmware update has started:
#   ~/EPG-Manager/mac/firmware_after.sh
# Waits for the QNAP to come back, then checks that everything is working and
# prints PASS / FAIL / WARN for each part.  It changes nothing.
QNAP="${EPG_QNAP:-GaryAdmin@192.168.1.176}"
QDIR="${EPG_QNAP_DIR:-/share/CACHEDEV1_DATA/EPG-Manager}"
SERVER="${EPG_SERVER:-http://192.168.1.176:5001}"
PY="$(command -v python3 || echo /usr/bin/python3)"
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails + 1)); }
warn() { echo "WARN  $1"; }

echo "== Waiting for the dashboard to answer at $SERVER (up to 25 minutes)"
up=0
for i in $(seq 1 100); do
  if curl -fsS -m 10 "$SERVER/epg-web/api/recordings/overview" >/dev/null 2>&1; then up=1; break; fi
  printf '.'; sleep 15
done
echo
if [ $up -eq 1 ]; then pass "dashboard server is answering"; else
  fail "dashboard server did not come back"
  echo "  On the QNAP try: cd $QDIR && ./start_epg_server.sh   (then run this script again)"
fi

if [ $up -eq 1 ]; then
  ver="$(curl -fsS -m 15 "$SERVER/epg-web" 2>/dev/null | grep -o 'v20[0-9]\{6\}[a-z]' | head -1)"
  [ -n "$ver" ] && pass "dashboard version $ver" || warn "could not read the dashboard version"

  echo "== Waiting for the recorder to check in (up to 3 minutes)"
  agent=""
  for i in $(seq 1 18); do
    agent="$(curl -fsS -m 15 "$SERVER/epg-web/api/recordings/overview" 2>/dev/null | "$PY" -c '
import sys, json
try:
    a = json.load(sys.stdin).get("agent")
except Exception:
    a = None
if a and a.get("seconds_ago", 999) < 90:
    print("%s (checked in %d s ago)" % (a.get("version") or "version unknown", a["seconds_ago"]))
')"
    [ -n "$agent" ] && break
    sleep 10
  done
  [ -n "$agent" ] && pass "recorder $agent" || fail "recorder has not checked in (is the Mac mini recorder running?)"
fi

echo "== Checking on the QNAP ($QNAP) - it may ask for the password"
ssh "$QNAP" "QDIR='$QDIR' sh -s" <<'REMOTE'
cd "$QDIR" || { echo "FAIL  cannot open $QDIR"; exit 0; }
B="$(ls -dt backup_prefirmware_* 2>/dev/null | head -1)"
if [ -z "$B" ]; then
  echo "WARN  no backup folder found (did you run firmware_before.sh?)"
elif [ ! -f "$B/autorun.sh" ]; then
  echo "WARN  no autorun.sh in $B to compare with"
elif cmp -s /etc/config/autorun.sh "$B/autorun.sh"; then
  echo "PASS  boot hook /etc/config/autorun.sh is unchanged by the update"
else
  echo "FAIL  boot hook /etc/config/autorun.sh differs from the backup"
  echo "      restore it:  cp -p $QDIR/$B/autorun.sh /etc/config/autorun.sh"
fi
up="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; now="$(date +%s)"
if [ -n "$up" ] && [ -f boot.log ]; then
  m="$(stat -c %Y boot.log 2>/dev/null)"
  if [ -n "$m" ] && [ "$m" -ge $((now - up - 60)) ]; then
    echo "PASS  boot hook ran after the last reboot (boot.log was written)"
  else
    echo "FAIL  boot.log is older than the last reboot - the boot hook did not run"
    echo "      check QTS Control Panel > System > Hardware: run user defined processes at startup"
  fi
else
  echo "FAIL  no boot.log - the boot hook has not run since it was set up (or since the QNAP rebooted)"
  echo "      check QTS Control Panel > System > Hardware: run user defined processes at startup"
fi
[ -e /share/EPG-Manager ] && echo "PASS  shortcut /share/EPG-Manager exists" \
  || echo "WARN  shortcut /share/EPG-Manager is missing (scripts use the real path, so this is harmless)"
echo "--- last lines of epg_server.log ---"
tail -8 epg_server.log 2>/dev/null || echo "(no log)"
REMOTE
[ $? -eq 0 ] || fail "could not run the checks on the QNAP over ssh"

echo "== Checking the Plex share on this Mac"
if [ -d "/Volumes/Plex/TV Shows" ] && ls "/Volumes/Plex/TV Shows" >/dev/null 2>&1; then
  pass "/Volumes/Plex is mounted"
else
  fail "/Volumes/Plex is not mounted - reconnect the QNAP share before anything records"
fi

echo
if [ $fails -eq 0 ]; then echo "ALL GOOD."; else echo "$fails check(s) FAILED - see above."; fi
exit $fails
