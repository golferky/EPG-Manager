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

## Mac recording agent

EPG Manager supports two recording backends:

- `local` (default): Flask launches FFmpeg directly, preserving current behavior.
- `agent`: Flask stores durable jobs for a separate Mac worker to claim.

The agent mode is intended for running the web application and guide database on
a QNAP while keeping FFmpeg on a Mac. The server and agent communicate over an
authenticated HTTP API with job leases and heartbeats.

FFmpeg keeps running through a brief Flask restart. When the server comes back,
the next heartbeat renews the job lease and updates Schedule status.

### Agent setup

Generate a private token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Add the same token to the server's `epg_config.json`:

```json
{
  "recording_backend": "agent",
  "recording_agent_token": "generated-token"
}
```

Keep `recording_backend` set to `local` until the agent health check succeeds.
Copy `recording_agent.example.json` to the ignored `recording_agent.json`, then
set the server URL, token, provider credentials, local recording directory, and
mounted Plex path.

Test one poll without recording anything:

```bash
python recording_agent.py --config recording_agent.json --once
```

Run continuously:

```bash
python recording_agent.py --config recording_agent.json
```

`max_concurrent_recordings` defaults to 6. Each claimed job has its own FFmpeg
worker, so overlapping and back-to-back programs can record at the same time.

Before recording, the agent probes the incoming stream and any matching Plex
copy. It records when Plex has no copy or the incoming stream is materially
better. Completed MP4 files are copied as `.partial`, size-verified, and renamed
atomically. When the Plex share is unavailable, the job remains
`awaiting_transfer` and the local recording is retained.

The agent checks Plex again immediately before transfer, so a share that was
missing during preflight cannot cause a lower-quality duplicate to overwrite an
existing movie. The verified local MP4 is retained after a successful copy.

For paths below `/Volumes`, the agent verifies that the share root is a real
mount point. Set `plex_mount_marker` to the name of a marker file inside the
Plex movie directory if an additional identity check is desired.
