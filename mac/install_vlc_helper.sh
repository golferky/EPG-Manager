#!/bin/bash
# Installs "EPG VLC", a tiny app that registers the epgvlc:// link type so the
# EPG Manager dashboard can open videos in VLC with one click.  Run once per Mac.
set -e

APP="$HOME/Applications/EPG VLC.app"
SRC="$(mktemp -t epgvlc).applescript"

if [ ! -d "/Applications/VLC.app" ]; then
  echo "VLC is not in /Applications. Install VLC first, then run this again." >&2
  exit 1
fi

cat > "$SRC" <<'EOF'
on open location theURL
	set prefix to "epgvlc://play/"
	if theURL starts with prefix then
		set encoded to text ((length of prefix) + 1) thru -1 of theURL
		set cmd to "u=$(printf '%s' " & quoted form of encoded & " | /usr/bin/perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg'); printf '#EXTM3U\\n%s\\n' \"$u\" > /tmp/epg-play.m3u; /usr/bin/open -a VLC /tmp/epg-play.m3u"
		do shell script cmd
	end if
end open location
EOF

mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" "$SRC"
rm -f "$SRC"

PL="$APP/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Delete :CFBundleIdentifier" "$PL" 2>/dev/null || true
$PB -c "Add :CFBundleIdentifier string com.gary.epgvlc" "$PL"
$PB -c "Delete :LSUIElement" "$PL" 2>/dev/null || true
$PB -c "Add :LSUIElement bool true" "$PL"
$PB -c "Delete :CFBundleURLTypes" "$PL" 2>/dev/null || true
$PB -c "Add :CFBundleURLTypes array" "$PL"
$PB -c "Add :CFBundleURLTypes:0 dict" "$PL"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLName string EPG VLC" "$PL"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PL"
$PB -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string epgvlc" "$PL"

# Editing Info.plist breaks the signature the compiler added; sign it again.
codesign --force --deep --sign - "$APP"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

echo
echo "Installed: $APP"
echo "Test it (VLC should open and play):"
echo "  open 'epgvlc://play/http%3A%2F%2F192.168.1.176%3A5001%2Fepg-web%2Fapi%2Fplay%2Fproxy%3Fchannel_id%3D247.andygriffithshow'"
