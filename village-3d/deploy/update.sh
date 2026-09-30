#!/usr/bin/env bash
# Daily job: when aidigestorg/ai-village publishes a new revision, fetch the tables extract.py reads, rebuild data/
# and publish the site. Same revision as the last build: exits without downloading or rebuilding anything.
# The tables live as one copy in $VILLAGE_DATA, updated in place (only changed files are fetched; the screenshot
# archives are never downloaded).
#   crontab: 30 4 * * * flock -n /tmp/village-update.lock /path/to/deploy/update.sh >> $HOME/village-update.log 2>&1
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
REPO=aidigestorg/ai-village
export PATH="$HOME/.local/bin:$PATH" VILLAGE_DATA=${VILLAGE_DATA:-/data/ai-village-tables}

latest=$(uv run -q --no-project --with huggingface_hub python -c \
	"from huggingface_hub import HfApi; print(HfApi().dataset_info('$REPO').sha)")
built=$(cat "$VILLAGE_DATA/.built" 2>/dev/null || true)
if [ "$latest" = "$built" ]; then echo "$(date -Is) ${latest:0:8} already built"; exit 0; fi

echo "$(date -Is) new revision ${built:0:8} -> ${latest:0:8}: downloading"
uvx -q --from huggingface_hub hf download "$REPO" --repo-type dataset --revision "$latest" --local-dir "$VILLAGE_DATA" \
	--format quiet manifest.json agents.jsonl.gz agent_goals.jsonl.gz agent_memories.jsonl.gz chat_messages.jsonl.gz \
	claude_code_messages.jsonl.gz computer_use_sessions.jsonl.gz computer_use_turns.jsonl.gz events.jsonl.gz \
	summaries.jsonl.gz village_goals.jsonl.gz village-transcript.json >/dev/null  # it prints the folder path
"$HERE/deploy/deploy.sh" publish --build
echo "$latest" > "$VILLAGE_DATA/.built"  # only after a successful publish, so a failed run retries tomorrow
echo "$(date -Is) published data from ${latest:0:8}"
