#!/bin/zsh
# Keep EPG Manager's QNAP SMB shares present after macOS sleep/reconnects.
# Credentials are supplied by the existing macOS Keychain SMB entries.

mount_share() {
  local share_name="$1"
  local share_url="$2"
  if /sbin/mount | /usr/bin/grep -q " on /Volumes/${share_name} "; then
    return 0
  fi
  /usr/bin/osascript -e "mount volume \"${share_url}\"" >/dev/null 2>&1 || true
}

mount_share "Plex" "smb://GarysNas._smb._tcp.local/Plex"
mount_share "EPG"  "smb://GarysNas._smb._tcp.local/EPG"
