#!/bin/sh
set -eu

: "${DUCKDNS_DOMAIN:?Set DUCKDNS_DOMAIN in the environment}"
: "${DUCKDNS_TOKEN:?Set DUCKDNS_TOKEN in the environment}"

LOG_DIR="${EPG_LOG_DIR:-$HOME/epg/logs}"
mkdir -p "$LOG_DIR"

curl --fail --silent --show-error \
  --get 'https://www.duckdns.org/update' \
  --data-urlencode "domains=$DUCKDNS_DOMAIN" \
  --data-urlencode "token=$DUCKDNS_TOKEN" \
  --data-urlencode 'ip=' \
  --output "$LOG_DIR/duckdns.log"
