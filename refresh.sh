#!/bin/bash
#
# HoodScout daily refresh: pull -> verify -> render -> deploy.
#
# Run by launchd once a day (see com.hoodscout.daily.plist). launchd gives a job
# almost no environment, so everything here is an absolute path and nothing
# relies on an interactive shell profile.
#
# ⚠️ The Claude Artifact republish step was REMOVED 2026-08-24. It was a
# look-and-feel surface during build-out and was never the product; Cloudflare
# Pages is. It also stopped working silently: the artifact last updated on
# 08-20, and for the four days after that the step logged "republished to ..."
# every night purely because `claude -p` EXITED ZERO. It exits zero whether it
# publishes or explains that it cannot, and nothing looked at the output for the
# URL it was asked to return. The artifact had in fact been deleted.
#
# The lesson outlives the step: CHECK THE RESULT, NOT THE EXIT STATUS. The same
# shape hid a two-day ledger outage (`git push -q || true`) and a stalled poller
# (`launch_watch.py || true`) in this very repo.
#
# Usage:  ./refresh.sh            full run
#         ./refresh.sh --no-deploy    pull and render only

set -uo pipefail

PROJECT="/Users/raincityanalytics/projects/robinhood-chain-pulse"
PYTHON="/Users/raincityanalytics/anaconda3/envs/node/bin/python3"
SITE_URL="https://hoodscout.pages.dev"
LOG="$PROJECT/out/refresh.log"

DEPLOY=1
[ "${1:-}" = "--no-deploy" ] && DEPLOY=0

cd "$PROJECT" || exit 1
mkdir -p "$PROJECT/out"

# Keep the log from growing without bound -- last ~2000 lines is several weeks.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

say() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG"; }

say "=== refresh start ==="

# 1. Pull. The Seaport scan is the slow part (~3 min); everything else is API.
if ! "$PYTHON" chain_pulse.py >> "$LOG" 2>&1; then
    say "FAILED: chain_pulse.py — keeping the previous pulse.json and stopping"
    exit 1
fi
say "pulled pulse.json"

# 2. Cross-check against Dune. Non-fatal: a Dune outage or an exhausted credit
#    balance must not block the dashboard from updating. The audit strip is
#    simply rendered from whatever the last successful report was.
if "$PYTHON" verify_dune.py --days 10 >> "$LOG" 2>&1; then
    say "dune cross-check ok"
else
    say "WARN: verify_dune.py failed — rendering with the previous audit report"
fi

# 3. Render.
if ! "$PYTHON" build_dashboard.py --base-url "$SITE_URL" --public >> "$LOG" 2>&1; then
    say "FAILED: build_dashboard.py"
    exit 1
fi
say "rendered dashboard.html"

# 3b. Social card. This is the X link preview, so it is not optional cosmetics:
#     a link with no card gets scrolled past. Rendering it also acts as a smoke
#     test — the numbers are large enough that a bad one is obvious on sight,
#     which is how the under-ingested DAU bucket got caught.
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
    "$CHROME" --headless --disable-gpu --hide-scrollbars \
        --force-device-scale-factor=1 --virtual-time-budget=4000 \
        --window-size=1200,630 --screenshot="$PROJECT/out/site/card.png" \
        "file://$PROJECT/out/site/card.html" >> "$LOG" 2>&1 \
        && say "rendered card.png" \
        || say "WARN: card render failed — the previous card.png stays live"
else
    say "WARN: Chrome not found — card.png not regenerated"
fi

# 3c. Deploy the public site. Cloudflare Pages needs `wrangler login` once
#     (interactive browser OAuth) or CLOUDFLARE_API_TOKEN in the environment.
#     Until then this step no-ops loudly rather than failing the run.
if [ "$DEPLOY" = "1" ] && [ "${SKIP_DEPLOY:-0}" != "1" ]; then
    if npx --yes wrangler pages deploy "$PROJECT/out/site" \
        --project-name=hoodscout --branch=main --commit-dirty=true >> "$LOG" 2>&1; then
        say "deployed to $SITE_URL"
    else
        say "WARN: Pages deploy failed — run 'npx wrangler login' once, then retry"
    fi
fi

say "=== refresh done ==="
