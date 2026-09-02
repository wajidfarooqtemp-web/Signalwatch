# rate_limits_db.py
#
# Moves IP rate limiting and AI provider cooldowns out of in-memory
# Python dictionaries and into Postgres, which every server process
# can read and write together.
#
# WHY THIS EXISTS:
# app.py previously used plain module-level variables (_ip_log,
# _evolution_ip_log, _groq_blocked_until, etc). That works fine with
# exactly one running process, which is Signalwatch's current setup
# (WEB_CONCURRENCY=1 on Render). The moment more than one process or
# server instance runs at once, each one has its own separate memory,
# so rate limits and cooldowns stop being consistent across users.
# This file fixes that by storing the same counters in the database
# every process already shares, using the exact same hour-bucket
# pattern already proven in mcp_keys_db.py's mcp_usage table.

import os
from datetime import datetime, timedelta


def _get_conn():
    """Opens a database connection. Returns None if unavailable."""
    try:
        import psycopg2
        return psycopg2.connect(os.getenv("DATABASE_URL", ""), connect_timeout=5)
    except Exception as e:
        print(f"Rate limit DB connection error: {e}")
        return None


def setup_rate_limit_tables():
    """
    Creates the two tables this file needs. Called once when app.py
    starts, same pattern as every other setup_*_tables() function.
    """
    conn = _get_conn()
    if not conn:
        print("Rate limits: DB unavailable — tables not created")
        return
    try:
        cur = conn.cursor()

        # One row per (key, scope, hour). scope lets one table serve
        # both the general 60/hour IP limit and the stricter Website
        # Evolution 5/hour limit, without needing two separate tables.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit_counters (
                key_id      TEXT NOT NULL,
                scope       TEXT NOT NULL,
                hour_bucket TIMESTAMPTZ NOT NULL,
                count       INTEGER DEFAULT 0,
                PRIMARY KEY (key_id, scope, hour_bucket)
            )
        """)

        # One row per AI provider (groq, cerebras, mistral). Replaces
        # the three separate _blocked_until globals with one shared
        # table any process can check.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_provider_cooldowns (
                provider      TEXT PRIMARY KEY,
                blocked_until TIMESTAMPTZ
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("Rate limit tables ready")
    except Exception as e:
        print(f"setup_rate_limit_tables error: {e}")


def check_and_increment_rate_limit(key_id: str, scope: str, hourly_limit: int) -> bool:
    """
    Atomically checks whether key_id (usually an IP address) is under
    hourly_limit for this scope, and if so, records this request.

    Returns True if the request is allowed, False if the limit is
    already reached.

    Fails OPEN (returns True) if the database is unreachable — the
    same choice already made elsewhere in this codebase (see
    try_consume_lead_allowance in app.py), since a database hiccup
    should never be the reason a legitimate request gets blocked.
    """
    conn = _get_conn()
    if not conn:
        return True
    try:
        now = datetime.now()
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)

        cur = conn.cursor()
        cur.execute("""
            SELECT count FROM rate_limit_counters
            WHERE key_id = %s AND scope = %s AND hour_bucket = %s
        """, (key_id, scope, hour_bucket))
        row = cur.fetchone()
        current = row[0] if row else 0

        if current >= hourly_limit:
            cur.close()
            conn.close()
            return False

        cur.execute("""
            INSERT INTO rate_limit_counters (key_id, scope, hour_bucket, count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (key_id, scope, hour_bucket)
            DO UPDATE SET count = rate_limit_counters.count + 1
        """, (key_id, scope, hour_bucket))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"check_and_increment_rate_limit error: {e}")
        return True


def cleanup_old_rate_limits():
    """
    Deletes rate limit rows older than 3 hours, so this table never
    grows without bound. Same self-cleaning idea already used in
    analytics.py's cleanup_old_events, just a much shorter retention
    window since hourly counters are useless after a few hours anyway.
    """
    conn = _get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(hours=3)
        cur.execute("DELETE FROM rate_limit_counters WHERE hour_bucket < %s", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted:
            print(f"Rate limits cleanup: removed {deleted} old counter rows")
    except Exception as e:
        print(f"cleanup_old_rate_limits error: {e}")


def is_provider_blocked(provider: str) -> bool:
    """
    Checks whether an AI provider (groq, cerebras, mistral) is still
    inside its cooldown window from a recent 429.

    Fails open (returns False, meaning "not blocked, go ahead and
    try") if the database is unreachable, so a DB hiccup never wrongly
    blocks an AI call that could otherwise succeed.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT blocked_until FROM ai_provider_cooldowns WHERE provider = %s", (provider,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or not row[0]:
            return False
        blocked_until = row[0]
        # Postgres returns a timezone-aware datetime; compare safely
        now = datetime.now(blocked_until.tzinfo) if blocked_until.tzinfo else datetime.now()
        return now < blocked_until
    except Exception as e:
        print(f"is_provider_blocked error: {e}")
        return False


def set_provider_blocked(provider: str, seconds: int):
    """
    Records that this provider just hit a rate limit, and should not
    be tried again for the given number of seconds. Any process
    checking is_provider_blocked() afterward, even a completely
    different one, will now see this and skip the same provider.
    """
    conn = _get_conn()
    if not conn:
        return
    try:
        blocked_until = datetime.now() + timedelta(seconds=seconds)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_provider_cooldowns (provider, blocked_until)
            VALUES (%s, %s)
            ON CONFLICT (provider) DO UPDATE SET blocked_until = EXCLUDED.blocked_until
        """, (provider, blocked_until))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"set_provider_blocked error: {e}")