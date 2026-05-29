#!/bin/bash
# Wrapper for launchd / manual runs.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
exec /usr/bin/python3 update_showtimes.py >> logs/update.log 2>&1
