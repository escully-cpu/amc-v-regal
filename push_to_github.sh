#!/bin/bash
# Called by run.sh after each successful update.
# Pushes the latest index.html to GitHub Pages.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

git add index.html
git diff --cached --quiet && exit 0   # nothing changed, skip push

git commit -m "Daily update — $(date '+%Y-%m-%d %H:%M')"
git push origin main >> "$DIR/logs/push.log" 2>> "$DIR/logs/push_error.log"
