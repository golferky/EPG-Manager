#!/usr/bin/env python3
"""Give every Plex TV show folder a year:  Show  ->  Show (1964).  Same tool as
plex_movie_years.py, pointed at the TV Shows folder (plex_tv_path in the recorder config).

    python3 ~/EPG-Manager/mac/plex_tv_years.py --dry-run
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_movie_years  # noqa: E402

if __name__ == '__main__':
    sys.exit(plex_movie_years.main(['--tv'] + sys.argv[1:]))
