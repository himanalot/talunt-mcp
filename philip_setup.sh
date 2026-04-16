#!/usr/bin/env bash
# End-to-end setup for the "Industry ML Researchers (Philip)" sequence.
# Requires: uv installed, signed in to talunt.io in Chrome.
# Run from anywhere:  bash philip_setup.sh
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
talunt() { uv run --script "$SCRIPT_DIR/cli.py" "$@"; }

SEARCH_DIR=/tmp/philip_searches
mkdir -p "$SEARCH_DIR"
POLL_TIMEOUT=360   # seconds per search — polls GET /multi-source-search/{job_id} every 3s

# ---------- 1. create draft sequence ----------
echo ">>> 1. Creating draft sequence"
SEQ_ID=$(talunt create "Industry ML Researchers (Philip)" --max-per-day 2000 | jq -r '.id')
echo "    sequence id: $SEQ_ID"

# ---------- 2. add LinkedIn step ----------
echo ">>> 2. Adding LinkedIn step (auto_connect + wait_for_connection + ai_personalize all on)"
MESSAGE=$(cat <<'EOF'
Thanks for connecting {{First Name OR Title w/ Last Name if appropriate e.g. Dr.}}! I'm a senior at Phillips Academy Andover heading to Stanford CS/MS&E in the fall. My research at Dartmouth has focused on fairness benchmarks for LLMs across English dialects (we called it EnDive / AAVENUE, presented at NAACL and EMNLP). I'd been curious how industry researchers working on {{their specific ML area, e.g. LLM alignment / eval / production NLP}} organize their workflows. I've struggled with this during my research, and since I want to pursue this further in the future, I'd love to get some insights here.

Would you be up for a 15 min chat? Happy to buy you a digital coffee!
EOF
)
talunt add-linkedin-step "$SEQ_ID" --message "$MESSAGE" > /dev/null
echo "    step added"

# ---------- 3. run 3 searches in parallel with long polls ----------
echo ">>> 3. Running 3 searches in parallel (each polls up to ${POLL_TIMEOUT}s for the full target)"
(
  talunt search "ML engineers working on LLM evaluation, benchmarks, and model testing at AI companies" \
    --limit 100 --poll-timeout $POLL_TIMEOUT > "$SEARCH_DIR/llm_eval.json"
) &
PID_LLM=$!
(
  talunt search "applied NLP researchers at tech companies building production language model systems" \
    --limit 100 --poll-timeout $POLL_TIMEOUT > "$SEARCH_DIR/applied_nlp.json"
) &
PID_NLP=$!
(
  talunt search "AI fairness responsible AI researchers in industry working on bias evaluation" \
    --limit 100 --poll-timeout $POLL_TIMEOUT > "$SEARCH_DIR/ai_fairness.json"
) &
PID_FAIR=$!

wait $PID_LLM $PID_NLP $PID_FAIR || true

TOTAL=0
for f in "$SEARCH_DIR"/llm_eval.json "$SEARCH_DIR"/applied_nlp.json "$SEARCH_DIR"/ai_fairness.json; do
  n=$(jq '.candidates | length' "$f")
  pool=$(jq '.total_count' "$f")
  target=$(jq '.progress.target // 100' "$f")
  TOTAL=$((TOTAL + n))
  printf "    %-28s %3d / %d requested  (pool size: %s)\n" "$(basename "$f")" "$n" "$target" "$pool"
done
echo "    total candidates across 3 searches: $TOTAL"

# ---------- 4. import each search into the sequence ----------
echo ">>> 4. Importing into the sequence (server creates talent_profiles + enrolls atomically)"
for f in "$SEARCH_DIR"/llm_eval.json "$SEARCH_DIR"/applied_nlp.json "$SEARCH_DIR"/ai_fairness.json; do
  res=$(talunt import-search "$SEQ_ID" --from "$f")
  imported=$(echo "$res" | jq -r '.imported // .recordCount // 0')
  enrolled=$(echo "$res" | jq -r '.enrolled // 0')
  failed=$(echo "$res" | jq -r '.failed // 0')
  printf "    %-28s imported=%s enrolled=%s failed=%s\n" "$(basename "$f")" "$imported" "$enrolled" "$failed"
done

# ---------- 5. give server a beat for async processing, then verify ----------
echo ">>> 5. Waiting 20s for async enrollment processing, then verifying totals"
sleep 20
STATS=$(talunt seq-stats "$SEQ_ID")
TOT_ENROLLED=$(echo "$STATS" | jq '.totalEnrolled')
TOT_ACTIVE=$(echo "$STATS" | jq '.totalActive')
TOT_WAITING=$(echo "$STATS" | jq '.totalWaitingConnection')
echo "    totalEnrolled=$TOT_ENROLLED  totalActive=$TOT_ACTIVE  totalWaitingConnection=$TOT_WAITING"

if [ "$TOT_ENROLLED" -lt "$TOTAL" ]; then
  echo ""
  echo "    NOTE: $TOT_ENROLLED enrolled < $TOTAL imported. Usually means dedupe (same"
  echo "          person appeared in multiple searches) — not an error."
fi

OWNER=$(talunt team | jq -r '.members[] | select(.isCurrentUser) | .name')

cat <<EOF

============================================================
Draft sequence ready, OWNED by you ($OWNER).
When you activate, connection requests + the first DM (post-accept)
will go out from YOUR LinkedIn account at up to 2000/day.

Sequence id: $SEQ_ID

Review before activating:
    bash $SCRIPT_DIR/peek.sh $SEQ_ID          # stats + sample messages + 5 enrollments
or:
    uv run --script $SCRIPT_DIR/cli.py messages $SEQ_ID
    uv run --script $SCRIPT_DIR/cli.py seq-results $SEQ_ID | jq '.candidates[0:5] | map({name: .candidate_name, title: .current_title, company: .current_company})'

When ready to go live:
    uv run --script $SCRIPT_DIR/cli.py activate $SEQ_ID

To pause at any time:
    uv run --script $SCRIPT_DIR/cli.py pause $SEQ_ID

If anything looks wrong, nuke and restart:
    uv run --script $SCRIPT_DIR/cli.py delete $SEQ_ID --yes
============================================================
EOF
