#!/bin/zsh
# Deploy a dashboard change from the Mac mini to the QNAP in one go:
#   ~/EPG-Manager/mac/deploy_dashboard.sh
# 1. pulls the latest code, 2. copies server.py to the QNAP, 3. restarts the dashboard there,
# 4. asks the RUNNING dashboard for its version and checks it matches (a stale server fails loudly).
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
# start_epg_server.sh stops the old server, starts the new one detached and checks it answers.
ssh "$QNAP" "cd $QDIR && ./start_epg_server.sh"

# The version in the file is not the version that is running: ask the running dashboard.
WANT=$(grep -m1 '^VERSION' server.py | sed 's/.*"\(.*\)".*/\1/')
HOST="${QNAP#*@}"
RUNNING=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  RUNNING=$(curl -s -m 5 "http://$HOST:5001/epg-web" | grep -oE 'v20[0-9]{6}[a-z]' | head -1)
  [ "$RUNNING" = "$WANT" ] && break
  sleep 3
done
if [ "$RUNNING" = "$WANT" ]; then
  echo "== Done. The dashboard is running $RUNNING. Give it a minute to load the guide before you check the Wanted tab."
else
  echo "== WARNING: expected $WANT but the dashboard reports '${RUNNING:-nothing}'. The restart did not take; run ~/EPG-Manager/mac/deploy_dashboard.sh again or restart it by hand on the QNAP." >&2
  exit 1
fi
