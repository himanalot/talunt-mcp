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

# --- UC campus → OpenAlex institution ID ---
UC_OPENALEX_IDS = {
    "ucb":     "I95457486",
    "ucla":    "I161318765",
    "ucsd":    "I36258959",
    "ucsf":    "I204250578",
    "ucdavis": "I84171931",
    "uci":     "I204983920",
    "ucsb":    "I138006243",
    "ucsc":    "I94285222",
    "ucr":     "I204465549",
    "ucm":     "I114027177",
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


# ------------------- ORCID -------------------

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

def _best_email(emails: list[str], uc_slug: str, author_name: str = "") -> str | None:
    """Pick the most likely author email.

    Priority:
      1. Institution domain + last-name token in local-part (e.g. 'eisen@berkeley.edu')
      2. Any domain + last-name token in local-part
      3. Institution domain match
      4. Any UC-system domain
      5. First email
    Last-name heuristic reduces co-author false-positives (e.g. searching 'Banfield'
    would otherwise pick up 'doudna-cate@berkeley.edu' from a co-authored paper)."""
    if not emails:
        return None
    dom = UC_DOMAINS.get(uc_slug, "").lower()
    last = author_name.split()[-1].lower().strip(".") if author_name else ""

    def local_has_last(e):
        if not last or len(last) < 3:
            return False
        local = e.split("@", 1)[0].lower()
        return last in local

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
