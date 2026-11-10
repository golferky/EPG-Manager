#!/bin/bash
# Run on the Mac mini BEFORE a QNAP firmware update:  ~/EPG-Manager/mac/firmware_before.sh
# 1. makes sure nothing is recording or about to record (next 30 minutes)
# 2. backs up the server files and the boot hook on the QNAP (one password prompt)
# Prints READY when it is safe to start the update.
QNAP="${EPG_QNAP:-GaryAdmin@192.168.1.176}"
QDIR="${EPG_QNAP_DIR:-/share/CACHEDEV1_DATA/EPG-Manager}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/2 Is the recorder idle? (nothing recording, nothing in the next 30 minutes)"
if ! EPG_SOON_SECONDS=1800 "$HERE/safe_restart.sh" --check; then
  echo "NOT READY: wait until the recorder is idle, then run this again."
  exit 1
fi

echo "== 2/2 Backing up on the QNAP ($QNAP) - it may ask for the password"
ssh "$QNAP" "QDIR='$QDIR' sh -s" <<'REMOTE'
set -e
cd "$QDIR"
B="backup_prefirmware_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$B"
for f in server.py sd_guide.py sd_token.json start_epg_server.sh start.sh config.json; do
  [ -f "$f" ] && cp -p "$f" "$B/"
done
cp -p /etc/config/autorun.sh "$B/autorun.sh" 2>/dev/null || echo "WARNING: could not copy /etc/config/autorun.sh"
echo "Backed up into $QDIR/$B:"
ls -l "$B"
echo "--- /etc/config/autorun.sh now ---"
cat /etc/config/autorun.sh 2>/dev/null || echo "(missing)"
echo "--- server version ---"
grep -m1 '^VERSION' server.py
REMOTE
rc=$?
if [ $rc -ne 0 ]; then echo "NOT READY: the backup on the QNAP failed (exit $rc)."; exit 1; fi
echo
echo "READY. In QTS also check Control Panel > System > Hardware: the option to run user"
echo "defined processes at startup must be ON (the boot hook needs it)."
echo "Then start the firmware update. Afterwards run: ~/EPG-Manager/mac/firmware_after.sh"
