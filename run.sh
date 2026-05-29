#!/bin/bash
# Wrapper for launchd / manual runs.
cd "$(dirname "$0")" || exit 1
mkdir -p logs

/usr/bin/python3 update_showtimes.py >> logs/update.log 2>&1
status=$?

# Deploy to GitHub Pages on success.
[ $status -eq 0 ] && ./push_to_github.sh >> logs/update.log 2>&1

exit $status
