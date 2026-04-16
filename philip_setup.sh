#!/usr/bin/env bash
# End-to-end setup for the "Industry ML Researchers (Philip)" sequence.
# Requires: uv installed, signed in to talunt.io in Chrome.
# Run from anywhere:  bash philip_setup.sh
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
talunt() { uv run --script "$SCRIPT_DIR/cli.py" "$@"; }

SEARCH_DIR=/tmp/philip_searches
mkdir -p "$SEARCH_DIR"

echo ">>> 1. Creating draft sequence"
SEQ_ID=$(talunt create "Industry ML Researchers (Philip)" --max-per-day 2000 | jq -r '.id')
echo "    sequence id: $SEQ_ID"

echo ">>> 2. Adding LinkedIn step (auto_connect + wait_for_connection + ai_personalize all on)"
MESSAGE=$(cat <<'EOF'
Thanks for connecting {{First Name OR Title w/ Last Name if appropriate e.g. Dr.}}! I'm a senior at Phillips Academy Andover heading to Stanford CS/MS&E in the fall. My research at Dartmouth has focused on fairness benchmarks for LLMs across English dialects (we called it EnDive / AAVENUE, presented at NAACL and EMNLP). I'd been curious how industry researchers working on {{their specific ML area, e.g. LLM alignment / eval / production NLP}} organize their workflows. I've struggled with this during my research, and since I want to pursue this further in the future, I'd love to get some insights here.

Would you be up for a 15 min chat? Happy to buy you a digital coffee!
EOF
)
talunt add-linkedin-step "$SEQ_ID" --message "$MESSAGE" > /dev/null
echo "    step added"

echo ">>> 3. Running 3 searches in parallel (auto-polls for full target set, ~1-2 min each)"
talunt search "ML engineers working on LLM evaluation, benchmarks, and model testing at AI companies"  --limit 100 --poll-timeout 240 > "$SEARCH_DIR/llm_eval.json"    &
talunt search "applied NLP researchers at tech companies building production language model systems"  --limit 100 --poll-timeout 240 > "$SEARCH_DIR/applied_nlp.json" &
talunt search "AI fairness responsible AI researchers in industry working on bias evaluation"         --limit 100 --poll-timeout 240 > "$SEARCH_DIR/ai_fairness.json" &
wait
for f in "$SEARCH_DIR"/llm_eval.json "$SEARCH_DIR"/applied_nlp.json "$SEARCH_DIR"/ai_fairness.json; do
  n=$(jq '.candidates | length' "$f")
  echo "    $(basename "$f"): $n candidates  (pool: $(jq '.total_count' "$f"))"
done

echo ">>> 4. Importing each search into the sequence (creates talent_profiles + enrolls atomically)"
talunt import-search "$SEQ_ID" --from "$SEARCH_DIR/llm_eval.json"
talunt import-search "$SEQ_ID" --from "$SEARCH_DIR/applied_nlp.json"
talunt import-search "$SEQ_ID" --from "$SEARCH_DIR/ai_fairness.json"

echo ">>> 5. Current sequence stats"
talunt seq-stats "$SEQ_ID"

cat <<EOF

============================================================
Draft is ready, OWNED by you ($(talunt team | jq -r '.members[] | select(.isCurrentUser) | .name')).
When you activate, connection requests start going out from YOUR LinkedIn.

To activate:
    talunt activate $SEQ_ID

To peek before activating:
    talunt messages $SEQ_ID
    talunt seq-results $SEQ_ID | jq '.candidates[0:5]'
============================================================
EOF
