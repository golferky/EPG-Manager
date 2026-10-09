#!/bin/zsh
# Called by launchd each morning. Flask performs the Schedule Direct fetch,
# guide import, and active-series rescan so credentials remain private.

set -u

server_url="${EPG_SERVER_URL:-http://127.0.0.1:5001}"
endpoint="${server_url%/}/epg-web/api/fetch-sd"

print "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Starting scheduled 21-day Schedule Direct refresh"
/usr/bin/curl --fail --silent --show-error \
  --retry 3 --retry-delay 30 --connect-timeout 15 --max-time 900 \
  -X POST -H 'Content-Type: application/json' --data '{"days":21}' "$endpoint"
curl_exit=$?

if [[ $curl_exit -eq 0 ]]; then
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Schedule Direct refresh started; the EPG server will import it in the background"
else
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Scheduled guide refresh failed (curl $curl_exit)" >&2
fi
exit $curl_exit
