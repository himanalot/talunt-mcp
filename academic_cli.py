#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""
academic — find academic researchers via OpenAlex + PubMed + ORCID.

Designed to complement talunt cli.py: the `pipeline` subcommand outputs JSON
in the same shape Talunt's `import-search` consumes, so academic contacts
can be piped directly into a sequence:

  acad pipeline ucb --topic "genomics" --limit 100 > /tmp/berkeley_genomics.json
  talunt import-search <seq-id> --from /tmp/berkeley_genomics.json

Subcommands:
  list-ucs                   - show known UC campuses + OpenAlex institution IDs
  find-topic <query>         - search OpenAlex topics (returns topic IDs to pass via --topic)
  authors <uc> [--topic ID]  - list authors at a UC, optionally filtered by topic
  emails <name>              - PubMed search for author's recent papers, regex emails
  orcid                      - ORCID expanded-search by institution + keyword
  ucsf-profiles <query>      - UCSF structured faculty search (emails included)
  pipeline <uc>              - end-to-end: OpenAlex authors + PubMed email enrichment
                               Outputs Talunt import-search-compatible JSON.

Polite-pool settings: all requests include contact email for higher rate limits.
"""
from __future__ import annotations
import argparse, json, re, sys, time, concurrent.futures, urllib.parse
import httpx

CONTACT_EMAIL = "ishanramrakhiani@gmail.com"

# --- UC campus → OpenAlex institution ID (verified via api.openalex.org/institutions?search=) ---
UC_OPENALEX_IDS = {
    "ucb":     "I95457486",    # University of California, Berkeley
    "ucla":    "I161318765",   # University of California, Los Angeles
    "ucsd":    "I36258959",    # University of California, San Diego
    "ucsf":    "I180670191",   # University of California, San Francisco
    "ucdavis": "I84218800",    # University of California, Davis
    "uci":     "I204250578",   # University of California, Irvine
    "ucsb":    "I154570441",   # University of California, Santa Barbara
    "ucsc":    "I185103710",   # University of California, Santa Cruz
    "ucr":     "I103635307",   # University of California, Riverside
    "ucm":     "I156087764",   # University of California, Merced
}
UC_NAMES = {
    "ucb":     "University of California, Berkeley",
    "ucla":    "University of California, Los Angeles",
    "ucsd":    "University of California, San Diego",
    "ucsf":    "University of California, San Francisco",
    "ucdavis": "University of California, Davis",
    "uci":     "University of California, Irvine",
    "ucsb":    "University of California, Santa Barbara",
    "ucsc":    "University of California, Santa Cruz",
    "ucr":     "University of California, Riverside",
    "ucm":     "University of California, Merced",
}
UC_DOMAINS = {
    "ucb": "berkeley.edu", "ucla": "ucla.edu", "ucsd": "ucsd.edu",
    "ucsf": "ucsf.edu", "ucdavis": "ucdavis.edu", "uci": "uci.edu",
    "ucsb": "ucsb.edu", "ucsc": "ucsc.edu", "ucr": "ucr.edu", "ucm": "ucmerced.edu",
}

# Bonus non-UC schools (added because the earlier build had wrong OpenAlex IDs
# pointing here; rather than discard the data we keep them as first-class slugs).
UC_OPENALEX_IDS.update({
    "arizona": "I138006243",
    "washu":   "I204465549",
    "unc":     "I114027177",
})
UC_NAMES.update({
    "arizona": "University of Arizona",
    "washu":   "Washington University in St. Louis",
    "unc":     "University of North Carolina at Chapel Hill",
})
UC_DOMAINS.update({
    "arizona": "arizona.edu",
    "washu":   "wustl.edu",
    "unc":     "unc.edu",
})


def out(obj):
    print(json.dumps(obj, indent=2, default=str))


# ------------------- OpenAlex -------------------

def openalex_find_topic(query: str, limit: int = 10):
    r = httpx.get(
        "https://api.openalex.org/topics",
        params={"search": query, "per-page": limit, "mailto": CONTACT_EMAIL},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return [
        {"id": t["id"].split("/")[-1], "display_name": t.get("display_name"),
         "works_count": t.get("works_count"), "description": t.get("description")}
        for t in results
    ]


def openalex_authors(institution_id: str, topic_id: str | None = None,
                     search: str | None = None, limit: int = 100):
    filters = [f"last_known_institutions.id:{institution_id}"]
    if topic_id:
        filters.append(f"topics.id:{topic_id}")
    out_list = []
    cursor = "*"
    per_page = min(200, max(25, limit))
    while len(out_list) < limit:
        params = {
            "filter": ",".join(filters),
            "per-page": per_page,
            "cursor": cursor,
            "mailto": CONTACT_EMAIL,
            "sort": "cited_by_count:desc",
        }
        if search:
            params["search"] = search
        r = httpx.get("https://api.openalex.org/authors", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out_list.extend(data.get("results", []))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out_list[:limit]


# ------------------- PubMed -------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

def _pubmed_author_term(full_name: str) -> str:
    """Convert 'Michael B. Eisen' → 'Eisen MB' (PubMed author-index format)."""
    tokens = [t.strip().rstrip(".") for t in full_name.split() if t.strip()]
    if len(tokens) < 2:
        return full_name
    last = tokens[-1]
    initials = "".join(t[0].upper() for t in tokens[:-1] if t)
    return f"{last} {initials}"


def pubmed_author_emails(author_name: str, limit_papers: int = 25,
                        orcid: str | None = None):
    """Search PubMed by author (preferring ORCID when given), fetch recent papers,
    regex emails out of affiliation strings. Returns sorted unique emails."""
    if orcid:
        orcid_clean = orcid.replace("https://orcid.org/", "").strip()
        term = f"{orcid_clean}[orcid]"
    else:
        term = f"{_pubmed_author_term(author_name)}[au]"

    r = httpx.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed", "term": term,
            "retmode": "json", "retmax": str(limit_papers), "sort": "pub_date",
            "tool": "talunt-mcp", "email": CONTACT_EMAIL,
        },
        timeout=30,
    )
    r.raise_for_status()
    pmids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not pmids:
        return []
    time.sleep(0.4)
    r = httpx.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
                "tool": "talunt-mcp", "email": CONTACT_EMAIL},
        timeout=60,
    )
    r.raise_for_status()
    emails = set()
    for m in EMAIL_RE.finditer(r.text):
        e = m.group(0).rstrip(".").lower()
        if e.endswith(("&amp", "&lt", "&gt")):
            continue
        emails.add(e)
    return sorted(emails)


# ------------------- ORCID profile (employments + educations) -------------------

def _orcid_clean(orcid: str | None) -> str | None:
    if not orcid: return None
    return orcid.replace("https://orcid.org/", "").strip("/") or None


def orcid_employments(orcid: str | None) -> list[dict]:
    """Fetch current + past employment records from ORCID.
    Returns list of {role_title, department, institution, start_year, end_year}."""
    o = _orcid_clean(orcid)
    if not o: return []
    try:
        r = httpx.get(f"https://pub.orcid.org/v3.0/{o}/employments",
                      headers={"Accept": "application/json"}, timeout=30)
    except httpx.HTTPError:
        return []
    if not r.is_success: return []
    data = r.json()
    out = []
    for group in data.get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            s = summary.get("employment-summary") or {}
            org = s.get("organization") or {}
            start = ((s.get("start-date") or {}).get("year") or {}).get("value")
            end = ((s.get("end-date") or {}).get("year") or {}).get("value")
            out.append({
                "role_title":  s.get("role-title"),
                "department":  s.get("department-name"),
                "institution": org.get("name"),
                "start_year":  int(start) if start else None,
                "end_year":    int(end) if end else None,
            })
    return out


def orcid_educations(orcid: str | None) -> list[dict]:
    """Fetch education records (PhD, MS, BS, MD, etc.) from ORCID."""
    o = _orcid_clean(orcid)
    if not o: return []
    try:
        r = httpx.get(f"https://pub.orcid.org/v3.0/{o}/educations",
                      headers={"Accept": "application/json"}, timeout=30)
    except httpx.HTTPError:
        return []
    if not r.is_success: return []
    data = r.json()
    out = []
    for group in data.get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            s = summary.get("education-summary") or {}
            org = s.get("organization") or {}
            start = ((s.get("start-date") or {}).get("year") or {}).get("value")
            end = ((s.get("end-date") or {}).get("year") or {}).get("value")
            out.append({
                "role_title":  s.get("role-title"),   # e.g. "PhD", "Doctor of Philosophy"
                "department":  s.get("department-name"),
                "institution": org.get("name"),
                "start_year":  int(start) if start else None,
                "end_year":    int(end) if end else None,
            })
    return out


_DOCTORATE_KEYWORDS = ("phd", "ph.d", "ph. d", "doctor of philosophy", "doctorate",
                      "d.phil", "md/phd", "md-phd", "m.d./ph.d", "doctor of medicine",
                      " md ", " md,")

def _has_completed_doctorate(educations: list[dict]) -> bool:
    import datetime
    now_year = datetime.datetime.utcnow().year
    for e in educations:
        role = (e.get("role_title") or "").lower()
        end = e.get("end_year")
        if any(k in role for k in _DOCTORATE_KEYWORDS):
            if end is None or end <= now_year:
                return True
    return False


def _has_inprogress_doctorate(educations: list[dict]) -> bool:
    import datetime
    now_year = datetime.datetime.utcnow().year
    for e in educations:
        role = (e.get("role_title") or "").lower()
        end = e.get("end_year")
        if any(k in role for k in _DOCTORATE_KEYWORDS):
            if end is not None and end > now_year:
                return True
    return False


def _current_employment(employments: list[dict]) -> dict | None:
    import datetime
    now_year = datetime.datetime.utcnow().year
    # prefer the one with no end date (still ongoing)
    for e in employments:
        if e.get("end_year") is None:
            return e
    # else most recent by end_year
    if not employments: return None
    ranked = sorted(employments,
                    key=lambda x: (x.get("end_year") or 0, x.get("start_year") or 0),
                    reverse=True)
    return ranked[0]


def infer_title_prefix(employments: list[dict], educations: list[dict],
                     h_index: int = 0) -> str | None:
    """Return 'Prof.' / 'Dr.' / 'PhD candidate' / None (→ first-name)."""
    # 1. Current employment = Professor
    cur = _current_employment(employments)
    if cur:
        role = (cur.get("role_title") or "").lower()
        if "professor" in role and "assistant professor" not in role and "associate" not in role:
            return "Prof."
        if "professor" in role:  # assistant/associate still OK as "Prof."
            return "Prof."
    # 2. Completed doctorate anywhere
    if _has_completed_doctorate(educations):
        return "Dr."
    # 3. In-progress doctorate
    if _has_inprogress_doctorate(educations):
        return "PhD candidate"
    # 4. h-index fallback
    if h_index and h_index >= 10:
        return "Dr."
    return None


# ------------------- ORCID search (existing) -------------------

def orcid_search(institution: str, keyword: str | None = None, limit: int = 100):
    q_parts = [f'affiliation-org-name:"{institution}"']
    if keyword:
        q_parts.append(f"keyword:{keyword}")
    r = httpx.get(
        "https://pub.orcid.org/v3.0/expanded-search/",
        params={"q": " AND ".join(q_parts), "rows": str(min(limit, 1000))},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("expanded-result") or []


# ------------------- OpenAlex Works (latest paper per author) -------------------

def openalex_latest_work(openalex_author_id: str | None) -> dict | None:
    """Fetch the author's most recent paper (title, venue, year, doi)."""
    if not openalex_author_id: return None
    aid = openalex_author_id.replace("https://openalex.org/", "").strip("/")
    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"authorships.author.id:{aid}",
                "sort": "publication_date:desc",
                "per-page": 1,
                "mailto": CONTACT_EMAIL,
            },
            timeout=30,
        )
    except httpx.HTTPError:
        return None
    if not r.is_success: return None
    results = (r.json() or {}).get("results") or []
    if not results: return None
    w = results[0]
    venue = (((w.get("primary_location") or {}).get("source") or {}).get("display_name"))
    return {
        "title": w.get("title") or w.get("display_name"),
        "year": (w.get("publication_date") or "")[:4] or None,
        "venue": venue,
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
        "type": w.get("type"),
        "is_preprint": (w.get("type") == "preprint"),
    }


# ------------------- Enrichment orchestrator -------------------

def enrich_academics_in_place(candidates: list[dict], workers: int = 3,
                              log=sys.stderr) -> list[dict]:
    """For each candidate, hit ORCID employments/educations + OpenAlex latest work.
    Adds: title_prefix, current_role, department, phd_completed, phd_in_progress,
    recent_paper, salutation_name.  Mutates in place and returns the list."""
    import concurrent.futures, time

    def enrich_one(c):
        orcid_id = c.get("orcid")
        openalex_id = c.get("openalex_id")
        emps = orcid_employments(orcid_id) if orcid_id else []
        eds  = orcid_educations(orcid_id)  if orcid_id else []
        latest = openalex_latest_work(openalex_id) if openalex_id else None

        title = infer_title_prefix(emps, eds, h_index=c.get("h_index") or 0)
        cur = _current_employment(emps) or {}

        last_name = (c.get("name") or "").split()[-1] if c.get("name") else ""
        first_name = (c.get("name") or "").split()[0] if c.get("name") else ""
        if title in ("Prof.", "Dr.") and last_name:
            salutation = f"{title} {last_name}"
        elif title == "PhD candidate" and first_name:
            salutation = first_name
        else:
            salutation = first_name or c.get("name") or ""

        c["title_prefix"]     = title
        c["salutation_name"]  = salutation
        c["current_role"]     = cur.get("role_title")
        c["department"]       = cur.get("department") or c.get("last_known_institution_name")
        c["current_institution"] = cur.get("institution")
        c["phd_completed"]    = _has_completed_doctorate(eds)
        c["phd_in_progress"]  = _has_inprogress_doctorate(eds)
        c["recent_paper"]     = latest
        return c

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        enriched = list(ex.map(enrich_one, candidates))
    elapsed = time.time() - t0

    with_title = sum(1 for c in enriched if c.get("title_prefix"))
    with_dr    = sum(1 for c in enriched if c.get("title_prefix") == "Dr.")
    with_prof  = sum(1 for c in enriched if c.get("title_prefix") == "Prof.")
    with_phd   = sum(1 for c in enriched if c.get("title_prefix") == "PhD candidate")
    with_paper = sum(1 for c in enriched if c.get("recent_paper"))
    with_dept  = sum(1 for c in enriched
                     if c.get("department") and c.get("department") != c.get("last_known_institution_name"))
    print(f"[acad] enriched {len(enriched)} in {elapsed:.0f}s: "
          f"title={with_title} (Prof={with_prof} Dr={with_dr} PhDcand={with_phd}), "
          f"recent_paper={with_paper}, distinct_dept={with_dept}", file=log)
    return enriched


# ------------------- Template rendering -------------------

def render_message(template: str, candidate: dict, institution_fallback: str = "") -> str:
    """Render a template against a candidate using .format() placeholders.

    Available placeholders:
      {name} {first_name} {last_name} {salutation}
      {title}                — "Prof." / "Dr." / "PhD candidate" / ""
      {institution}          — display-name of their institution
      {department}           — ORCID department OR last_known_institution_name
      {current_role}         — e.g. "Professor", "Postdoctoral Scholar"
      {email}
      {h_index}
      {topic}                — their primary OpenAlex topic
      {topics}               — top-3 topics joined by ", "
      {recent_paper_title}
      {recent_paper_year}
      {recent_paper_venue}
      {recent_paper}         — "<title> (<venue> <year>)"

    Missing placeholders resolve to empty strings (not KeyError)."""
    first = (candidate.get("name") or "").split()[0] if candidate.get("name") else ""
    last  = (candidate.get("name") or "").split()[-1] if candidate.get("name") else ""
    latest = candidate.get("recent_paper") or {}
    topics = candidate.get("topics") or []
    venue = latest.get("venue") or ""
    year = latest.get("year") or ""
    recent_combined = latest.get("title", "")
    if latest.get("title") and (venue or year):
        recent_combined = f"{latest['title']}"
        if venue: recent_combined += f" ({venue}"
        if year:  recent_combined += f" {year}"
        if venue: recent_combined += ")"
    ctx = {
        "name": candidate.get("name") or "",
        "first_name": first,
        "last_name": last,
        "title": candidate.get("title_prefix") or "",
        "salutation": candidate.get("salutation_name") or first or "",
        "institution": candidate.get("company") or candidate.get("current_institution")
                       or institution_fallback or "",
        "department": candidate.get("department")
                      or candidate.get("current_institution") or institution_fallback or "",
        "current_role": candidate.get("current_role") or "",
        "email": candidate.get("email") or "",
        "h_index": candidate.get("h_index") or "",
        "topic": topics[0] if topics else "",
        "topics": ", ".join(topics[:3]),
        "recent_paper_title": latest.get("title") or "",
        "recent_paper_year": year,
        "recent_paper_venue": venue,
        "recent_paper": recent_combined,
    }
    class _SafeDict(dict):
        def __missing__(self, key): return ""
    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:
        return template


# ------------------- UCSF Profiles -------------------

def ucsf_profiles_search(query: str, limit: int = 25):
    """
    UCSF Profiles RNS structured search.  Public JSON search endpoint.
    Example query: 'genomics', 'bioinformatics', 'crispr'.
    """
    r = httpx.get(
        "https://api.profiles.ucsf.edu/search",
        params={"q": query, "ps": str(limit), "fmt": "json"},
        timeout=30,
    )
    if not r.is_success:
        return []
    try:
        d = r.json()
    except Exception:
        return []
    return d.get("results") or d.get("items") or []


# ------------------- Pipeline -------------------

def _alnum_lower(s: str) -> str:
    """lowercase + strip everything except [a-z0-9]. Handles hyphenated names
    ('Coleman-Derr'), unicode hyphens ('‐'), apostrophes ('O'Brien'), accents."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s.lower() if ch.isalnum() and ord(ch) < 128)


def _best_email(emails: list[str], uc_slug: str, author_name: str = "") -> str | None:
    """Pick the most likely author email.

    Priority:
      1. Institution domain + last-name token in local-part (e.g. 'eisen@berkeley.edu')
      2. Any domain + last-name token in local-part (gmail, subdomain, moved institution)
      3. Institution domain match only (risky — could be co-author at same school)
      4. Any UC-system domain
      5. First email

    Last-name matching is normalized: hyphens/accents/apostrophes are stripped from
    both the last name and the email local-part before comparing, so 'Coleman-Derr'
    matches 'colemanderr@...', 'O'Brien' matches 'obrien@...', etc."""
    if not emails:
        return None
    dom = UC_DOMAINS.get(uc_slug, "").lower()
    last_raw = author_name.split()[-1].rstrip(".") if author_name else ""
    last_norm = _alnum_lower(last_raw)

    def local_has_last(e):
        if not last_norm or len(last_norm) < 4:
            return False
        local_norm = _alnum_lower(e.split("@", 1)[0])
        return last_norm in local_norm

    # 1. inst domain + last name match
    for e in emails:
        if dom and e.endswith("@" + dom) and local_has_last(e):
            return e
    # 2. any domain + last name match
    for e in emails:
        if local_has_last(e):
            return e
    # 3. institution domain only
    for e in emails:
        if dom and e.endswith("@" + dom):
            return e
    # 4. any UC-system domain
    for e in emails:
        if any(e.endswith("@" + d) for d in UC_DOMAINS.values()):
            return e
    # 5. first
    return emails[0]


def pipeline(uc_slug: str, topic_query: str | None = None, limit: int = 100,
             email_workers: int = 1, papers_per_author: int = 25,
             skip_emails: bool = False, log=sys.stderr):
    institution_id = UC_OPENALEX_IDS[uc_slug]
    institution_name = UC_NAMES[uc_slug]
    topic_id = None
    topic_name = None
    if topic_query:
        topics = openalex_find_topic(topic_query, limit=3)
        if topics:
            topic_id = topics[0]["id"]
            topic_name = topics[0]["display_name"]
            print(f"[acad] matched topic: {topic_name} ({topic_id})", file=log)
        else:
            print(f"[acad] no OpenAlex topic matched {topic_query!r}; running unfiltered", file=log)

    authors = openalex_authors(institution_id, topic_id=topic_id, limit=limit)
    print(f"[acad] {institution_name}: {len(authors)} authors from OpenAlex"
          + (f" (topic: {topic_name})" if topic_name else ""), file=log)

    def enrich(author):
        name = author.get("display_name", "") or ""
        orcid_url = author.get("orcid")
        orcid_id = orcid_url.split("/")[-1] if orcid_url else None
        works_count = author.get("works_count", 0) or 0
        cited = author.get("cited_by_count", 0) or 0
        hindex = ((author.get("summary_stats") or {}).get("h_index")) or 0
        topics = [t.get("display_name") for t in (author.get("topics") or [])][:3]
        last_inst = ((author.get("last_known_institutions") or [{}])[0]
                     if author.get("last_known_institutions") else {})

        emails = []
        if name and not skip_emails:
            try:
                emails = pubmed_author_emails(name, limit_papers=papers_per_author,
                                              orcid=orcid_id)
            except Exception:
                emails = []

        best = _best_email(emails, uc_slug, author_name=name)
        primary_topic = topics[0] if topics else "Researcher"
        headline = f"{primary_topic} at {institution_name}"
        if hindex:
            headline += f" · h-index {hindex}"

        return {
            # Talunt import-search fields
            "id": f"openalex-{(author.get('id') or '').split('/')[-1]}",
            "name": name,
            "email": best,
            "linkedin_url": None,
            "headline": headline,
            "title": "Researcher",
            "company": institution_name,
            "location": institution_name.split(",")[-1].strip(),
            "source": "openalex",
            # extra metadata (Talunt import ignores these, but they're useful for you)
            "orcid": orcid_id,
            "orcid_url": orcid_url,
            "all_emails": emails,
            "works_count": works_count,
            "cited_by_count": cited,
            "h_index": hindex,
            "topics": topics,
            "openalex_id": (author.get("id") or "").split("/")[-1],
            "last_known_institution_name": last_inst.get("display_name"),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=email_workers) as ex:
        candidates = list(ex.map(enrich, authors))

    with_email = sum(1 for c in candidates if c.get("email"))
    print(f"[acad] enriched: {with_email}/{len(candidates)} have an email", file=log)

    return {
        "query": f"{topic_name or topic_query or 'researchers'} at {institution_name}",
        "uc": uc_slug,
        "topic_id": topic_id,
        "topic_name": topic_name,
        "total": len(candidates),
        "with_email": with_email,
        "candidates": candidates,
    }


# ------------------- CLI -------------------

def main():
    p = argparse.ArgumentParser(prog="acad",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-ucs", help="list known UC campuses + OpenAlex IDs")
    s.set_defaults(fn=lambda a: out({
        k: {"openalex_id": UC_OPENALEX_IDS[k], "name": UC_NAMES[k], "domain": UC_DOMAINS[k]}
        for k in UC_OPENALEX_IDS
    }))

    s = sub.add_parser("find-topic", help="search OpenAlex topics (returns topic IDs)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=lambda a: out(openalex_find_topic(a.query, a.limit)))

    s = sub.add_parser("authors",
                       help="list OpenAlex authors at a UC institution, sorted by citations")
    s.add_argument("uc", choices=list(UC_OPENALEX_IDS))
    s.add_argument("--topic", help="OpenAlex topic ID, e.g. T10434")
    s.add_argument("--search", help="free-text search within the institution's authors")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(fn=lambda a: out(openalex_authors(
        UC_OPENALEX_IDS[a.uc], topic_id=a.topic, search=a.search, limit=a.limit)))

    s = sub.add_parser("emails", help="fetch emails for one author via recent PubMed papers")
    s.add_argument("name", help='author full name, e.g. "Jennifer Doudna"')
    s.add_argument("--papers", type=int, default=25)
    s.add_argument("--orcid", help="author ORCID (improves precision — no name ambiguity)")
    s.set_defaults(fn=lambda a: out({
        "author": a.name,
        "orcid": a.orcid,
        "emails": pubmed_author_emails(a.name, a.papers, orcid=a.orcid),
    }))

    s = sub.add_parser("orcid", help="ORCID expanded-search by institution + keyword")
    s.add_argument("--institution", required=True,
                   help='e.g. "University of California, Berkeley"')
    s.add_argument("--keyword", default=None)
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(fn=lambda a: out(orcid_search(a.institution, a.keyword, a.limit)))

    s = sub.add_parser("ucsf-profiles", help="UCSF structured faculty search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=lambda a: out(ucsf_profiles_search(a.query, a.limit)))

    # enrich-academics: adds title/dept/recent_paper to existing pipeline JSON in place
    s = sub.add_parser("enrich-academics",
                       help="adds ORCID employments + OpenAlex latest work to an "
                            "existing JSON file in place, including title_prefix, "
                            "department, current_role, recent_paper, salutation_name")
    s.add_argument("file", help="JSON file from `acad pipeline` (mutated in place)")
    s.add_argument("--workers", type=int, default=3,
                   help="parallel HTTP workers (ORCID allows ~24/s, OpenAlex ~100/s)")
    def _cmd_enrich(a):
        with open(a.file) as f: data = json.load(f)
        data["candidates"] = enrich_academics_in_place(data.get("candidates", []),
                                                        workers=a.workers)
        with open(a.file, "w") as f: json.dump(data, f, default=str)
        out({
            "file": a.file,
            "enriched": len(data["candidates"]),
            "with_title":       sum(1 for c in data["candidates"] if c.get("title_prefix")),
            "with_recent_paper":sum(1 for c in data["candidates"] if c.get("recent_paper")),
        })
    s.set_defaults(fn=_cmd_enrich)

    # render-messages: fills personalized_subject/personalized_message per candidate
    s = sub.add_parser("render-messages",
                       help="renders per-candidate personalized_subject and "
                            "personalized_message using {placeholder} templates. "
                            "Run after enrich-academics.")
    s.add_argument("file", help="JSON file (mutated in place)")
    s.add_argument("--subject", required=True,
                   help="subject template, e.g. 'Question about your {recent_paper_title}'")
    s.add_argument("--message", required=False,
                   help="inline message template (prefer --message-file for multi-line)")
    s.add_argument("--message-file",
                   help="path to a .txt file containing the body template "
                        "(use {placeholders} — see docstring on render_message)")
    def _cmd_render(a):
        if not a.message and not a.message_file:
            print("must pass --message or --message-file", file=sys.stderr); sys.exit(2)
        body_tpl = a.message or open(a.message_file).read()
        subj_tpl = a.subject
        with open(a.file) as f: data = json.load(f)
        # institution fallback from query
        inst = data.get("query","").split(" at ")[-1] if " at " in data.get("query","") else ""
        rendered = 0
        for c in data.get("candidates", []):
            c["personalized_subject"] = render_message(subj_tpl, c, inst)
            c["personalized_message"] = render_message(body_tpl, c, inst)
            if c["personalized_message"]: rendered += 1
        with open(a.file, "w") as f: json.dump(data, f, default=str)
        out({"file": a.file, "rendered": rendered, "total": len(data.get("candidates",[]))})
    s.set_defaults(fn=_cmd_render)

    s = sub.add_parser("pipeline",
                       help="end-to-end: OpenAlex authors + PubMed emails, "
                            "Talunt import-search-compatible JSON")
    s.add_argument("uc", choices=list(UC_OPENALEX_IDS))
    s.add_argument("--topic", help="topic query, e.g. 'genomics'")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--papers-per-author", type=int, default=25,
                   help="how many recent PubMed papers to scan per author for emails")
    s.add_argument("--email-workers", type=int, default=1,
                   help="parallel PubMed calls. Default 1 (sequential) because "
                        "PubMed silently returns empty at 3+/s without an API key. "
                        "Bump to 2-3 only if you have an NCBI API key set.")
    s.add_argument("--skip-emails", action="store_true",
                   help="skip PubMed enrichment entirely (faster, no emails)")
    s.set_defaults(fn=lambda a: out(pipeline(
        a.uc, a.topic, a.limit,
        email_workers=a.email_workers,
        papers_per_author=a.papers_per_author,
        skip_emails=a.skip_emails,
    )))

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
