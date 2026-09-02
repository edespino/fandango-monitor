#!/bin/bash
# Regenerate the status page and push it to GitHub Pages.
#
# Run this instead of fandango_monitor.py from launchd if you want the
# published page kept current. Requires a git remote you can push to
# without a prompt (SSH key or a gh credential helper already set up).
set -euo pipefail

cd "$(dirname "$0")"

python3 fandango_monitor.py "$@"

if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -m "Update seat availability $(date '+%Y-%m-%d %H:%M')"
  git push
  echo "status page pushed"
else
  echo "status page unchanged"
fi
