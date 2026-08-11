#!/bin/bash
#
# HoodScout daily refresh: pull -> verify -> render -> republish.
#
# Run by launchd once a day (see com.hoodscout.daily.plist). launchd gives a job
# almost no environment, so everything here is an absolute path and nothing
# relies on an interactive shell profile.
#
# The republish step shells out to `claude -p` because re-rendering the HTML is
# only half the job -- the Artifact has to be pushed to the same URL, and that
# goes through the Artifact tool, which only Claude can call.
#
# Usage:  ./refresh.sh            full run
#         ./refresh.sh --no-publish   pull and render only

set -uo pipefail

PROJECT="/Users/raincityanalytics/projects/robinhood-chain-pulse"
PYTHON="/Users/raincityanalytics/anaconda3/envs/node/bin/python3"
CLAUDE="/Users/raincityanalytics/.local/bin/claude"
ARTIFACT_URL="https://claude.ai/code/artifact/573d969c-d31d-442a-af07-521f41f4a757"
SITE_URL="https://hoodscout.pages.dev"
LOG="$PROJECT/out/refresh.log"

PUBLISH=1
[ "${1:-}" = "--no-publish" ] && PUBLISH=0

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
if ! "$PYTHON" build_dashboard.py --base-url "$SITE_URL" >> "$LOG" 2>&1; then
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
if [ "${SKIP_DEPLOY:-0}" != "1" ]; then
    if npx --yes wrangler pages deploy "$PROJECT/out/site" \
        --project-name=hoodscout --branch=main --commit-dirty=true >> "$LOG" 2>&1; then
        say "deployed to $SITE_URL"
    else
        say "WARN: Pages deploy failed — run 'npx wrangler login' once, then retry"
    fi
fi

# 4. Republish to the SAME artifact URL so the link never changes.
#
# force:true is required and is safe HERE specifically. Every `claude -p` run
# is a fresh session that did not itself publish this artifact, so it always
# sees the previous night's version as "another session's" and refuses with a
# 409 unless told otherwise -- without force the job fails silently every
# single night. It is safe because this pipeline is the artifact's only
# writer and the page is a pure render of out/pulse.json, so there is never
# unmerged work on the other side to lose.
#
# The tradeoff: a hand-edit made directly to the published artifact WILL be
# overwritten at the next run. Edit build_dashboard.py, not the artifact.
if [ "$PUBLISH" = "1" ]; then
    PROMPT="Publish the file $PROJECT/out/dashboard.html as an Artifact, updating the existing artifact at $ARTIFACT_URL. Pass url='$ARTIFACT_URL' so the URL is preserved, and pass force=true — this is an automated nightly rebuild and the local file is authoritative, so a version conflict is expected and should be overwritten. Use favicon 🏹. The file is already built and current: do not modify it, do not read the rest of the project, just publish it. Reply with only the resulting URL."
    if "$CLAUDE" -p "$PROMPT" \
        --allowedTools "Artifact" \
        --permission-mode acceptEdits >> "$LOG" 2>&1; then
        say "republished to $ARTIFACT_URL"
    else
        say "WARN: republish failed — out/dashboard.html on disk is still current"
    fi
fi

say "=== refresh done ==="
