# talunt-mcp

Reverse-engineered MCP server + CLI for [app.talunt.io](https://app.talunt.io).
Auth is read live from your local Chrome cookies — no API keys, no token
management. As long as you're signed in to talunt.io in Chrome, the tools
work as you.

Two artifacts:
- `server.py` — FastMCP server (37 tools)
- `cli.py` — thin wrapper that emits JSON for piping through `jq`, `grep`, etc.

Both share the same underlying functions; use whichever fits the task.

---

## Setup

**Prereqs**
- macOS (works on Linux with minor tweaks; Windows untested)
- Google Chrome, signed into talunt.io (any Chrome profile with an active session)
- `uv` (fast Python package manager)
- Claude Code (optional, only if you want the MCP)

**Install uv** (one-time)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Clone this repo**
```bash
git clone <this-repo-url> ~/talunt-mcp
cd ~/talunt-mcp
```

First tool call will trigger a macOS Keychain prompt for Chrome cookie access. Click **Always Allow**.

---

## Using the CLI

Easiest: add an alias to `~/.zshrc`:
```bash
alias talunt='uv run --script ~/talunt-mcp/cli.py'
```

Then:
```bash
talunt list-sequences
talunt seq-stats Eng                    # resolves "Eng" to sequence id
talunt find-candidate armon
talunt messages                         # all sequence message templates
talunt accounts                         # connected LinkedIn accounts
talunt owners                           # sequences grouped by owner
talunt list-sequences | jq '.sequences[] | select(.status=="active") | .name'
```

All subcommands: run `talunt --help`.

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `TALUNT_COMPANY_ID` | Shepherd Health workspace | Override if you're in a different company |

---

## Using the MCP (for Claude Code)

Register once:
```bash
claude mcp add talunt -- uv run --script ~/talunt-mcp/server.py
```

Then inside Claude Code: run `/mcp` to verify the `talunt` server is connected. 37 tools become available as `mcp__talunt__<name>`.

---

## One quirk worth knowing: progressive search

`multi_source_search` is async-progressive on the server. A `POST /api/recruiter/multi-source-search` returns ~25 candidates quickly even when you ask for `limit=100`; the server keeps enriching in the background. The full set becomes retrievable via `GET /api/recruiter/multi-source-search/{job_id}` after ~15-90 seconds.

This MCP/CLI handles it for you: `multi_source_search` (and `talunt search`) automatically polls GET every 3s until `progress.found >= target` or `poll_timeout` elapses, then returns the full set. Use `poll=False` / `--no-poll` if you want the fast initial ~25 back.

## Philip handoff: create the ML sequence + run searches + enroll

Message template for the step (already what we agreed on):

```
Thanks for connecting {{First Name OR Title w/ Last Name if appropriate e.g. Dr.}}! I'm a senior at Phillips Academy Andover heading to Stanford CS/MS&E in the fall. My research at Dartmouth has focused on fairness benchmarks for LLMs across English dialects (we called it EnDive / AAVENUE, presented at NAACL and EMNLP). I'd been curious how industry researchers working on {{their specific ML area, e.g. LLM alignment / eval / production NLP}} organize their workflows. I've struggled with this during my research, and since I want to pursue this further in the future, I'd love to get some insights here.

Would you be up for a 15 min chat? Happy to buy you a digital coffee!
```

**One-shot script:** a bash script at `philip_setup.sh` does everything below in one go. Just run:
```bash
bash philip_setup.sh
```
It creates the sequence, adds the LinkedIn step, runs three parallel searches (with auto-polling to full 100 each), imports them as enrollments, and prints the activation command. The sequence ends in `draft` status — review the stats, then activate when ready.

Manual walkthrough (if you want to tweak queries or step by step):

```bash
# 1. Create the draft sequence (maxPerDay=2000 — server default is 50)
talunt create "Industry ML Researchers (Philip)" --max-per-day 2000

# grab the id from the output, e.g.
SEQ_ID=<id-from-above>

# 2. Add the LinkedIn step. send_after_connection + auto_connect + ai_personalize
#    all default to True, matching how the rest of your sequences are set up.
#    Save the message to a file first so the shell doesn't mangle the newlines:
cat > /tmp/msg.txt <<'EOF'
Thanks for connecting {{First Name OR Title w/ Last Name if appropriate e.g. Dr.}}! I'm a senior at Phillips Academy Andover heading to Stanford CS/MS&E in the fall. My research at Dartmouth has focused on fairness benchmarks for LLMs across English dialects (we called it EnDive / AAVENUE, presented at NAACL and EMNLP). I'd been curious how industry researchers working on {{their specific ML area, e.g. LLM alignment / eval / production NLP}} organize their workflows. I've struggled with this during my research, and since I want to pursue this further in the future, I'd love to get some insights here.

Would you be up for a 15 min chat? Happy to buy you a digital coffee!
EOF
talunt add-linkedin-step $SEQ_ID --message "$(cat /tmp/msg.txt)"

# 3. Import the three prepared search result files into the sequence.
#    These were generated by Ishan from talunt search; each has up to ~100
#    candidates. Importing creates talent_profile rows AND enrolls them
#    into the sequence atomically (server-side CSV bulk import).
talunt import-search $SEQ_ID --from ./searches_for_philip/llm_eval.json
talunt import-search $SEQ_ID --from ./searches_for_philip/applied_nlp.json
talunt import-search $SEQ_ID --from ./searches_for_philip/ai_fairness.json

# 4. (Optional) Review before activating
talunt seq-stats $SEQ_ID                        # totals
talunt messages $SEQ_ID                         # confirm message
talunt seq-results $SEQ_ID | jq '.candidates[0:3]'   # peek at 3 enrollments

# 5. Activate — connection requests start going out from Philip's LinkedIn
talunt activate $SEQ_ID
```

### Why Philip needs to run this himself

`send_as_user_id` only applies to **email** steps (confirmed by the Talunt
frontend JS — the picker is gated on `messageType === "email"`). For LinkedIn
routing, the sequence sends from whoever *owns* the sequence, and
`created_by_user_id` can't be changed after creation (PATCH is silently
ignored). So the owner must be Philip, meaning Philip must create the
sequence from his own authenticated session.

Once Ishan hands off the three `searches_for_philip/*.json` files (email /
AirDrop / shared drive — **not checked into git**, they contain real
LinkedIn profile data), Philip can run the commands above in ~2 minutes.

---

## Architecture / notes

- Cookies are read via `browser-cookie3` from the default Chrome profile
  on each API call — no caching, so sessions refresh transparently.
- All tool functions live in `server.py`. `cli.py` imports them and adds
  argparse plumbing + name-to-id resolution for sequence refs.
- Sequence refs accept either a UUID or a case-insensitive name substring
  (e.g. `talunt seq "industry ml"` resolves to "Industry ML Researchers").
- `import_search_results_to_sequence` builds a CSV in memory from a
  `multi_source_search` candidates array and POSTs it to
  `/api/talent-pool/bulk-import` with `sequence_id` set — the server then
  creates `talent_profile` rows and enrolls them atomically.
- PII from search results never leaves your machine unless you share it
  explicitly.

---

## Subcommands

```
list-sequences                  list all sequences
seq <ref>                       get full sequence (sequence + steps + flow)
seq-results <ref>               per-candidate enrollment data (can be large)
seq-stats <ref>                 just the stats object
messages [ref]                  message templates (all, or one sequence)
owners                          sequences grouped by owner
find-candidate <substr>         which sequences is this person in?
create <name> [--max-per-day N] new draft sequence
add-linkedin-step <ref> --message ... [--send-as USER_ID]
add-email-step <ref> --subject ... --message ...
import-search <ref> --from path.json [--enrich]
enroll-candidates <ref> --candidates '[{...}]'
enroll-list <list_id> <seq_ref> [--only-with-email/--only-with-linkedin]
activate <ref>                  status → active (start sending)
pause <ref>                     status → paused
set-status <ref> {draft|active|paused}
delete <ref> --yes              delete (irreversible)
update-sequence <ref> --body '{...}'       raw PATCH
update-step-message <ref> --step-id ... --message ...
send-now <ref> <enrollment_id>           force-send next step
skip-step <ref> <enrollment_id>          advance without sending
send-reply <ref> <enrollment_id> --message ...
gen-personalization <ref> <enrollment_id>...
save-personalization <ref> --message ...
search <query> [--limit N] [--mode smart]
analyze <query>                 AI interpretation of a search query
history                         search history
personalization <ref> <enrollment_id>
candidate <candidate_id>        full candidate analysis
contacts [--limit N] [--offset N]
team                            team members
accounts                        connected LinkedIn accounts
companies                       workspace companies
talent-lists                    talent lists
create-list <name>
add-list-members <list_id> --members '[{...}]'
conversations                   chat conversation list
conversation <id>               full chat thread with messages
agent-runs
```
