#!/bin/zsh
# Deploy a dashboard change from the Mac mini to the QNAP in one go:
#   ~/EPG-Manager/mac/deploy_dashboard.sh
# 1. pulls the latest code, 2. copies server.py to the QNAP, 3. restarts the dashboard there,
# 4. prints the version now in place so you can check it is the new one.
# If you do not use an SSH key it asks for the QNAP password twice (once to copy, once to restart).
# This does NOT touch the recorder: after a recording_agent.py change run mac/safe_restart.sh instead.

set -e
REPO="${EPG_REPO:-$HOME/EPG-Manager}"
QNAP="${EPG_QNAP:-GaryAdmin@192.168.1.176}"
QDIR="${EPG_QNAP_DIR:-/share/CACHEDEV1_DATA/EPG-Manager}"

cd "$REPO"
echo "== Pulling the latest code"
git pull
echo "== Local version:  $(grep -m1 '^VERSION' server.py)"

echo "== Copying server.py to the QNAP"
scp server.py "$QNAP:$QDIR/server.py"

echo "== Restarting the dashboard on the QNAP"
ssh "$QNAP" "cd $QDIR && (nohup ./start_epg_server.sh >/dev/null 2>&1 &); sleep 5; grep -m1 '^VERSION' server.py"
echo "== Done. Give the dashboard a minute to load the guide before you check the Wanted tab."
