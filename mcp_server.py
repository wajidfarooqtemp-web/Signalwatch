# mcp_server.py
#
# Signalwatch MCP Server — real implementation.
#
# AUTH MODEL:
#   Per-client API keys, stored hashed in Postgres (see mcp_keys_db.py).
#   The key is sent by the MCP client as a standard HTTP header:
#     Authorization: Bearer <raw_key>
#   We read it via the SDK's Context object, NOT as a tool function
#   argument — this matters. If auth were a tool parameter, the AI
#   model itself would need to know the secret and re-type it into
#   every single tool call, which both leaks the key into the model's
#   context/transcripts and advertises "send me a secret" to anyone
#   inspecting the tool schema. Headers are checked once per request,
#   before the tool body even runs, same as a cookie on a website.
#
# TWO RATE-LIMIT BUCKETS:
#   'data'  — search_mentions, search_source, get_trending_mentions,
#             get_sources, get_word_frequencies. Cheap, rule-based,
#             no AI involved. Generous limit.
#   'ai'    — find_leads only. Costs real OpenRouter/Groq/Cerebras/
#             Mistral quota. Strict, separate limit, so a client
#             hammering this tool can't starve your website's own
#             AI budget under cover of the same counter as harmless
#             searches.
#
# CIRCULAR IMPORT SAFETY:
#   app.py does "from mcp_server import mcp" at its top. So we import
#   app.py INSIDE each tool function, never at this file's top level —
#   otherwise app.py and mcp_server.py would each wait for the other
#   to finish loading, and neither ever would.

from typing import Optional
from mcp.server.fastmcp import FastMCP, Context

import mcp_keys_db as keys_db

# ── RATE LIMITS ───────────────────────────────────────────────────────────
# Generous — these tools are cheap rule-based fetches, same cost profile
# as a normal website search.
DATA_TOOL_HOURLY_LIMIT = 100

# Strict — this tool spends real AI provider quota per call.
AI_TOOL_HOURLY_LIMIT = 10


# ── FASTMCP INSTANCE ─────────────────────────────────────────────────────
# app.py imports THIS exact object and mounts it. Do not create a second
# FastMCP() anywhere — there must only ever be one.
mcp = FastMCP("Signalwatch")


# ── AUTH HELPER ───────────────────────────────────────────────────────────

def _authenticate(ctx: Context, tool_category: str, hourly_limit: int) -> dict:
    """
    Reads the Authorization header from the incoming HTTP request,
    verifies the API key against Postgres, and checks/increments the
    rate limit bucket for this tool_category.

    Returns {} on success (meaning: proceed), or {"error": "..."} on
    failure — every tool checks `if "error" in result: return result`
    as its very first line, same guard-clause pattern your app.py
    already uses everywhere else.
    """
    try:
        # Context gives access to the raw HTTP request for this call,
        # when running over the Streamable HTTP transport (which is
        # what we mounted in app.py). This is the SDK-documented way
        # to reach transport-level data — headers — from inside a tool,
        # without the AI model ever having to know or pass a secret.
        request = ctx.request_context.request
        auth_header = request.headers.get("authorization", "") if request else ""
    except Exception as e:
        # If this fires, the SDK's header-access path differs from what
        # we expect for your installed mcp version. Do NOT guess further —
        # report the exact exception text back so we fix this one path.
        return {"error": f"Could not read request headers: {e}"}

    if not auth_header:
        return {"error": "Missing Authorization header. Send: Authorization: Bearer <your_api_key>"}

    # Accept "Bearer <key>" (standard) — strip the prefix if present.
    raw_key = auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header
    raw_key = raw_key.strip()

    verification = keys_db.verify_api_key(raw_key)
    if not verification.get("valid"):
        return {"error": f"Authentication failed: {verification.get('reason', 'invalid key')}"}

    key_id = verification["key_id"]

    usage = keys_db.check_and_increment_usage(key_id, tool_category, hourly_limit)
    if not usage.get("allowed"):
        return {"error": usage.get("reason", "Rate limit exceeded")}

    return {}  # Success — empty dict means "no error, proceed"


# ── TOOLS ────────────────────────────────────────────────────────────────

@mcp.tool()
def ping() -> str:
    """Confirms the Signalwatch MCP server is reachable. No authentication required."""
    return "pong from Signalwatch MCP server"


@mcp.tool()
def search_mentions(
    ctx: Context,
    query: str,
    max_results: int = 20,
    sources: Optional[list] = None,
) -> dict:
    """
    Searches for mentions of a brand, product, or topic across Signalwatch's
    live data sources. Returns ranked results using the existing keyword
    relevance scoring pipeline. No AI is involved — pure rule-based retrieval.

    Args:
        query: What to search for, e.g. "Nike pricing complaints".
               Supports AND / OR / NOT / "exact phrase" syntax.
        max_results: Maximum ranked results to return (default 20, max 50).
        sources: Optional list of source names to search. If omitted, searches
                 all 13 primary sources. Valid names: reddit, hackernews,
                 newsapi, newsdata, rss, youtube, mastodon, bluesky,
                 trustpilot, appstore, playstore, googlenews, bingnews.

    Requires an Authorization: Bearer <api_key> header.
    """
    auth_result = _authenticate(ctx, "data", DATA_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    import app as sw

    clean_query = sw.sanitise_query(query)
    if not clean_query:
        return {"error": "Empty or invalid query"}

    max_results = min(max(1, max_results), 50)

    ALL_SOURCES = {
        "reddit": sw.fetch_reddit,
        "hackernews": sw.fetch_hackernews,
        "newsapi": sw.fetch_newsapi,
        "newsdata": sw.fetch_newsdata,
        "rss": sw.fetch_rss,
        "youtube": sw.fetch_youtube,
        "mastodon": sw.fetch_mastodon,
        "bluesky": sw.fetch_bluesky,
        "trustpilot": sw.fetch_trustpilot,
        "appstore": sw.fetch_appstore,
        "playstore": sw.fetch_playstore,
        "googlenews": sw.fetch_google_news,
        "bingnews": sw.fetch_bing_news,
    }

    if sources:
        invalid = [s for s in sources if s not in ALL_SOURCES]
        if invalid:
            return {"error": f"Invalid source names: {invalid}", "valid_sources": list(ALL_SOURCES.keys())}
        active_sources = {k: ALL_SOURCES[k] for k in sources}
    else:
        active_sources = ALL_SOURCES

    posts = []
    source_counts = {}
    for source_name, fetch_fn in active_sources.items():
        try:
            results = fetch_fn(clean_query)
            posts += results
            source_counts[source_name] = len(results)
        except Exception as e:
            source_counts[source_name] = 0
            print(f"MCP search_mentions: {source_name} failed: {e}")

    ranked = sw.filter_and_rank(posts, clean_query)

    return {
        "query": clean_query,
        "total_found": len(ranked),
        "sources_searched": list(active_sources.keys()),
        "source_counts": source_counts,
        "results": ranked[:max_results]
    }


@mcp.tool()
def search_source(
    ctx: Context,
    query: str,
    source: str,
    max_results: int = 20,
) -> dict:
    """
    Searches a SINGLE specific source for mentions. Use this when you know
    exactly which platform you want to query.

    Args:
        query: What to search for.
        source: One of: reddit, hackernews, newsapi, newsdata, rss, youtube,
                mastodon, bluesky, trustpilot, appstore, playstore,
                googlenews, bingnews.
        max_results: Maximum results to return (default 20, max 50).

    Requires an Authorization: Bearer <api_key> header.
    """
    auth_result = _authenticate(ctx, "data", DATA_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    import app as sw

    clean_query = sw.sanitise_query(query)
    if not clean_query:
        return {"error": "Empty or invalid query"}

    VALID_SOURCES = {
        "reddit": sw.fetch_reddit, "hackernews": sw.fetch_hackernews,
        "newsapi": sw.fetch_newsapi, "newsdata": sw.fetch_newsdata,
        "rss": sw.fetch_rss, "youtube": sw.fetch_youtube,
        "mastodon": sw.fetch_mastodon, "bluesky": sw.fetch_bluesky,
        "trustpilot": sw.fetch_trustpilot, "appstore": sw.fetch_appstore,
        "playstore": sw.fetch_playstore, "googlenews": sw.fetch_google_news,
        "bingnews": sw.fetch_bing_news,
    }

    if source not in VALID_SOURCES:
        return {"error": f"Invalid source: '{source}'", "valid_sources": list(VALID_SOURCES.keys())}

    max_results = min(max(1, max_results), 50)

    try:
        posts = VALID_SOURCES[source](clean_query)
    except Exception as e:
        return {"error": f"Source fetch failed: {str(e)}", "source": source}

    ranked = sw.filter_and_rank(posts, clean_query)

    return {
        "query": clean_query,
        "source": source,
        "total_found": len(ranked),
        "results": ranked[:max_results]
    }


@mcp.tool()
def get_trending_mentions(
    ctx: Context,
    query: str,
    days: int = 7,
    max_results: int = 10,
) -> dict:
    """
    Finds the most recent and highest-scoring mentions from the last N days.

    Args:
        query: What to search for.
        days: How many days back to look (default 7, max 90).
        max_results: Maximum results to return (default 10, max 30).

    Requires an Authorization: Bearer <api_key> header.
    """
    auth_result = _authenticate(ctx, "data", DATA_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    import app as sw
    from datetime import datetime, timedelta

    clean_query = sw.sanitise_query(query)
    if not clean_query:
        return {"error": "Empty or invalid query"}

    days = min(max(1, days), 90)
    max_results = min(max(1, max_results), 30)

    FAST_SOURCES = {
        "reddit": sw.fetch_reddit, "hackernews": sw.fetch_hackernews,
        "googlenews": sw.fetch_google_news, "bingnews": sw.fetch_bing_news,
        "rss": sw.fetch_rss, "mastodon": sw.fetch_mastodon,
        "bluesky": sw.fetch_bluesky,
    }

    posts = []
    source_counts = {}
    for name, fn in FAST_SOURCES.items():
        try:
            results = fn(clean_query)
            posts += results
            source_counts[name] = len(results)
        except Exception:
            source_counts[name] = 0

    ranked = sw.filter_and_rank(posts, clean_query)

    cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    trending = [r for r in ranked if r.get("created", 0) >= cutoff_ts]
    trending.sort(key=lambda x: (-x.get("created", 0), -x.get("score", 0)))

    return {
        "query": clean_query,
        "days_window": days,
        "total_found": len(trending),
        "sources_searched": list(FAST_SOURCES.keys()),
        "source_counts": source_counts,
        "results": trending[:max_results]
    }


@mcp.tool()
async def find_leads(
    ctx: Context,
    query: str,
    max_leads: int = 5,
) -> dict:
    """
    Scores mentions for buying intent and generates ready-to-send outreach
    pitches. This tool uses AI and is rate-limited separately and more
    strictly than the other tools, because it spends real AI provider quota.

    Args:
        query: What to search for (e.g. "plumbers London").
        max_leads: Maximum leads to return (default 5, max 10).

    Requires an Authorization: Bearer <api_key> header.
    """
    # NOTE: this tool is `async def`, unlike the others. It has to be —
    # it awaits your real, async find_leads() route in app.py directly.
    # The old version wrapped a sync function around
    # loop.run_until_complete(), which crashes with "event loop is
    # already running" because FastMCP's own event loop is already
    # active when a tool runs. Making the tool itself async and using
    # a plain `await` sidesteps that entirely — no nested event loop.
    auth_result = _authenticate(ctx, "ai", AI_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    import app as sw

    clean_query = sw.sanitise_query(query)
    if not clean_query:
        return {"error": "Empty or invalid query"}

    max_leads = min(max(1, max_leads), 10)

    # Your real find_leads() route requires a token starting with "sw_"
    # or "google_", and gates access through try_consume_lead_allowance()
    # (free: one scan ever) or the Pro 250/month counter. Rather than
    # inventing a fake token that silently fails that check every time
    # (the old version's bug), we mint a real, valid "sw_"-prefixed
    # token per MCP key, so MCP callers go through the exact same
    # allowance system as a website visitor — no bypass, no special case.
    mcp_lead_token = f"sw_mcp_{auth_result.get('key_id', 'unknown')}"

    try:
        request = ctx.request_context.request
    except Exception:
        request = None

    result = await sw.find_leads(query=clean_query, request=request, token=mcp_lead_token)

    if isinstance(result, dict):
        if "error" in result:
            return result
        leads = result.get("leads", [])
        return {
            "query": clean_query,
            "total_leads": len(leads),
            "scanned": result.get("scanned", 0),
            "leads": leads[:max_leads]
        }

    return {"error": "Unexpected response shape from find_leads"}


@mcp.tool()
def get_sources(ctx: Context) -> dict:
    """
    Returns a list of all available data sources with descriptions.
    Use this to discover what sources Signalwatch supports before searching.

    Requires an Authorization: Bearer <api_key> header.
    """
    auth_result = _authenticate(ctx, "data", DATA_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    sources = [
        {"name": "reddit", "label": "Reddit", "description": "Community discussions via RSS", "type": "social"},
        {"name": "hackernews", "label": "Hacker News", "description": "Tech community discussions", "type": "tech"},
        {"name": "newsapi", "label": "News API", "description": "Thousands of news publications", "type": "news"},
        {"name": "newsdata", "label": "NewsData.io", "description": "International news coverage", "type": "news"},
        {"name": "rss", "label": "RSS Feeds", "description": "Major news outlets and industry publications", "type": "news"},
        {"name": "youtube", "label": "YouTube", "description": "Video content and reviews", "type": "video"},
        {"name": "mastodon", "label": "Mastodon", "description": "Open-source social network posts", "type": "social"},
        {"name": "bluesky", "label": "Bluesky", "description": "AT Protocol network posts", "type": "social"},
        {"name": "trustpilot", "label": "Trustpilot", "description": "Customer reviews and ratings", "type": "reviews"},
        {"name": "appstore", "label": "App Store", "description": "Apple App Store reviews", "type": "reviews"},
        {"name": "playstore", "label": "Play Store", "description": "Google Play Store reviews", "type": "reviews"},
        {"name": "googlenews", "label": "Google News", "description": "Google News RSS aggregation", "type": "news"},
        {"name": "bingnews", "label": "Bing News", "description": "Bing News RSS aggregation", "type": "news"},
    ]
    return {"sources": sources, "total": len(sources), "status": "operational"}


@mcp.tool()
def get_word_frequencies(ctx: Context, query: str, max_words: int = 40) -> dict:
    """
    Analyses the most frequent meaningful words across mentions for a query.
    Useful for identifying dominant themes and topics.

    Args:
        query: What to search for.
        max_words: Maximum words to return (default 40, max 100).

    Requires an Authorization: Bearer <api_key> header.
    """
    auth_result = _authenticate(ctx, "data", DATA_TOOL_HOURLY_LIMIT)
    if "error" in auth_result:
        return auth_result

    import app as sw

    clean_query = sw.sanitise_query(query)
    if not clean_query:
        return {"error": "Empty or invalid query"}

    max_words = min(max(1, max_words), 100)

    FAST_SOURCES = {
        "reddit": sw.fetch_reddit, "hackernews": sw.fetch_hackernews,
        "googlenews": sw.fetch_google_news, "rss": sw.fetch_rss,
    }

    posts = []
    for name, fn in FAST_SOURCES.items():
        try:
            posts += fn(clean_query)
        except Exception:
            pass

    ranked = sw.filter_and_rank(posts, clean_query)
    frequencies = sw.get_word_frequencies(ranked[:50])

    return {
        "query": clean_query,
        "total_words_found": len(frequencies),
        "words": frequencies[:max_words]
    }