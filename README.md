# EPG Manager

A local Flask web application for browsing electronic program guide data, managing channels and recommendations, scheduling recordings, and converting recorded transport streams.

## Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` for recording and conversion features
- VLC for local playback features
- A Schedules Direct account and/or an XMLTV-compatible provider

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config.example.json epg_config.json
```

Edit `epg_config.json` with local paths and credentials. This file is ignored by Git and must never be committed.

By default, runtime configuration and state live in `~/epg`. Set
`EPG_BASE_DIR` before starting the server to use a different directory.

Run the application:

```bash
python3 server.py
```

Then open <http://localhost:5001/epg-web>.

## DuckDNS updater

`duckdns_update.sh` reads its settings from environment variables so the token is not stored in Git:

```bash
export DUCKDNS_DOMAIN='your-domain'
export DUCKDNS_TOKEN='your-token'
./duckdns_update.sh
```

Runtime databases, guide XML, logs, tokens, schedules, watchlists, and local configuration are excluded by `.gitignore`.
