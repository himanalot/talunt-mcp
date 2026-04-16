#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastmcp>=2.0",
#     "httpx",
#     "browser-cookie3",
# ]
# ///
"""
talunt CLI — thin wrapper over server.py that emits JSON for piping.

Usage:
  talunt list-sequences
  talunt find-candidate "armon"
  talunt seq <name-or-id>                 # resolves name to id then get-sequence
  talunt seq-stats <name-or-id>           # just the stats object
  talunt messages [name-or-id]            # message templates (all or one)
  talunt search "senior engineers sf" --limit 5
  talunt analyze "ml engineers nyc"
  talunt activate <name-or-id>
  talunt pause <name-or-id>
  talunt delete <name-or-id>
  talunt create <name> [--max-per-day 2000]
  talunt add-linkedin-step <seq> --message 'Hi {{First Name}}, ...'
  talunt import-search <seq> --from search.json [--enrich]
  talunt history                          # search history
  talunt contacts [--limit 20] [--offset 0]
  talunt team
  talunt companies
  talunt personalization <seq> <enrollment_id>

Output: JSON to stdout. Pipe through jq, grep, etc.
Examples:
  talunt list-sequences | jq '.sequences[] | {name, status}'
  talunt find-candidate armon | jq '.[].sequence_name'
  talunt seq-results Eng | jq '.stats'
"""
from __future__ import annotations
import argparse, json, os, sys, re

# Load server.py from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa


def out(obj):
    """Dump JSON to stdout."""
    print(json.dumps(obj, indent=2, default=str))


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def resolve_seq(ref: str) -> str:
    """Accept a sequence id or a name (case-insensitive substring) and return the id."""
    if UUID_RE.match(ref):
        return ref
    seqs = server.list_sequences()["sequences"]
    ref_l = ref.lower()
    exact = [s for s in seqs if s["name"].lower() == ref_l]
    sub = [s for s in seqs if ref_l in s["name"].lower()]
    pool = exact or sub
    if not pool:
        print(f"no sequence matches {ref!r}", file=sys.stderr)
        print("available:", file=sys.stderr)
        for s in seqs:
            print(f"  {s['name']}", file=sys.stderr)
        sys.exit(2)
    if len(pool) > 1 and not exact:
        print(f"ambiguous, {len(pool)} matches for {ref!r}:", file=sys.stderr)
        for s in pool:
            print(f"  {s['name']}  ({s['id']})", file=sys.stderr)
        sys.exit(2)
    return pool[0]["id"]


def cmd_list_sequences(args):
    out(server.list_sequences())


def cmd_find_candidate(args):
    out(server.find_candidate_in_sequences(args.query))


def cmd_seq(args):
    out(server.get_sequence(resolve_seq(args.ref)))


def cmd_seq_results(args):
    out(server.sequence_results(resolve_seq(args.ref)))


def cmd_seq_stats(args):
    out(server.sequence_results(resolve_seq(args.ref)).get("stats", {}))


def cmd_messages(args):
    if args.ref:
        out(server.get_sequence_messages(resolve_seq(args.ref)))
    else:
        all_ = {}
        for s in server.list_sequences()["sequences"]:
            all_[s["name"]] = server.get_sequence_messages(s["id"])
        out(all_)


def cmd_search(args):
    out(server.multi_source_search(
        query=args.query, limit=args.limit, mode=args.mode,
        poll=not args.no_poll, poll_timeout=args.poll_timeout,
    ))


def cmd_analyze(args):
    # analyze returns newline-delimited JSON as text; parse into a list
    raw = server.analyze_search_query(args.query, args.type)
    if isinstance(raw, str):
        lines = [l for l in raw.splitlines() if l.strip()]
        parsed = []
        for l in lines:
            try:
                parsed.append(json.loads(l))
            except json.JSONDecodeError:
                parsed.append({"raw": l})
        out(parsed)
    else:
        out(raw)


def cmd_activate(args):
    out(server.activate_sequence(resolve_seq(args.ref)))


def cmd_pause(args):
    out(server.pause_sequence(resolve_seq(args.ref)))


def cmd_delete(args):
    if not args.yes:
        print("refusing to delete without --yes", file=sys.stderr)
        sys.exit(2)
    out(server.delete_sequence(resolve_seq(args.ref)))


def cmd_create(args):
    out(server.create_sequence(
        name=args.name,
        max_per_day=args.max_per_day,
        description=args.description or "",
    ))


def cmd_add_linkedin_step(args):
    out(server.add_linkedin_step(
        sequence_id=resolve_seq(args.ref),
        message_template=args.message,
        step_name=args.step_name,
        send_as_user_id=args.send_as,
    ))


def cmd_import_search(args):
    with open(args.from_file) as f:
        data = json.load(f)
    # Accept either {candidates: [...]} or a bare list
    cands = data["candidates"] if isinstance(data, dict) and "candidates" in data else data
    out(server.import_search_results_to_sequence(
        search_candidates=cands,
        sequence_id=resolve_seq(args.ref),
        enable_enrichment=args.enrich,
    ))


def cmd_history(args):
    out(server.search_history())


def cmd_contacts(args):
    out(server.list_contacts(limit=args.limit, offset=args.offset))


def cmd_team(args):
    out(server.team_members())


def cmd_companies(args):
    out(server.list_companies())


def cmd_personalization(args):
    out(server.sequence_personalization(
        sequence_id=resolve_seq(args.ref),
        candidate_sequence_id=args.enrollment_id,
    ))


def cmd_candidate(args):
    out(server.candidate_analysis(args.candidate_id))


def cmd_agent_runs(args):
    out(server.agent_runs())


def cmd_conversations(args):
    out(server.list_conversations())


def cmd_conversation(args):
    out(server.get_conversation(args.id))


def cmd_update_sequence(args):
    body = json.loads(args.body)
    out(server.update_sequence(resolve_seq(args.ref), body))


def cmd_update_step_message(args):
    out(server.update_sequence_step_message(
        sequence_id=resolve_seq(args.ref),
        step_id=args.step_id,
        message_template=args.message,
    ))


def cmd_add_email_step(args):
    out(server.add_email_step(
        sequence_id=resolve_seq(args.ref),
        subject_template=args.subject,
        message_template=args.message,
        step_name=args.step_name,
    ))


def cmd_enroll_candidates(args):
    cands = json.loads(args.candidates)
    out(server.enroll_candidates(resolve_seq(args.ref), cands))


def cmd_set_status(args):
    out(server.set_sequence_status(resolve_seq(args.ref), args.status))


def cmd_talent_lists(args):
    out(server.list_talent_lists())


def cmd_create_list(args):
    out(server.create_talent_list(args.name))


def cmd_add_list_members(args):
    members = json.loads(args.members)
    out(server.add_members_to_list(args.list_id, members))


def cmd_enroll_list(args):
    out(server.enroll_list_in_sequence(
        list_id=args.list_id,
        sequence_id=resolve_seq(args.ref),
        exclude_already_enrolled=args.exclude_already_enrolled,
        only_with_email=args.only_with_email,
        only_with_linkedin=args.only_with_linkedin,
    ))


def cmd_send_now(args):
    out(server.send_now(resolve_seq(args.ref), args.enrollment_id))


def cmd_skip_step(args):
    out(server.skip_candidate_step(resolve_seq(args.ref), args.enrollment_id))


def cmd_send_reply(args):
    out(server.send_reply(
        sequence_id=resolve_seq(args.ref),
        enrollment_id=args.enrollment_id,
        message=args.message,
        subject=args.subject,
        channel=args.channel,
    ))


def cmd_gen_personalization(args):
    out(server.generate_personalization(
        resolve_seq(args.ref), args.enrollment_ids
    ))


def cmd_save_personalization(args):
    out(server.save_personalization(
        sequence_id=resolve_seq(args.ref),
        personalized_message=args.message,
        personalization_id=args.personalization_id,
        candidate_sequence_id=args.enrollment_id,
        step_id=args.step_id,
        personalized_subject=args.subject,
        is_approved=not args.no_approve,
    ))


def cmd_linkedin_accounts(args):
    out(server.linkedin_accounts())


def cmd_owners(args):
    """Show sequences grouped by owner (joins created_by_user_id → team members)."""
    members = server.team_members()
    by_id = {m["id"]: m for m in members["members"]}
    seqs = server.list_sequences()["sequences"]
    rows = []
    for s in seqs:
        uid = s.get("created_by_user_id")
        m = by_id.get(uid)
        rows.append({
            "sequence": s["name"],
            "status": s["status"],
            "owner_name": m["name"] if m else None,
            "owner_email": m["email"] if m else None,
            "owner_id": uid,
            "sequence_id": s["id"],
        })
    rows.sort(key=lambda r: (r["owner_name"] or "~", r["sequence"]))
    out(rows)


def main():
    p = argparse.ArgumentParser(prog="talunt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-sequences").set_defaults(fn=cmd_list_sequences)

    s = sub.add_parser("find-candidate")
    s.add_argument("query")
    s.set_defaults(fn=cmd_find_candidate)

    s = sub.add_parser("seq"); s.add_argument("ref")
    s.set_defaults(fn=cmd_seq)

    s = sub.add_parser("seq-results"); s.add_argument("ref")
    s.set_defaults(fn=cmd_seq_results)

    s = sub.add_parser("seq-stats"); s.add_argument("ref")
    s.set_defaults(fn=cmd_seq_stats)

    s = sub.add_parser("messages"); s.add_argument("ref", nargs="?")
    s.set_defaults(fn=cmd_messages)

    s = sub.add_parser("search"); s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--mode", default="smart")
    s.add_argument("--no-poll", action="store_true",
                   help="return initial ~25 immediately instead of polling to full target")
    s.add_argument("--poll-timeout", type=int, default=180,
                   help="max seconds to poll for the full result set")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("analyze"); s.add_argument("query")
    s.add_argument("--type", default="people")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("activate"); s.add_argument("ref")
    s.set_defaults(fn=cmd_activate)

    s = sub.add_parser("pause"); s.add_argument("ref")
    s.set_defaults(fn=cmd_pause)

    s = sub.add_parser("delete"); s.add_argument("ref")
    s.add_argument("--yes", action="store_true", help="required confirmation")
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser("create"); s.add_argument("name")
    s.add_argument("--max-per-day", type=int, default=2000)
    s.add_argument("--description", default=None)
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser("add-linkedin-step"); s.add_argument("ref")
    s.add_argument("--message", required=True)
    s.add_argument("--step-name", default="LinkedIn Message")
    s.add_argument("--send-as", default=None, help="override sender: user_id from team_members")
    s.set_defaults(fn=cmd_add_linkedin_step)

    s = sub.add_parser("import-search"); s.add_argument("ref")
    s.add_argument("--from", dest="from_file", required=True,
                   help="JSON file with either {candidates:[...]} or a bare list")
    s.add_argument("--enrich", action="store_true")
    s.set_defaults(fn=cmd_import_search)

    sub.add_parser("history").set_defaults(fn=cmd_history)

    s = sub.add_parser("contacts")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(fn=cmd_contacts)

    sub.add_parser("team").set_defaults(fn=cmd_team)
    sub.add_parser("companies").set_defaults(fn=cmd_companies)
    sub.add_parser("agent-runs").set_defaults(fn=cmd_agent_runs)
    sub.add_parser("owners", help="sequences grouped by owner").set_defaults(fn=cmd_owners)

    sub.add_parser("conversations").set_defaults(fn=cmd_conversations)
    s = sub.add_parser("conversation"); s.add_argument("id")
    s.set_defaults(fn=cmd_conversation)

    s = sub.add_parser("update-sequence"); s.add_argument("ref")
    s.add_argument("--body", required=True, help="JSON body for PATCH")
    s.set_defaults(fn=cmd_update_sequence)

    s = sub.add_parser("update-step-message"); s.add_argument("ref")
    s.add_argument("--step-id", required=True)
    s.add_argument("--message", required=True)
    s.set_defaults(fn=cmd_update_step_message)

    s = sub.add_parser("add-email-step"); s.add_argument("ref")
    s.add_argument("--subject", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--step-name", default="Email")
    s.set_defaults(fn=cmd_add_email_step)

    s = sub.add_parser("enroll-candidates"); s.add_argument("ref")
    s.add_argument("--candidates", required=True,
                   help='JSON list e.g. \'[{"candidateType":"talent_profile","candidateId":"..."}]\'')
    s.set_defaults(fn=cmd_enroll_candidates)

    s = sub.add_parser("set-status"); s.add_argument("ref")
    s.add_argument("status", choices=["draft", "active", "paused"])
    s.set_defaults(fn=cmd_set_status)

    sub.add_parser("talent-lists").set_defaults(fn=cmd_talent_lists)

    s = sub.add_parser("create-list"); s.add_argument("name")
    s.set_defaults(fn=cmd_create_list)

    s = sub.add_parser("add-list-members"); s.add_argument("list_id")
    s.add_argument("--members", required=True,
                   help='JSON list e.g. \'[{"member_type":"talent_profile","member_id":"..."}]\'')
    s.set_defaults(fn=cmd_add_list_members)

    s = sub.add_parser("enroll-list"); s.add_argument("list_id"); s.add_argument("ref")
    s.add_argument("--no-exclude-already-enrolled", dest="exclude_already_enrolled",
                   action="store_false", default=True)
    s.add_argument("--only-with-email", action="store_true", default=None)
    s.add_argument("--only-with-linkedin", action="store_true", default=None)
    s.set_defaults(fn=cmd_enroll_list)

    s = sub.add_parser("send-now", help="force-send the next step for one enrollment")
    s.add_argument("ref"); s.add_argument("enrollment_id")
    s.set_defaults(fn=cmd_send_now)

    s = sub.add_parser("skip-step"); s.add_argument("ref"); s.add_argument("enrollment_id")
    s.set_defaults(fn=cmd_skip_step)

    s = sub.add_parser("send-reply")
    s.add_argument("ref"); s.add_argument("enrollment_id")
    s.add_argument("--message", required=True)
    s.add_argument("--subject", default=None)
    s.add_argument("--channel", default="linkedin", choices=["linkedin", "email"])
    s.set_defaults(fn=cmd_send_reply)

    s = sub.add_parser("gen-personalization", help="trigger AI draft generation")
    s.add_argument("ref")
    s.add_argument("enrollment_ids", nargs="+")
    s.set_defaults(fn=cmd_gen_personalization)

    s = sub.add_parser("save-personalization")
    s.add_argument("ref")
    s.add_argument("--message", required=True)
    s.add_argument("--personalization-id", default=None)
    s.add_argument("--enrollment-id", default=None, help="candidate_sequence_id (with --step-id)")
    s.add_argument("--step-id", default=None)
    s.add_argument("--subject", default=None)
    s.add_argument("--no-approve", action="store_true")
    s.set_defaults(fn=cmd_save_personalization)

    sub.add_parser("accounts", help="connected LinkedIn accounts"
                   ).set_defaults(fn=cmd_linkedin_accounts)

    s = sub.add_parser("personalization"); s.add_argument("ref")
    s.add_argument("enrollment_id")
    s.set_defaults(fn=cmd_personalization)

    s = sub.add_parser("candidate"); s.add_argument("candidate_id")
    s.set_defaults(fn=cmd_candidate)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
