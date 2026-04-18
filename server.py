# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastmcp>=2.0",
#     "httpx",
#     "browser-cookie3",
# ]
# ///
"""
Talunt MCP server — wraps app.talunt.io endpoints as MCP tools.

Auth: reads the current Supabase session cookie live from the local Chrome
profile via browser-cookie3, so tools automatically work while you are signed
into talunt.io in Chrome. No token management required; cookies rotate as Chrome
refreshes them.
"""
import os
import browser_cookie3
import httpx
from fastmcp import FastMCP

BASE = "https://app.talunt.io"
DOMAIN = "app.talunt.io"
DEFAULT_COMPANY_ID = os.environ.get(
    "TALUNT_COMPANY_ID", "23887c44-176e-4fce-b9c9-453dfbbb9ed6"
)

mcp = FastMCP("talunt")


def _cookies() -> dict:
    jar = browser_cookie3.chrome(domain_name=DOMAIN)
    return {c.name: c.value for c in jar}


def _headers():
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "origin": BASE,
        "referer": f"{BASE}/recruiter/search",
    }


def _post(path: str, body: dict):
    r = httpx.post(
        f"{BASE}{path}", json=body, cookies=_cookies(), headers=_headers(), timeout=180.0
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.text


def _get(path: str):
    r = httpx.get(
        f"{BASE}{path}", cookies=_cookies(), headers=_headers(), timeout=60.0
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.text


def _patch(path: str, body: dict):
    r = httpx.patch(
        f"{BASE}{path}",
        json=body,
        cookies=_cookies(),
        headers=_headers(),
        timeout=60.0,
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.text


def _delete(path: str):
    r = httpx.delete(
        f"{BASE}{path}", cookies=_cookies(), headers=_headers(), timeout=30.0
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.text


# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------

@mcp.tool()
def analyze_search_query(query: str, search_type: str = "people"):
    """AI interpretation of a recruiter search query.

    Returns newline-delimited JSON as plain text (NOT a single JSON object).
    Each line is one of: {"type":"annotation","text":"...","category":"role|skill|location|..."},
    {"type":"summary","content":"..."}, {"type":"insight","title":"...","content":"..."},
    {"type":"signal","items":[...]}. Use it to preview how the app will parse a
    query before running a search. search_type is typically 'people'."""
    return _post(
        "/api/recruiter/analyze-search-query",
        {"query": query, "searchType": search_type},
    )


@mcp.tool()
def multi_source_search(
    query: str,
    mode: str = "smart",
    enrich_live: bool = True,
    limit: int = 100,
    company_id: str = DEFAULT_COMPANY_ID,
    poll: bool = True,
    poll_timeout: int = 120,
):
    """Run a candidate search across enabled sources (LinkedIn + others).

    The API is async-progressive: the initial POST returns ~25 candidates
    immediately with `status: 'completed'` but `progress.found=25/target=limit`.
    The server keeps enriching in the background; the full set is retrieved
    by polling GET /api/recruiter/multi-source-search/{job_id}. By default
    this tool polls until progress.found >= limit or `poll_timeout` seconds
    elapse, then returns the full response.

    Set poll=False to get the initial ~25 quickly. Response shape: {job_id,
    parent_job_id, candidates[], classification, progress{found, target,
    percent, is_searching}, search_criteria, sources_enabled, total_count,
    returned_count, status}. Typical size 500KB-2MB for limit=100. Creates
    a row in search_history with final_results_count matching the full pull."""
    import time
    initial = _post(
        "/api/recruiter/multi-source-search",
        {
            "query": query,
            "companyId": company_id,
            "mode": mode,
            "enrichLive": enrich_live,
            "limit": limit,
        },
    )
    if not poll:
        return initial
    job_id = initial.get("job_id")
    if not job_id:
        return initial
    target = (initial.get("progress") or {}).get("target") or limit
    if len(initial.get("candidates") or []) >= target:
        return initial

    deadline = time.time() + poll_timeout
    latest = initial
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = httpx.get(
                f"{BASE}/api/recruiter/multi-source-search/{job_id}",
                cookies=_cookies(), headers=_headers(), timeout=30.0,
            )
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        latest = r.json()
        prog = latest.get("progress") or {}
        found = prog.get("found", len(latest.get("candidates") or []))
        tgt = prog.get("target") or target
        status = latest.get("status")
        if found >= tgt:
            return latest
        if status in ("error", "cancelled"):
            return latest
    return latest


@mcp.tool()
def search_history():
    """List the recruiter's prior candidate searches, newest first.

    Returns {"searches": [{id, job_id, query, query_title, status,
    final_results_count, limit_requested, search_mode, search_sources{github,
    linkedin, openalex, hackathons, social_media}, initiated_at, completed_at,
    created_at, updated_at, ...}]}. Every multi_source_search call adds an
    entry here."""
    return _get("/api/recruiter/search-history")


# ------------------------------------------------------------------
# AI chat conversations (the /recruiter/chat sidebar, NOT search history)
# ------------------------------------------------------------------

@mcp.tool()
def list_conversations():
    """List saved AI-chat conversations from the /recruiter/chat sidebar.

    Returns {"conversations": [{id, title, created_at, updated_at}]}. These are
    chat threads with the recruiter assistant, not search jobs. For search
    history use `search_history` instead."""
    return _get("/api/chat/conversations")


@mcp.tool()
def get_conversation(conversation_id: str):
    """Fetch one AI-chat conversation including its full message history.

    Returns {id, user_id, company_id, title, messages: [{id, role, parts:
    [{text, type} | tool-call/result parts]}], created_at, updated_at}.
    Messages use the Vercel AI SDK parts[] format."""
    return _get(f"/api/chat/conversations/{conversation_id}")


# ------------------------------------------------------------------
# Outreach sequences
# ------------------------------------------------------------------

@mcp.tool()
def list_sequences():
    """List outreach sequences (the /recruiter/sequences page).

    Returns {"sequences": [{id, name, channel, status, settings, flow_layout,
    total_enrolled, total_sent, total_replied, total_pending, total_failed,
    activated_at, ...}], total, limit, offset}. NOTE: the enrollment/send
    counters in THIS response are often stale or zero — call `sequence_results`
    for accurate per-sequence stats."""
    return _get("/api/network/sequences")


@mcp.tool()
def get_sequence(sequence_id: str):
    """Fetch one outreach sequence's full config.

    Returns {"sequence": {id, name, description, status, channel, settings
    (warmup/timezone/maxPerDay/stopOn*), personalization_prompt, conversion_goal,
    ...}, "steps": [{id, step_order, step_name, message_type,
    message_template, subject_template, delay_days/hours/minutes,
    send_condition, trigger_type, send_after_connection,
    auto_connect_if_not_connected, ai_personalize, ...}], "flowLayout": {...}}.
    message_template supports {{placeholder}} tokens filled at send time."""
    return _get(f"/api/network/sequences/{sequence_id}")


@mcp.tool()
def get_sequence_messages(sequence_id: str):
    """Lightweight view of a sequence's step messages only.

    Returns a list of {step_id, step_order, step_name, message_type,
    subject_template, message_template}. Use this instead of `get_sequence`
    when you just want to read or audit the outreach copy without the full
    config payload."""
    data = _get(f"/api/network/sequences/{sequence_id}")
    return [
        {
            "step_id": s.get("id"),
            "step_order": s.get("step_order"),
            "step_name": s.get("step_name"),
            "message_type": s.get("message_type"),
            "subject_template": s.get("subject_template"),
            "message_template": s.get("message_template"),
        }
        for s in (data.get("steps") or [])
    ]


@mcp.tool()
def sequence_results(sequence_id: str):
    """Full per-candidate results + aggregate stats for a sequence.

    CAUTION: response can be 5+ MB (one row per enrolled candidate, with
    stepHistory and conversationMessages). Response shape:
    {sequence, steps, candidates[], stats}.

    stats keys: totalEnrolled, totalActive, totalWaiting,
    totalWaitingConnection, totalConnectionRequestsPending, totalPendingMessages,
    totalReplied, totalApplied, totalCompleted, totalErrored, replyRate,
    conversionRate.

    candidate.status pipeline: 'active' (enrolled, connection request not yet
    sent) → 'waiting_for_connection' (request sent, awaiting accept) →
    'completed' (all messages sent) → 'replied' / 'applied' / 'errored'
    (terminal states)."""
    return _get(f"/api/network/sequences/{sequence_id}/results")


@mcp.tool()
def sequence_personalization(sequence_id: str, candidate_sequence_id: str):
    """Fetch the AI-drafted personalized message(s) for one enrolled candidate.

    candidate_sequence_id is the candidate's enrollment id from
    `sequence_results` (candidates[i].id), NOT the underlying candidate_id.
    Returns {"personalizations": [{id, candidate_sequence_id, step_id,
    sequence_id, personalized_message, personalized_subject, is_approved,
    ai_model, generated_at, created_at}]}. Empty list means no draft has been
    generated for this enrollment yet."""
    return _get(
        f"/api/network/sequences/{sequence_id}/personalize"
        f"?candidateSequenceId={candidate_sequence_id}"
    )


@mcp.tool()
def update_sequence(sequence_id: str, body: dict):
    """PATCH a sequence. Partial updates allowed.

    body may include any subset of: name, description, conversion_goal,
    personalization_prompt (JSON string with yourRole/whatYouOffer/tone/etc),
    settings (warmup/timezone/maxPerDay/stopOnApply/stopOnReply/nodePositions),
    steps (array — sending this replaces the step list, so include all steps
    you want to keep), status ('draft' | 'active' | 'paused' — prefer
    `activate_sequence` / `pause_sequence` for this).
    Returns {success: bool, validationWarnings: [...]}."""
    return _patch(f"/api/network/sequences/{sequence_id}", body)


@mcp.tool()
def create_sequence(
    name: str,
    max_per_day: int = 2000,
    description: str = "",
    timezone: str = "America/Los_Angeles",
    stop_on_reply: bool = True,
    stop_on_apply: bool = True,
    send_on_weekends: bool = True,
    business_hours_only: bool = False,
    warmup_enabled: bool = True,
    warmup_start: int = 10,
    warmup_target: int = 100,
    warmup_increment_per_day: int = 10,
):
    """Create a new outreach sequence.

    Two-step flow: POSTs /api/network/sequences with {name} (all that the
    create endpoint accepts), then PATCHes settings to apply the overrides.

    Server defaults (applied if not overridden): channel='linkedin',
    status='draft', maxPerDay=50. This tool overrides maxPerDay to 2000
    by default since the server default is too low for most recruiting use.

    Returns the created (and patched) sequence object. Add steps afterwards
    with `add_linkedin_step` or `update_sequence`. New sequences start in
    status='draft' — call `activate_sequence` when ready to start sending."""
    created = _post("/api/network/sequences", {"name": name})
    seq = created["sequence"]
    settings = {
        **seq["settings"],
        "maxPerDay": max_per_day,
        "timezone": timezone,
        "stopOnReply": stop_on_reply,
        "stopOnApply": stop_on_apply,
        "sendOnWeekends": send_on_weekends,
        "businessHoursOnly": business_hours_only,
        "warmup": {
            "enabled": warmup_enabled,
            "startLimit": warmup_start,
            "targetLimit": warmup_target,
            "incrementPerDay": warmup_increment_per_day,
        },
    }
    body = {"settings": settings}
    if description:
        body["description"] = description
    _patch(f"/api/network/sequences/{seq['id']}", body)
    return _get(f"/api/network/sequences/{seq['id']}")["sequence"]


@mcp.tool()
def delete_sequence(sequence_id: str):
    """DELETE a sequence and all its enrollments. Irreversible. Returns {success}."""
    return _delete(f"/api/network/sequences/{sequence_id}")


@mcp.tool()
def activate_sequence(sequence_id: str):
    """Activate a sequence (transition status → 'active' so it starts sending).

    There is no dedicated /activate endpoint — this is PATCH {status: 'active'}
    on /api/network/sequences/{id}. On first activation the server stamps
    `activated_at`; subsequent activations (after pausing) don't reset it."""
    return _patch(f"/api/network/sequences/{sequence_id}", {"status": "active"})


@mcp.tool()
def pause_sequence(sequence_id: str):
    """Pause a running sequence (status → 'paused'). In-flight sends stop;
    `activated_at` is preserved. Resume by calling `activate_sequence`."""
    return _patch(f"/api/network/sequences/{sequence_id}", {"status": "paused"})


@mcp.tool()
def set_sequence_status(sequence_id: str, status: str):
    """Set a sequence's status directly. status ∈ {'draft', 'active', 'paused'}.
    Prefer `activate_sequence` / `pause_sequence` for readability."""
    return _patch(f"/api/network/sequences/{sequence_id}", {"status": status})


@mcp.tool()
def add_linkedin_step(
    sequence_id: str,
    message_template: str,
    step_name: str = "LinkedIn Message",
    delay_days: int = 0,
    delay_hours: int = 0,
    delay_minutes: int = 0,
    send_after_connection: bool = True,
    auto_connect_if_not_connected: bool = True,
    ai_personalize: bool = True,
    send_condition: str = "always",
    send_as_user_id: str = None,
):
    """Append a LinkedIn message step to a sequence (preserves existing steps).

    Sets message_type='message' (the LinkedIn DM type). Defaults match the
    typical flow: wait for connection before sending, auto-send a connection
    request if not already connected, and apply AI personalization.

    message_template uses {{placeholder}} tokens. With ai_personalize=True and
    a configured personalization_prompt on the sequence, the AI will rewrite
    text inside {{ }} per candidate based on their profile. Literal tokens
    outside braces are kept verbatim.

    Example template: "Hi {{First Name}}, I saw your work on
    {{a specific project or paper they contributed to}} — …"

    `send_as_user_id` (optional): override the sending account. If set, the
    step sends from that user's connected LinkedIn account instead of the
    sequence owner's. Use a user_id from `team_members` and verify they have
    an active account in `linkedin_accounts`. (Note: `created_by_user_id`
    cannot be changed after creation — server silently ignores that PATCH
    field — so this is the only way to route a step to a non-owner's
    account.)

    Returns the updated full sequence from GET."""
    import uuid
    current = _get(f"/api/network/sequences/{sequence_id}")
    steps = current.get("steps") or []
    new_step = {
        "id": str(uuid.uuid4()),
        "step_order": len(steps) + 1,
        "step_name": step_name,
        "message_type": "message",
        "message_template": message_template,
        "subject_template": None,
        "send_condition": send_condition,
        "delay_days": delay_days,
        "delay_hours": delay_hours,
        "delay_minutes": delay_minutes,
        "trigger_type": "delay",
        "send_after_connection": send_after_connection,
        "auto_connect_if_not_connected": auto_connect_if_not_connected,
        "ai_personalize": ai_personalize,
        "recipient_type": None,
        "send_as_user_id": send_as_user_id,
        "email_account_id": None,
    }
    steps.append(new_step)
    _patch(f"/api/network/sequences/{sequence_id}", {"steps": steps})
    return _get(f"/api/network/sequences/{sequence_id}")


@mcp.tool()
def add_email_step(
    sequence_id: str,
    subject_template: str,
    message_template: str,
    step_name: str = "Email",
    delay_days: int = 0,
    delay_hours: int = 0,
    delay_minutes: int = 0,
    ai_personalize: bool = True,
    send_condition: str = "always",
    email_account_id: str = None,
):
    """Append an email step to a sequence (preserves existing steps).

    Sets message_type='email' and requires both subject_template and
    message_template. Unlike LinkedIn steps, email doesn't need
    send_after_connection / auto_connect. `{{placeholder}}` tokens behave the
    same as LinkedIn steps.

    Returns the updated full sequence from GET."""
    import uuid
    current = _get(f"/api/network/sequences/{sequence_id}")
    steps = current.get("steps") or []
    new_step = {
        "id": str(uuid.uuid4()),
        "step_order": len(steps) + 1,
        "step_name": step_name,
        "message_type": "email",
        "message_template": message_template,
        "subject_template": subject_template,
        "send_condition": send_condition,
        "delay_days": delay_days,
        "delay_hours": delay_hours,
        "delay_minutes": delay_minutes,
        "trigger_type": "delay",
        "send_after_connection": False,
        "auto_connect_if_not_connected": False,
        "ai_personalize": ai_personalize,
        "recipient_type": None,
        "send_as_user_id": None,
        "email_account_id": email_account_id,
    }
    steps.append(new_step)
    _patch(f"/api/network/sequences/{sequence_id}", {"steps": steps})
    return _get(f"/api/network/sequences/{sequence_id}")


@mcp.tool()
def list_talent_lists():
    """List the workspace's talent lists (saved groups of talent_profile candidates).

    Returns {"lists": [{id, name, member_count, ...}]}. Talent lists are the
    way to group candidates for bulk operations — you can add members to a
    list then enroll the whole list in a sequence in one call via
    `enroll_list_in_sequence`."""
    return _get("/api/talent-lists")


@mcp.tool()
def create_talent_list(name: str):
    """Create a new (empty) talent list. Returns the created list row."""
    return _post("/api/talent-lists", {"name": name})


@mcp.tool()
def add_members_to_list(list_id: str, members: list):
    """Add talent-pool members to an existing list.

    members is a list of {"member_type": str, "member_id": str} where
    member_type is 'talent_profile' | 'outbound_candidate' | 'network_contact'
    (same vocabulary as enroll_candidates, just snake_case)."""
    return _post(f"/api/talent-lists/{list_id}/members", {"members": members})


@mcp.tool()
def enroll_list_in_sequence(
    list_id: str,
    sequence_id: str,
    exclude_already_enrolled: bool = True,
    only_with_email: bool = None,
    only_with_linkedin: bool = None,
):
    """Bulk-enroll every member of a talent list into a sequence.

    Returns {enrolled: int, skipped: int, errors: int}. Filters:
    exclude_already_enrolled skips members already in the sequence.
    only_with_email / only_with_linkedin restrict to members that have that
    channel populated (useful to match the sequence's channel)."""
    body = {
        "sequence_id": sequence_id,
        "exclude_already_enrolled": exclude_already_enrolled,
    }
    if only_with_email is not None:
        body["only_with_email"] = only_with_email
    if only_with_linkedin is not None:
        body["only_with_linkedin"] = only_with_linkedin
    return _post(
        f"/api/talent-lists/{list_id}/enroll-in-sequence", body
    )


@mcp.tool()
def import_search_results_to_sequence(
    search_candidates: list,
    sequence_id: str,
    enable_enrichment: bool = False,
):
    """Convert a list of search-result candidates into talent_profiles and
    enroll them into a sequence in a single call.

    search_candidates is the `candidates` array from a `multi_source_search`
    response. This builds a CSV in memory with columns name, linkedin_url,
    email, headline, current_title, current_company, location and posts it
    to /api/talent-pool/bulk-import with sequence_id set — so the server
    auto-creates talent_profile rows and enrolls them.

    This is the ONLY path to enroll search results, because /enroll requires
    talent_profile UUIDs and search results come back with source-prefixed
    ids (e.g. 'linkedin-foo-123') that aren't UUIDs.

    Returns {success, recordCount, failed, resumesUploaded?, warnings?}."""
    import csv, io
    buf = io.StringIO()
    cols = ["name", "linkedin_url", "email", "headline",
            "current_title", "current_company", "location"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for c in search_candidates:
        w.writerow({
            "name": c.get("name") or "",
            "linkedin_url": c.get("linkedin_url") or "",
            "email": c.get("work_email") or c.get("email") or "",
            "headline": c.get("headline") or "",
            "current_title": c.get("title") or "",
            "current_company": c.get("company") or "",
            "location": c.get("location") or c.get("country") or "",
        })
    csv_bytes = buf.getvalue().encode("utf-8")

    files = {"csv_file": ("search_results.csv", csv_bytes, "text/csv")}
    data = {"sequence_id": sequence_id}
    if enable_enrichment:
        data["enable_enrichment"] = "true"

    r = httpx.post(
        f"{BASE}/api/talent-pool/bulk-import",
        cookies=_cookies(),
        headers={
            "accept": "*/*",
            "origin": BASE,
            "referer": f"{BASE}/recruiter/search",
        },
        files=files,
        data=data,
        timeout=180.0,
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def enroll_candidates(sequence_id: str, candidates: list):
    """Bulk-enroll candidates into a sequence.

    Each item in `candidates` can include:
      - candidateType   (required) 'talent_profile' | 'outbound_candidate' | 'network_contact'
      - candidateId     (required) the row UUID (talent_profile_id for
                        talent_profile, contact id for network_contact, etc.)
      - candidateName   (optional) used for display in the sequence results
      - candidateEmail  (optional) populates the enrollment's email channel
      - linkedinUrl     (optional) populates the LinkedIn channel
      - candidateSnapshot (optional) full profile blob, used when the server
                        needs denormalized data for message rendering

    All keys are camelCase (not snake_case). The UI passes all six fields
    when enrolling; for routine re-enrollment of existing workspace records
    just type+id is enough.

    Returns {success, enrolled: int, errors: int}. Re-enrolling an already-
    enrolled candidate is idempotent (enrolled=0, errors=0).

    LIMITATION: raw search-result ids from `multi_source_search`
    ('linkedin-foo-123' etc.) are NOT talent_profile UUIDs and will always
    error here. Use `import_search_results_to_sequence` instead — it CSV-
    imports them through /api/talent-pool/bulk-import which creates the
    talent_profile rows and enrolls them atomically."""
    return _post(
        f"/api/network/sequences/{sequence_id}/enroll",
        {"candidates": candidates},
    )


@mcp.tool()
def update_sequence_step_message(
    sequence_id: str, step_id: str, message_template: str
):
    """Safely edit one step's message_template via GET → modify → PATCH.

    Fetches the current sequence, replaces the specified step's
    message_template (other fields preserved), and PATCHes back the full
    config so nothing else is lost. message_template supports {{placeholder}}
    tokens; if the step has ai_personalize=true, the personalization_prompt
    further rewrites this per-candidate at send time. Raises ValueError if
    step_id is not found."""
    current = _get(f"/api/network/sequences/{sequence_id}")
    seq = current.get("sequence") or {}
    steps = current.get("steps") or []
    found = False
    for s in steps:
        if s.get("id") == step_id:
            s["message_template"] = message_template
            found = True
            break
    if not found:
        raise ValueError(
            f"step_id {step_id} not found in sequence {sequence_id}"
        )
    body = {
        "name": seq.get("name"),
        "description": seq.get("description"),
        "conversion_goal": seq.get("conversion_goal"),
        "personalization_prompt": seq.get("personalization_prompt"),
        "settings": seq.get("settings"),
        "steps": steps,
    }
    return _patch(f"/api/network/sequences/{sequence_id}", body)


# ------------------------------------------------------------------
# Candidates, contacts, companies, team, agent
# ------------------------------------------------------------------

@mcp.tool()
def candidate_analysis(candidate_id: str):
    """Deep profile for one candidate: identity, LinkedIn, devpost, socials.

    Response (~60 KB) shape: {candidate (name, email, headline, description,
    location, skills[], experience[], education[], languages[], certifications[],
    connections_count, followers_count, is_open_to_work, is_hiring,
    job_seeking_signals, hiring_intent_signals, linkedin_activity_analysis,
    generated_blurb, match_reasoning, ...), linkedin (same fields, enriched),
    devpost (often null), social_media (often null)}."""
    return _get(f"/api/candidates/{candidate_id}/analysis")


@mcp.tool()
def list_contacts(limit: int = 20, offset: int = 0):
    """List the workspace's network contacts (pulled mostly from LinkedIn).

    Returns {"contacts": [...], "total": int, "search_intent", "enrichment_status"}.
    Contacts have 80+ fields: identity (full_name, first_name, last_name,
    email, phone, profile_url, profile_picture_url), employment
    (current_title, current_company, headline, work_experience[], education[]),
    enrichment (skills[], languages[], certifications[], bio, github_data,
    recent_posts, recent_comments, hiring_intent_signals, job_seeking_signals,
    open_to_work, auto_rating_*, activity_enriched_at, crustdata_enriched_at),
    network (network_distance, shared_connections_count, connection_date,
    last_interaction_date). Workspaces can have thousands of contacts —
    paginate via limit/offset."""
    return _get(f"/api/network/contacts?limit={limit}&offset={offset}")


@mcp.tool()
def list_companies():
    """List companies in the current workspace (multi-tenant recruiter tool).

    Returns {"companies": [{id, name, domain, website_url,
    headquarters_location, founded_year, employee_count, company_description,
    company_image, logo_url, linkedin_url, twitter_url, founder_name,
    founder_email, founder_linkedin, founder_twitter, founder_bio,
    founder_photo_url}]}. Each company id is usable as `company_id` in
    `multi_source_search`."""
    return _get("/api/companies")


@mcp.tool()
def team_members():
    """List the workspace's team members.

    Returns {"members": [{id, name, email, role (SALES|RECRUITER|...),
    isCurrentUser, linkedinAccountType, linkedinInmailCredits,
    linkedinConnected}], "currentUserId"}. Useful for finding who owns a
    sequence or chat thread."""
    return _get("/api/team/members")


@mcp.tool()
def send_now(sequence_id: str, enrollment_id: str):
    """Force-send the next step for a single enrolled candidate immediately
    (bypasses the scheduled delay).

    enrollment_id is `candidates[i].id` from `sequence_results` — the
    candidate_sequence_id, not the underlying candidate_id. POSTs
    /candidates/{id}/actions with action='execute_now'. Returns
    {success, message} or {error}. Works even if the sequence is in 'draft'
    — the UI exposes this as the "Send Now" button in the candidate drawer."""
    return _post(
        f"/api/network/sequences/{sequence_id}/candidates/{enrollment_id}/actions",
        {"action": "execute_now"},
    )


@mcp.tool()
def skip_candidate_step(sequence_id: str, enrollment_id: str):
    """Advance an enrollment past its current step without sending.
    POSTs /candidates/{id}/actions with action='advance_step'."""
    return _post(
        f"/api/network/sequences/{sequence_id}/candidates/{enrollment_id}/actions",
        {"action": "advance_step"},
    )


@mcp.tool()
def send_reply(
    sequence_id: str,
    enrollment_id: str,
    message: str,
    subject: str = None,
    channel: str = "linkedin",
):
    """Send a manual reply to a candidate (outside the automated step flow).

    channel is 'linkedin' (DM) or 'email'. subject is required for email and
    ignored for LinkedIn. POSTs /candidates/{id}/actions with
    action='send_reply'."""
    body = {"action": "send_reply", "message": message, "channel": channel}
    if subject:
        body["subject"] = subject
    return _post(
        f"/api/network/sequences/{sequence_id}/candidates/{enrollment_id}/actions",
        body,
    )


@mcp.tool()
def generate_personalization(sequence_id: str, candidate_sequence_ids: list):
    """Trigger AI drafting of personalized message(s) for one or more
    enrollments. POSTs /sequences/{id}/personalize with
    {candidateSequenceIds}. Returns {generated: int, errors: [...]}."""
    return _post(
        f"/api/network/sequences/{sequence_id}/personalize",
        {"candidateSequenceIds": candidate_sequence_ids},
    )


@mcp.tool()
def import_and_personalize(
    sequence_id: str,
    candidates: list,
    step_id: str = None,
    enable_enrichment: bool = False,
    settle_seconds: int = 20,
):
    """Import candidates AND pre-set per-candidate personalizations.

    For each candidate with `personalized_message` / `personalized_subject` fields,
    after the bulk CSV import + enrollment completes this tool PATCHes
    /personalize for every matched enrollment with isApproved=true, so when the
    sequence step fires it sends the pre-rendered message verbatim (no AI
    regeneration). Match is by email (case-insensitive).

    Returns {imported, personalized, unmatched, step_id, sequence_id}."""
    import time
    # Run the standard CSV import
    import_result = import_search_results_to_sequence.fn(
        search_candidates=candidates,
        sequence_id=sequence_id,
        enable_enrichment=enable_enrichment,
    ) if hasattr(import_search_results_to_sequence, "fn") else \
        import_search_results_to_sequence(
        search_candidates=candidates,
        sequence_id=sequence_id,
        enable_enrichment=enable_enrichment,
    )
    # Wait for server-side enrollment to settle
    time.sleep(settle_seconds)

    # Figure out step_id if not given (first step)
    if not step_id:
        seq = _get(f"/api/network/sequences/{sequence_id}")
        steps = seq.get("steps") or []
        if not steps:
            raise RuntimeError("sequence has no steps; add a step before import_and_personalize")
        step_id = steps[0]["id"]

    # Build email → personalization map from input candidates
    pers_by_email = {}
    for c in candidates:
        email = (c.get("email") or c.get("work_email") or "").lower().strip()
        msg = c.get("personalized_message")
        subj = c.get("personalized_subject")
        if email and (msg or subj):
            pers_by_email[email] = {"message": msg, "subject": subj}

    # Fetch enrollments, match by email, PATCH each
    results = server_sequence_results = _get(
        f"/api/network/sequences/{sequence_id}/results"
    )
    personalized = unmatched = 0
    per_email_enrolled = {
        (c.get("candidate_email") or "").lower().strip(): c.get("id")
        for c in server_sequence_results.get("candidates", [])
        if c.get("candidate_email")
    }
    for email, pers in pers_by_email.items():
        enrollment_id = per_email_enrolled.get(email)
        if not enrollment_id:
            unmatched += 1
            continue
        try:
            _patch(
                f"/api/network/sequences/{sequence_id}/personalize",
                {
                    "candidateSequenceId": enrollment_id,
                    "stepId": step_id,
                    "personalizedMessage": pers.get("message") or "",
                    "personalizedSubject": pers.get("subject") or "",
                    "isApproved": True,
                },
            )
            personalized += 1
        except Exception:
            unmatched += 1

    return {
        "import_result": import_result,
        "personalized": personalized,
        "unmatched": unmatched,
        "step_id": step_id,
        "sequence_id": sequence_id,
    }


@mcp.tool()
def save_personalization(
    sequence_id: str,
    personalized_message: str,
    personalization_id: str = None,
    candidate_sequence_id: str = None,
    step_id: str = None,
    personalized_subject: str = None,
    is_approved: bool = True,
):
    """Save an edited personalization draft.

    Pass either `personalization_id` (to update an existing draft) OR the
    pair `(candidate_sequence_id, step_id)` (to create one). Sets
    isApproved=True so the draft is queued for send. PATCHes
    /sequences/{id}/personalize."""
    body = {
        "personalizedMessage": personalized_message,
        "personalizedSubject": personalized_subject,
        "isApproved": is_approved,
    }
    if personalization_id:
        body["personalizationId"] = personalization_id
    else:
        if not (candidate_sequence_id and step_id):
            raise ValueError(
                "must pass personalization_id OR both candidate_sequence_id and step_id"
            )
        body["candidateSequenceId"] = candidate_sequence_id
        body["stepId"] = step_id
    return _patch(f"/api/network/sequences/{sequence_id}/personalize", body)


@mcp.tool()
def linkedin_accounts():
    """List connected LinkedIn accounts for the workspace.

    Returns {"accounts": [{id, provider, connection_status, user_id, user:
    {id, name, email}, account_email, account_name, profile_url,
    contacts_synced_count, last_connected_at, last_sync_at, ...}], total}.
    Each account is tied to a specific `user_id` — that's the mapping used
    by the sending engine when a step has `send_as_user_id = null` and
    falls back to the sequence owner."""
    return _get("/api/network/accounts")


@mcp.tool()
def find_candidate_in_sequences(name_substring: str):
    """Search all sequences for enrolled candidates whose name matches the
    given substring (case-insensitive). Useful for "which sequence is X in?"

    Returns a list of {sequence_name, sequence_id, sequence_status,
    candidate_name, candidate_sequence_id, status, enrolled_at,
    completed_at, replied_at, linkedin_url} — one row per match, across
    all sequences."""
    needle = name_substring.lower().strip()
    hits = []
    for s in _get("/api/network/sequences").get("sequences", []):
        r = _get(f"/api/network/sequences/{s['id']}/results")
        for c in r.get("candidates", []):
            if needle in (c.get("candidate_name") or "").lower():
                hits.append({
                    "sequence_name": s["name"],
                    "sequence_id": s["id"],
                    "sequence_status": s["status"],
                    "candidate_name": c.get("candidate_name"),
                    "candidate_sequence_id": c.get("id"),
                    "candidate_id": c.get("candidate_id"),
                    "status": c.get("status"),
                    "enrolled_at": c.get("enrolled_at"),
                    "completed_at": c.get("completed_at"),
                    "replied_at": c.get("replied_at"),
                    "linkedin_url": c.get("linkedin_url"),
                })
    return hits


@mcp.tool()
def agent_runs():
    """List recent recruiter-agent runs.

    Returns {"runs": [...]}. This is the history of autonomous agent
    executions (separate from AI chat conversations). Often empty for
    workspaces that haven't kicked off agent runs yet."""
    return _get("/api/recruiter/agent-runs")


if __name__ == "__main__":
    mcp.run()
