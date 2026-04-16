#!/usr/bin/env bash
# Quick preview of a sequence before activating.  Usage:  bash peek.sh <seq-id-or-name>
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
talunt() { uv run --script "$SCRIPT_DIR/cli.py" "$@"; }

REF="${1:?usage: bash peek.sh <seq-id-or-name>}"

echo "=== STATS ==="
talunt seq-stats "$REF"

echo
echo "=== MESSAGE TEMPLATES ==="
talunt messages "$REF" | jq -r '.[] | "step \(.step_order): \(.step_name)\n\(.message_template)\n"'

echo "=== FIRST 5 ENROLLMENTS ==="
talunt seq-results "$REF" | jq '.candidates[0:5] | map({name: .candidate_name, status, title: .current_title, company: .current_company, linkedin: .linkedin_url})'

echo
echo "=== STATUS DISTRIBUTION ==="
talunt seq-results "$REF" | jq '[.candidates[] | .status] | group_by(.) | map({status: .[0], count: length})'
