# website_evolution.py
#
# "Website Evolution" — a standalone, isolated feature.
# Given an exact domain, samples Common Crawl's historical index across
# time to detect how that domain's own pages have changed.
#
# ISOLATION GUARANTEE: this file does not import from app.py at module
# level, is not imported by app.py yet (that happens in Chunk 3), and
# has zero connection to mcp_server.py or mcp_keys_db.py. It can be
# deleted entirely with no effect on anything else in Signalwatch.
#
# WHY SAMPLING, NOT "NEWEST SHARD, STOP EARLY":
# An earlier design fetched only the newest crawl shard and stopped once
# it hit a URL cap. For a large, frequently-crawled domain (e.g. a
# company homepage with hundreds of pages), that exhausts the cap on
# whatever CDX happens to return first from the MOST RECENT crawl —
# meaning you never see older, more interesting history at all. This
# version instead takes a few snapshots spread across many months, so
# you get breadth across TIME, not just depth in one moment.

import requests
from datetime import datetime, timedelta
import gzip
import io
import re as _re

# A real domain looks like word characters, dots, hyphens — nothing
# else. This isn't meant to be a fully RFC-compliant domain validator,
# just a practical filter that rejects anything with characters that
# have no business being in a domain name (quotes, angle brackets,
# semicolons, whitespace) before it ever reaches a network call.
_DOMAIN_PATTERN = _re.compile(r'^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+$')

# Common Crawl publishes crawl "shards" with IDs like CC-MAIN-2024-33.
# There is no simple "give me a shard from N months ago" API — instead,
# Common Crawl publishes a JSON file listing every crawl ID that exists,
# each with an actual completion date. We fetch that list once per
# search and pick shards close to our target months-back offsets.
CRAWL_INDEX_LIST_URL = "https://index.commoncrawl.org/collinfo.json"

def is_valid_domain(domain: str) -> bool:
    """
    Rejects anything that isn't a plausible domain BEFORE it reaches
    any network call or gets echoed back to the frontend. This is the
    server-side half of injection defense — the frontend's escapeHtml()
    is the other half, defending the display side. Both matter:
    this stops garbage from ever being processed; escapeHtml() stops
    it from ever being rendered unsafely if it somehow got through.
    """
    if not domain or len(domain) > 253:
        return False
    return bool(_DOMAIN_PATTERN.match(domain))

def get_available_crawls() -> list:
    """
    Fetches the list of all Common Crawl crawl shards Common Crawl
    currently has indexed, newest first. Each entry looks like:
      {"id": "CC-MAIN-2024-33", "name": "...", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-33-index"}

    Returns an empty list on any failure — callers must handle that
    gracefully (this whole feature should degrade to "no data found",
    never crash the rest of the app).
    """
    try:
        res = requests.get(
            CRAWL_INDEX_LIST_URL,
            timeout=10,
            headers={"User-Agent": "signalwatch-website-evolution/1.0"}
        )
        if res.status_code != 200:
            print(f"WebsiteEvolution: collinfo.json returned {res.status_code}")
            return []
        return res.json()
    except Exception as e:
        print(f"WebsiteEvolution: could not fetch crawl list: {e}")
        return []


def pick_sampled_shards(all_crawls: list, months_back: int, shard_interval_months: int) -> list:
    """
    Picks a genuine spread of shards across calendar time, by parsing
    each shard's REAL date from its ID (format: CC-MAIN-YYYY-WW, where
    WW is an ISO week number) rather than guessing from list position.

    Earlier version assumed "shards are evenly one-per-month in list
    order" — that assumption was wrong (confirmed by testing: 12
    "different" shards all landed within the same week). Parsing the
    actual year+week from each ID fixes this properly.
    """
    import datetime as dt

    def parse_shard_date(shard_id: str):
        # Expected format: "CC-MAIN-2024-33" -> year 2024, ISO week 33
        try:
            parts = shard_id.split("-")
            year = int(parts[2])
            week = int(parts[3])
            # %G-%V-%u parses ISO year+week+weekday; weekday 1 = Monday,
            # giving us a real, comparable date for this shard
            return dt.datetime.strptime(f"{year}-{week}-1", "%G-%V-%u")
        except Exception:
            return None

    # Attach a real parsed date to every shard, drop any we can't parse
    dated = []
    for shard in all_crawls:
        d = parse_shard_date(shard.get("id", ""))
        if d:
            dated.append((d, shard))

    if not dated:
        return []

    newest_date = dated[0][0]  # collinfo.json is already newest-first
    cutoff_date = newest_date - dt.timedelta(days=months_back * 30)

    # Only consider shards within our target window
    in_range = [(d, s) for d, s in dated if d >= cutoff_date]

    # Now pick shards spaced by REAL months, not list position.
    # Walk oldest-to-newest within range, taking one shard each time
    # we cross another shard_interval_months boundary.
    in_range.sort(key=lambda pair: pair[0])  # oldest first for this walk

    picked = []
    last_picked_date = None
    for d, shard in in_range:
        if last_picked_date is None or (d - last_picked_date).days >= (shard_interval_months * 30):
            picked.append(shard)
            last_picked_date = d

    print(f"WebsiteEvolution: picked {len(picked)} shards spanning "
          f"{in_range[0][0].date() if in_range else 'n/a'} to "
          f"{in_range[-1][0].date() if in_range else 'n/a'}")

    return picked


def query_cdx_shard(domain: str, shard: dict, per_shard_cap: int) -> list:
    """
    Queries ONE Common Crawl shard's CDX index for every URL under
    `domain`, capped at `per_shard_cap` rows returned from this shard.

    Returns a list of dicts, each a raw CDX record:
      {url, timestamp, digest, filename, offset, length, shard_id}

    On any failure for this one shard, returns [] — one bad shard must
    never stop the others from being tried.
    """
    cdx_url = shard.get("cdx-api", "")
    shard_id = shard.get("id", "unknown")

    if not cdx_url:
        return []

    try:
        res = requests.get(
            cdx_url,
            params={
                "url": domain,
                "matchType": "domain",
                "output": "json",
                # CDX's own "limit" parameter caps rows server-side —
                # cheaper than fetching everything and truncating locally
                "limit": per_shard_cap
            },
            timeout=15,
            headers={"User-Agent": "signalwatch-website-evolution/1.0"}
        )

        if res.status_code != 200:
            # A 404 here usually just means this shard has no records
            # at all for this domain — not an error worth logging loudly
            if res.status_code != 404:
                print(f"WebsiteEvolution: shard {shard_id} returned {res.status_code}")
            return []

        # CDX JSON output is NEWLINE-DELIMITED JSON, not a single JSON
        # array — one JSON object per line. This trips people up the
        # first time; res.json() would fail here.
        records = []
        for line in res.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                import json
                row = json.loads(line)
            except Exception:
                continue  # skip malformed lines rather than failing the whole shard

            records.append({
                "url":       row.get("url", ""),
                "timestamp": row.get("timestamp", ""),  # format: YYYYMMDDHHMMSS
                "digest":    row.get("digest", ""),
                "filename":  row.get("filename", ""),
                "offset":    row.get("offset", ""),
                "length":    row.get("length", ""),
                "shard_id":  shard_id
            })

        return records

    except Exception as e:
        print(f"WebsiteEvolution: shard {shard_id} query failed: {e}")
        return []


def query_cdx_sampled(domain: str, months_back: int = 24, shard_interval_months: int = 2, per_shard_cap: int = 10) -> list:
    """
    The main Stage 1 entry point. Samples several shards spread across
    `months_back` months, `shard_interval_months` apart, capping each
    shard's contribution at `per_shard_cap` rows.

    Returns the combined raw CDX rows from all sampled shards, UNDEDUPED
    — deduping happens in dedup_snapshots(), a separate, testable step.
    """
    all_crawls = get_available_crawls()
    if not all_crawls:
        print("WebsiteEvolution: no crawl list available, returning empty")
        return []

    sampled_shards = pick_sampled_shards(all_crawls, months_back, shard_interval_months)
    print(f"WebsiteEvolution: sampling {len(sampled_shards)} shards for {domain}")

    all_records = []
    for shard in sampled_shards:
        records = query_cdx_shard(domain, shard, per_shard_cap)
        all_records += records

    print(f"WebsiteEvolution: {len(all_records)} raw records found for {domain} across {len(sampled_shards)} shards")
    return all_records


def dedup_snapshots(records: list) -> list:
    """
    Stage 2. Collapses records that are the exact same content, using
    the `digest` field CDX already provides (a content hash) — this
    happens BEFORE any network fetch, so repeat crawls of an unchanged
    page never cost us a byte-range fetch later.

    Keeping logic: for each (url, digest) pair, keep only the EARLIEST
    timestamp. Why earliest, not latest: we want to know WHEN a version
    of a page first appeared, which is what "detect changes over time"
    (Stage 5, later) actually needs — the first sighting of each
    distinct version, not the most recent re-confirmation of it.
    """
    seen = {}  # (url, digest) -> record with earliest timestamp so far

    for r in records:
        key = (r["url"], r["digest"])
        if key not in seen:
            seen[key] = r
        else:
            # Keep whichever has the earlier timestamp
            if r["timestamp"] < seen[key]["timestamp"]:
                seen[key] = r

    deduped = list(seen.values())
    print(f"WebsiteEvolution: deduped {len(records)} records down to {len(deduped)} distinct versions")
    return deduped

def fetch_snapshot_content(record: dict) -> dict:
    """
    Stage 3, single-record version. Given one deduped CDX record,
    performs the actual byte-range fetch against Common Crawl's S3-
    backed HTTP endpoint, decompresses just that segment, and extracts
    a title + short snippet from the HTML.

    This is the ONE network-expensive step in the whole pipeline —
    everything in Stage 1/2 was index-only (small JSON responses).
    This is why bounded_fetch_all() below caps how many times this
    function gets called per search.

    Returns the same record dict, with two new keys added: "title"
    and "snippet". On any failure, title/snippet are empty strings —
    callers should skip records where extraction failed, not crash.
    """
    filename = record.get("filename", "")
    offset = record.get("offset", "")
    length = record.get("length", "")

    if not filename or offset == "" or length == "":
        record["title"] = ""
        record["snippet"] = ""
        return record

    try:
        offset_int = int(offset)
        length_int = int(length)

        # The exact byte range we need, expressed as an HTTP Range
        # header — this is what makes the fetch "selective" rather
        # than a bulk download. We request ONLY these bytes, not the
        # whole WARC file (which can be a gigabyte or more).
        range_header = f"bytes={offset_int}-{offset_int + length_int - 1}"

        res = requests.get(
            f"https://data.commoncrawl.org/{filename}",
            headers={
                "Range": range_header,
                "User-Agent": "signalwatch-website-evolution/1.0"
            },
            timeout=15
        )

        # 206 = Partial Content, the correct success response for a
        # Range request. Some servers return 200 if they ignore Range
        # entirely, which would mean we got the WHOLE file — we treat
        # that as a failure for this record rather than parsing a
        # potentially huge response.
        if res.status_code != 206:
            print(f"WebsiteEvolution: byte-range fetch got status {res.status_code} for {record.get('url','')}")
            record["title"] = ""
            record["snippet"] = ""
            return record

        # WARC segments are gzip-compressed individually, so this one
        # small chunk can be decompressed on its own without needing
        # the rest of the file.
        raw_bytes = gzip.decompress(res.content)
        text = raw_bytes.decode("utf-8", errors="ignore")

        record["title"] = _extract_title(text)
        record["snippet"] = _extract_snippet(text)
        return record

    except Exception as e:
        print(f"WebsiteEvolution: fetch failed for {record.get('url','')}: {e}")
        record["title"] = ""
        record["snippet"] = ""
        return record


def _extract_title(warc_text: str) -> str:
    """
    Pulls the HTML <title> tag's text out of a decompressed WARC
    segment. Deliberately simple regex-based extraction, not a full
    HTML parser — this mirrors the same lightweight approach already
    used elsewhere in Signalwatch (e.g. RSS title extraction in
    app.py), not a new dependency or pattern.
    """
    import re
    match = re.search(r"<title[^>]*>(.*?)</title>", warc_text, re.IGNORECASE | re.DOTALL)
    if match:
        title = match.group(1).strip()
        # Collapse whitespace/newlines that sometimes appear inside <title>
        title = re.sub(r"\s+", " ", title)
        return title[:200]
    return ""


def _extract_snippet(warc_text: str) -> str:
    """
    Pulls a short plain-text snippet from the first meaningful
    paragraph-like content. Same lightweight, regex-based approach —
    strips tags rather than fully parsing the DOM. This keeps snippets
    short and comparable in size to titles from other Signalwatch
    sources, per the earlier design decision to avoid full-page text
    (which would dominate scoring if ever compared against other
    sources, and reads poorly in a UI card either way).
    """
    import re
    # Strip script/style blocks entirely first — their contents are
    # never meaningful snippet text and can be huge
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", warc_text, flags=re.IGNORECASE | re.DOTALL)
    # Strip all remaining HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:250]


def bounded_fetch_all(deduped_records: list, max_fetches: int = 30) -> list:
    """
    Stage 3 entry point. Fetches content for deduped records, up to a
    HARD cap of max_fetches — regardless of how many deduped records
    exist. This is what bounds worst-case latency and network cost
    for a search, no matter how large or densely-crawled the domain is.

    Which records get fetched when there are more than max_fetches:
    we sort by timestamp first and take an EVEN SPREAD across the full
    time range, not just the first max_fetches in whatever order they
    arrived — otherwise we'd silently re-introduce the same "all
    clustered together" problem we just fixed in Stage 1, just at a
    different stage of the pipeline.
    """
    if not deduped_records:
        return []

    if len(deduped_records) <= max_fetches:
        selected = deduped_records
    else:
        # Sort oldest to newest, then take an evenly spaced sample
        # across the full sorted list — this preserves the time-spread
        # property from Stage 1 instead of undoing it here.
        sorted_records = sorted(deduped_records, key=lambda r: r["timestamp"])
        step = len(sorted_records) / max_fetches
        selected = [sorted_records[int(i * step)] for i in range(max_fetches)]

    print(f"WebsiteEvolution: fetching content for {len(selected)} of {len(deduped_records)} deduped records")

    results = []
    for record in selected:
        fetched = fetch_snapshot_content(record)
        # Only keep records where we actually got a title — empty
        # extractions are dropped here rather than carried forward as
        # noise into Stage 4/5
        if fetched.get("title"):
            results.append(fetched)

    print(f"WebsiteEvolution: {len(results)} records had usable content extracted")
    return results

def normalize_url(url: str) -> str:
    """
    Normalizes a URL so trivially-different variants of the same page
    are treated as one page in Stage 4 grouping. Without this, the
    Stage 3 test output above would split "https://stripe.com" and
    "https://stripe.com/" into two separate, misleading "histories"
    of what is really one page.

    Deliberately conservative: only strips things we're CONFIDENT are
    equivalent (protocol casing, www. prefix, trailing slash, URL
    fragment). Does NOT strip query strings — "?page=2" is a
    genuinely different page, not a formatting quirk.
    """
    import re
    normalized = url.strip().lower()
    normalized = re.sub(r"^https?://", "", normalized)  # drop protocol
    normalized = re.sub(r"^www\.", "", normalized)       # drop www.
    normalized = normalized.rstrip("/")                   # drop trailing slash
    normalized = normalized.split("#")[0]                 # drop fragment
    return normalized


def group_and_sort_by_url(snapshots: list) -> dict:
    """
    Stage 4. Groups fetched snapshots by their NORMALIZED url, then
    sorts each group's snapshots oldest-to-newest by timestamp.

    Returns: {normalized_url: [snapshot, snapshot, ...]} — each list
    already sorted chronologically, ready for Stage 5's diffing.
    """
    groups = {}
    for snap in snapshots:
        key = normalize_url(snap["url"])
        groups.setdefault(key, []).append(snap)

    for key in groups:
        groups[key].sort(key=lambda s: s["timestamp"])

    print(f"WebsiteEvolution: grouped {len(snapshots)} snapshots into {len(groups)} distinct pages")
    return groups


def _format_timestamp(ts: str) -> str:
    """
    Converts CDX's raw timestamp format (YYYYMMDDHHMMSS) into a
    readable date string. Used throughout Stage 5's output so the
    eventual AI prompt and UI both get clean dates, not raw digit
    strings like "20241211055112".
    """
    try:
        dt = datetime.strptime(ts[:8], "%Y%m%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return ts  # fall back to raw string rather than crashing


def detect_changes(groups: dict) -> list:
    """
    Stage 5. Pure comparison logic — no AI involved yet. Detects two
    kinds of change across the grouped, sorted snapshots:

    (a) NEW PAGE: a normalized URL that only starts appearing partway
        through the sampled time range (its earliest snapshot isn't
        near the overall earliest date across all pages) — treated as
        "this section/page appears to be new".

    (b) CONTENT CHANGE: within one page's own history, consecutive
        snapshots where the title differs from the previous one —
        treated as "this page's title/content changed between these
        two dates".

    Returns a flat list of change events, each a dict with a "type"
    ("new_page" or "content_change"), the url, relevant dates, and
    old/new title where applicable. This list is what Stage 6 will
    hand to the AI to summarize — deliberately structured, not prose,
    so the AI is working from facts we've already verified, not
    inferring change from raw snapshot dumps itself.
    """
    if not groups:
        return []

    # Establish the overall earliest date seen across ALL pages, so we
    # can tell "this page's first sighting is suspiciously late" apart
    # from "this page just happens to be the one we grouped first"
    all_timestamps = [s["timestamp"] for snaps in groups.values() for s in snaps]
    overall_earliest = min(all_timestamps)

    changes = []

    for url_key, snaps in groups.items():
        if not snaps:
            continue

        first_seen = snaps[0]["timestamp"]

        # (a) New page detection: if this page's first sighting is
        # more than ~45 days after the overall earliest sampled date,
        # treat it as newly appeared rather than just "not sampled
        # earlier by chance". 45 days is a deliberate buffer — Stage 1
        # samples every ~2 months, so a page could legitimately be
        # missed by one sampling gap without actually being new.
        first_dt = datetime.strptime(first_seen[:8], "%Y%m%d")
        earliest_dt = datetime.strptime(overall_earliest[:8], "%Y%m%d")
        if (first_dt - earliest_dt).days > 45:
            changes.append({
                "type": "new_page",
                "url": snaps[0]["url"],
                "first_seen_date": _format_timestamp(first_seen),
                "title": snaps[0].get("title", "")
            })

        # (b) Content change detection: walk consecutive pairs within
        # this page's own sorted history, flag where the title changed
        # Titles that indicate a redirect/error page, not real content —
        # comparing against these produces misleading "changes" like
        # "homepage changed to say 301 Moved Permanently", which is
        # just HTTP plumbing being captured mid-redirect, not a real
        # content change worth reporting.
        # Substring match, not exact match — redirect/error pages show
        # up with slightly different title text depending on the server
        # ("301 Moved", "301 Moved Permanently", etc.), confirmed by a
        # real google.com test where "301 Moved" (not the longer exact
        # string we originally filtered) slipped through
        NOISE_PATTERNS = ["301 moved", "302 found", "403 forbidden", "404 not found"]

        for i in range(1, len(snaps)):
            prev_title = snaps[i - 1].get("title", "").strip().lower()
            curr_title = snaps[i].get("title", "").strip().lower()
            prev_is_noise = not prev_title or any(p in prev_title for p in NOISE_PATTERNS)
            curr_is_noise = not curr_title or any(p in curr_title for p in NOISE_PATTERNS)
            if prev_is_noise or curr_is_noise:
                continue  # skip redirect/error noise, not a real change
            if prev_title and curr_title and prev_title != curr_title:
                changes.append({
                    "type": "content_change",
                    "url": snaps[i]["url"],
                    "date": _format_timestamp(snaps[i]["timestamp"]),
                    "previous_date": _format_timestamp(snaps[i - 1]["timestamp"]),
                    "old_title": prev_title,
                    "new_title": curr_title
                })

    # Sort all changes chronologically for a sensible timeline order
    changes.sort(key=lambda c: c.get("date") or c.get("first_seen_date") or "")

    print(f"WebsiteEvolution: detected {len(changes)} change events")
    return changes

def summarize_evolution(changes: list, domain: str) -> dict:
    """
    Stage 6. The ONLY function in this entire file that calls into
    app.py — imported locally inside this function, not at module
    level, so this file has zero import-time dependency on app.py and
    can still be tested completely standalone (as every earlier chunk
    already was).

    Takes the structured change events from Stage 5 (facts we've
    already verified mechanically — no AI involved in detecting them)
    and asks the AI to turn them into a short, readable timeline plus
    one paragraph of overall briefing. The AI is NOT asked to find
    changes itself — only to describe changes we already found. This
    keeps hallucination risk low: it's summarizing facts, not
    inventing them.

    Returns {"timeline": [...], "briefing": "..."} — empty/honest
    values if there's nothing to summarize or the AI is unavailable.
    """
    print(f"WEBSITE_EVOLUTION_DEBUG_MARKER: function called with {len(changes)} changes for {domain}")
    if not changes:
        return {
            "timeline": [],
            "briefing": f"No significant changes were detected for {domain} in the sampled history."
        }

    # Import here, not at module top — avoids any import-order coupling
    # with app.py, and matches the exact pattern already used for
    # mcp_server.py's circular-import avoidance
    import app as sw

    # Build a compact, factual description of each change for the
    # prompt — dates and titles only, nothing the AI has to infer
    change_lines = []
    for c in changes[:20]:  # cap prompt size, same discipline as generate_insight()'s use of top results only
        if c["type"] == "new_page":
            change_lines.append(f"- New page appeared around {c['first_seen_date']}: \"{c['title']}\" ({c['url']})")
        elif c["type"] == "content_change":
            change_lines.append(
                f"- Page {c['url']} changed between {c['previous_date']} and {c['date']}: "
                f"was \"{c['old_title']}\", became \"{c['new_title']}\""
            )

    changes_text = "\n".join(change_lines)

    prompt = f"""You are analysing the historical evolution of the website {domain}, based on archived snapshots from Common Crawl.

Detected changes, in chronological order:
{changes_text}

Return a JSON object with exactly two keys: "timeline" and "briefing".

"timeline": an array of objects, each with "date", "event", and "url" keys. Copy the url exactly from the matching change above — do not invent or alter it. "event" is a short, plain-English sentence describing what changed. (e.g. "Pricing page title updated" or "New healthcare section appeared"). Base every entry strictly on the changes listed above — do not invent anything not present in the list.

"briefing": 2 to 3 sentences in plain English summarising the overall pattern of how this site evolved across the sampled period. No hedging words. No markdown.

Return only raw JSON. No markdown. No backticks. No code fences."""

    # Reuses ai_call() exactly as every other AI feature in Signalwatch
    # does — same fallback chain (OpenRouter -> Groq -> Cerebras ->
    # Mistral), same labeling convention for Render log traceability
    ai_result = sw.ai_call(prompt, max_tokens=700, allow_backup_fallback=True, label="website_evolution")
    print(f"WEBSITE_EVOLUTION_DEBUG_MARKER: ai_result is {'present' if ai_result else 'EMPTY/NONE'}, length={len(ai_result) if ai_result else 0}")

    if not ai_result:
        # Honest fallback — never fabricate a timeline if the AI is
        # unavailable, same principle generate_insight() already
        # follows for its own briefing/action fields
        return {
            "timeline": [{"date": c.get("date", c.get("first_seen_date", "")), "event": f"{c['type']} detected", "url": c.get("url", "")} for c in changes[:10]],
            "briefing": f"Detected {len(changes)} changes for {domain}, but an AI summary wasn't available this time."
        }

    # FIRST ATTEMPT: schema-validated parsing via LangChain. This is
    # what actually catches syntax errors like "Expecting ',' delimiter"
    # — the old code only detected the failure after the fact, it never
    # had a repair step.
    from insight_parser import parse_website_evolution
    parsed_result = parse_website_evolution(ai_result)
    if parsed_result:
        print("WebsiteEvolution: parsed via LangChain schema parser")
        briefing = parsed_result.get("briefing", "")
        return {
            "timeline": parsed_result.get("timeline", []),
            "briefing": sw.sanitise_briefing_output(briefing) or briefing
        }
    print("WebsiteEvolution: LangChain parser failed, trying regex fallback")

    try:
        # Reuses the same robust JSON extraction your main product
        # already relies on — strips code fences, finds the JSON object
        # even with reasoning text before/after it, handles the same
        # messy-model-output cases your briefing pipeline already
        # solved. Writing a second, thinner parser here (the original
        # version of this function) was the actual bug — this fixes it
        # by reusing the proven one instead.
        import re, json as jsonlib
        clean = ai_result.strip()
        brace = clean.find('{')
        if brace > 0:
            clean = clean[brace:]
        clean = re.sub(r'```[a-zA-Z]*\n?', '', clean)
        clean = re.sub(r'```', '', clean)
        clean = clean.strip()

        json_match = re.search(r"\{[\s\S]*\}", clean)
        if json_match:
            parsed = jsonlib.loads(json_match.group())
            timeline = parsed.get("timeline", [])
            briefing = parsed.get("briefing", "")
            return {
                "timeline": timeline,
                "briefing": sw.sanitise_briefing_output(briefing) or briefing
            }
        else:
            print(f"WebsiteEvolution: no JSON object found in AI response: {ai_result[:200]}")
    except Exception as e:
        print(f"WebsiteEvolution: AI response parse failed: {e} | raw response: {ai_result[:200]}")

    # Parsing failed — same honest-fallback principle as above
    return {
        "timeline": [{"date": c.get("date", c.get("first_seen_date", "")), "event": f"{c['type']} detected", "url": c.get("url", "")} for c in changes[:10]],
        "briefing": f"Detected {len(changes)} changes for {domain}. AI summary could not be parsed this time."
    }
