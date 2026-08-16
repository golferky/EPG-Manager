#!/bin/zsh
# Called by launchd each morning.  Flask performs the fetch, guide import, and
# active-series rescan so credentials remain in the private runtime config.

set -u

server_url="${EPG_SERVER_URL:-http://127.0.0.1:5001}"
endpoint="${server_url%/}/epg-web/api/fetch-guide"

print "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Starting scheduled guide refresh"
/usr/bin/curl --fail --silent --show-error \
  --retry 3 --retry-delay 30 --connect-timeout 15 --max-time 900 \
  -X POST "$endpoint"
curl_exit=$?

if [[ $curl_exit -eq 0 ]]; then
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Scheduled guide refresh finished"
else
  print "\n[$(/bin/date '+%Y-%m-%d %H:%M:%S')] Scheduled guide refresh failed (curl $curl_exit)" >&2
fi
exit $curl_exit
