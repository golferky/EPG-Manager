# EPG Manager: how to work here

Read this first. It records how the owner's machines are set up and how changes are deployed, so none of it has to be rediscovered after a long conversation is compacted.

## Machines
- **QNAP NAS** (192.168.1.176, ssh user `GaryAdmin`): runs the Flask dashboard. App dir `/share/CACHEDEV1_DATA/EPG-Manager`. LAN URL `http://192.168.1.176:5001/epg-web`. Log: `/share/CACHEDEV1_DATA/EPG-Manager/epg_server.log` (no timestamps; use `wc -l` and `grep -n` line numbers to tell new lines from old). Real guide DB: `/share/EPG/guide.db` (the `guide.db` inside the app dir is an empty stray file).
- **Mac mini** (192.168.1.208, user `garyscudder`): runs the recorder agent. Repo `~/EPG-Manager`, config `~/EPG-Manager/recording_agent.json`. Plex share is `/Volumes/Plex`, the QNAP `EPG` share is `/Volumes/EPG`.
- **MacBook** (192.168.1.189): browser only.
- The QNAP has **no git, no sqlite3, no python3 on PATH**. Use `/share/CACHEDEV1_DATA/.qpkg/Python3/opt/python3/bin/python3.12` for Python. Read the guide DB there with `sqlite3.connect('file:/share/EPG/guide.db?mode=ro', uri=True)`.
- **Never copy guide.db to the Mac** and never query it over the SMB share from the Mac (it fails to open). Keep it on the QNAP.

## Deploying a dashboard change (server.py)
1. Open a PR and merge it only when the user says so ("open the pull request", "merge it"; "open the pull request then merge it" means both). Before merging, merge `origin/main` into the branch. Squash merges leave version-line conflicts; keep the branch's newer `VERSION`.
2. Mac: `cd ~/EPG-Manager && git pull`
3. Mac: `scp ~/EPG-Manager/server.py ~/EPG-Manager/sd_guide.py ~/EPG-Manager/episode_match.py GaryAdmin@192.168.1.176:/share/CACHEDEV1_DATA/EPG-Manager/` (ALL THREE files: the dashboard calls sd_guide.py for every guide fetch; a change to it that is not copied does nothing)
4. QNAP: `cd /share/CACHEDEV1_DATA/EPG-Manager && ./start_epg_server.sh`, then `grep -m1 '^VERSION' server.py` to confirm the new version.
- Shortcut for steps 2 to 4, run on the Mac: `~/EPG-Manager/mac/deploy_dashboard.sh` (pulls, copies server.py and sd_guide.py, restarts the dashboard, then checks the RUNNING dashboard reports the new version; the version in the file alone proves nothing).
- A change to `recording_agent.py` only needs the Mac `git pull`, then `mac/safe_restart.sh`. Never restart the recorder while a recording is running.

## Standing rules
- Give one command at a time and say which machine it runs on. Code blocks hold only runnable commands (examples with placeholders go in plain text). The user's shell is zsh on the Mac and `sh` on the QNAP.
- Bump the dashboard `VERSION` in `server.py` (`vYYYYMMDDx`, today's real date, letter restarts at `a` each day) and `AGENT_VERSION` in `recording_agent.py` on every change to those files.
- Open a PR only when asked. Merge only when told.
- Never delete the user's files without explicit consent; give delete commands only after they say delete.
- Never print or ask for credentials or stream URLs (they contain the IPTV login). Hide addresses with `sed -E 's#https?://[^ ]+#<address>#g'` or write `<address>`. Do not mention the PrimeStreams expiry; the user renews it monthly. Keep OMDb/TMDb keys private.
- Keep iPhone/iPad Safari and Windows working. VLC is the default player.
- The dashboard's HTML/JS lives in a non-raw Python triple-quoted string, so use no backslashes in the JS. Validate with a Jinja render and `node --check` on the extracted scripts.
- FFmpeg runs with `-nostdin` and `stdin=DEVNULL` (otherwise it wrecks the user's Terminal).
- 24/7-channel recording is tabled; do not revisit unless asked.
- **Never put a partial or damaged movie into Plex**, even one that is only missing opening credits or a few minutes. Only complete, clean recordings go in `Movies`. Partials stay in `Incomplete Recordings` (or are deleted on request) and the film stays on Wanted so it is re-recorded.
- Tests: `python3 -m unittest discover -s tests` (needs `pip install flask`).

## Eaglecast on/off (subscription may lapse or be renewed)
- Status / switch off / switch on: `curl -s http://192.168.1.176:5001/epg-web/api/eaglecast`, and `-X POST -H 'Content-Type: application/json' -d '{"enabled": false}'` (or `true`) to the same URL. Off means new recordings use PrimeStreams only; the setting is `eaglecast_enabled` in the dashboard config. Turning it back on needs no other change.
- Report what could move, then move it: POST `{}` then `{"apply": true}` to `/epg-web/api/eaglecast/move-to-primestreams`. It switches the stored source of queued bookings PrimeStreams carries on their own channel, and for ones it does not carry it switches to the same airing, or failing that the same episode within six hours (an east/west feed), on another channel PrimeStreams does carry (`via` names it; poor, bad and foreign channels and already-booked airings or episodes are skipped); the rest are listed and left alone.
- Read-only cost report: `/epg-web/api/eaglecast-dependency`.
- End dates: set the Eaglecast date with POST `{"expires": "YYYY-MM-DD"}` to `/epg-web/api/eaglecast` (the dashboard also asks the provider account for it when it can, and that wins). Airings that run past it use PrimeStreams, and bookings past it move to PrimeStreams automatically every half hour where PrimeStreams carries the channel. Renewing: send the new date.
- Reminders: the dashboard shows a banner when Schedules Direct is within 30 days of its end date (Eaglecast within 7). Set the date with POST `{"key": "schedules_direct", "expires": "YYYY-MM-DD"}` to `/epg-web/api/reminders`. Schedules Direct currently ends 2027-05-05. PrimeStreams is deliberately not tracked: the owner renews it monthly.

## Guide filters
- **Hide non-English** (checkbox beside Hide quality duplicates, remembered in the browser, on by default; untick it to see everything again): drops whole channels, and everything on them, that are clearly not English. It judges the channel only, from the language Schedules Direct gives the station (kept in `channel_languages`, filled on the next guide fetch), the language words and labels in the name, non-Latin writing, and a short list of known foreign networks (`_is_non_english_channel`). A bilingual (en plus another) station stays.
- **Admin / Hide adult** (a small muted **Admin** button in the header; the adult checkbox, its count and the Adult only dropdown entry are NOT in the page until the tab has unlocked admin with the password, and a first press with no password set creates it; the server enforces the lock either way. Admin menu: Change password, Lock admin; a user name for future separate users is not built): hides adult channels (a name list: Brazzers, Playboy, Penthouse and so on; "Adult Swim" is not one) and any programme tagged "Adults only", in the grid and in search; adult channels are also never booked by automation. If an adult password is set, the SERVER hides adult content unless the browser session has entered it (a token header, 2 hours sliding, cleared when the tab closes or the server restarts). Only a salted PBKDF2 hash is stored (`adult_password_hash` in the dashboard config). Changing or removing the password needs the current one; 5 wrong tries pause attempts for 5 minutes. The password is typed only in the dashboard, never in chat or a command. The "All programming" dropdown also has **Non-English only** and **Adult only** views that show exactly what the two switches hide, and the checkboxes show how many channels each hides; the adult view is refused by the server (`adult_locked`) unless the password has been entered, and is never reopened by itself on page load. The owner has also asked about user accounts and admins; not built.

## Episode guesses (shows whose guide listings have no season/episode, such as every ANTENNA TV classic)
- First choice, exact: descriptions come from one shared source, so if the SAME text is on a numbered airing of that show on another channel (FETV, DTV323 ...), that is the episode: score 100%, `source: guide`, no guessing (`_guide_known_episode`; `/api/guide-descriptions` reports it as `episode` and `mac/name_episodes.py` uses it first). Only when there is no exact text does it fall back to wording similarity, which is a GUESS.
- The programme window matches each unnumbered airing's description to the series' episodes and shows `≈ S2E16 · Name (87%)` (confident) or `maybe S2E16 ...` (30%+), and a confident match marks the show as a series so Record series appears. It is a GUESS from wording, never written into the guide, and recordings are still filed unnamed in `TV Needing Episode Info` until you name them; nothing uses the guess at record time yet.
- Sources: TMDb (episode list, fetched in the background the first time a show's window opens, cached 24 h) plus OMDb plots (`/api/omdb-episodes`, fetched in the background into `omdb_episodes_cache.json`, at most 800 OMDb requests a day; the first window open for a show may need a second look 8 seconds later). The matching code is `episode_match.py`, shared with `mac/name_episodes.py` (`--source tmdb+omdb`, `--evaluate` to measure a source against episodes you already know). `episode_match.py` must be deployed with `server.py`.

## Stop-hook note
The "N unpushed commits" stop-hook warning is stale after a squash merge. Check `git ls-remote origin <branch>` against `git rev-parse HEAD`; if they match, nothing needs pushing.
