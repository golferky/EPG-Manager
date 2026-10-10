#!/bin/zsh
# Called by launchd each morning. Keep both guide sources current: the
# provider guide supplies channels that Schedule Direct does not carry, then
# Schedule Direct supplies its longer (21-day) listings.  The Flask server
# performs the imports and active-series rescan so credentials remain private.

set -u

server_url="${EPG_SERVER_URL:-http://127.0.0.1:5001}"
provider_endpoint="${server_url%/}/epg-web/api/fetch-guide"
sd_endpoint="${server_url%/}/epg-web/api/fetch-sd"

print "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Starting scheduled provider guide refresh"
/usr/bin/curl --fail --silent --show-error \
  --retry 3 --retry-delay 30 --connect-timeout 15 --max-time 900 \
  -X POST "$provider_endpoint"
provider_exit=$?

if [[ $provider_exit -eq 0 ]]; then
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Provider guide refresh complete"
else
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Provider guide refresh failed (curl $provider_exit)" >&2
fi

print "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Starting scheduled 21-day Schedule Direct refresh"
/usr/bin/curl --fail --silent --show-error \
  --retry 3 --retry-delay 30 --connect-timeout 15 --max-time 900 \
  -X POST -H 'Content-Type: application/json' --data '{"days":21}' "$sd_endpoint"
sd_exit=$?

if [[ $sd_exit -eq 0 ]]; then
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Schedule Direct refresh started; the EPG server will import it in the background"
else
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Schedule Direct refresh failed (curl $sd_exit)" >&2
fi

# A transient provider failure must not prevent the longer Schedule Direct
# refresh, but preserve a failure if either source could not be refreshed.
if [[ $provider_exit -ne 0 ]]; then
  exit $provider_exit
fi
exit $sd_exit
