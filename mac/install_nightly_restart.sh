#!/bin/bash
# Install (or remove) the 4 AM "restart the recorder if it is idle" job on the Mac mini.
#   ~/EPG-Manager/mac/install_nightly_restart.sh            install
#   ~/EPG-Manager/mac/install_nightly_restart.sh --remove   remove
# Change the time with:  HOUR=4 MINUTE=0 ~/EPG-Manager/mac/install_nightly_restart.sh
LABEL="com.epg.nightly-restart"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOUR="${HOUR:-4}"; MINUTE="${MINUTE:-0}"
UID_NOW="$(id -u)"

launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null
if [ "$1" = "--remove" ]; then
  rm -f "$PLIST" && echo "removed the nightly restart"
  exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/mac/nightly_restart.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>$HOUR</integer>
    <key>Minute</key>
    <integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$DIR/nightly-restart.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/nightly-restart.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF
launchctl bootstrap "gui/$UID_NOW" "$PLIST" && echo "installed: the recorder restarts if idle at $(printf '%02d:%02d' "$HOUR" "$MINUTE") each night (log: $DIR/nightly-restart.log)"
