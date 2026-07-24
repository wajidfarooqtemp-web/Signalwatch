# mcp_keys_db.py
#
# Database layer for MCP API keys and usage tracking.
# Mirrors the same patterns already used in payments.py and analytics.py —
# plain psycopg2, a setup_*_tables() function called once at boot, and
# small single-purpose functions. Nothing here is MCP-protocol-specific;
# it's just Postgres access, which is why it's a separate file from
# mcp_server.py (that file will import these functions).

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv

# This file can be imported on its own (like we just did from the
# command line) without app.py ever running first. Since app.py's
# load_dotenv() only helps when app.py itself is the entry point,
# we call it here too, so mcp_keys_db.py always has access to .env
# no matter how it's invoked. Calling load_dotenv() twice in one
# process (once here, once from app.py later) is completely safe —
# it just re-reads the same file, no side effects.
load_dotenv()


def _get_conn():
    """Opens a database connection. Returns None if unavailable."""
    try:
        import psycopg2
        return psycopg2.connect(os.getenv("DATABASE_URL", ""), connect_timeout=5)
    except Exception as e:
        print(f"MCP DB connection error: {e}")
        return None


def setup_mcp_tables():
    """
    Creates the two tables MCP auth needs. Called once when app.py starts,
    same pattern as setup_db() / setup_payment_tables() / setup_analytics_table().
    """
    conn = _get_conn()
    if not conn:
        print("MCP: DB unavailable — tables not created")
        return
    try:
        cur = conn.cursor()

        # Each row is one issued API key for one prospect/client.
        # We NEVER store the raw key — only its SHA-256 hash. This is the
        # same principle as never storing a plaintext password: if this
        # table ever leaks, the keys themselves are not directly usable.
        # key_prefix stores the first 8 characters of the raw key ONLY,
        # purely so you can recognise "which key is this" in a dashboard
        # or log line without ever being able to reconstruct the full key.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mcp_api_keys (
                id           SERIAL PRIMARY KEY,
                key_hash     TEXT UNIQUE NOT NULL,
                key_prefix   TEXT NOT NULL,
                client_name  TEXT NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                revoked_at   TIMESTAMPTZ,
                is_active    BOOLEAN DEFAULT TRUE
            )
        """)

        # One row per (key, hour-bucket, tool-category). This is how we
        # implement a sliding-ish hourly rate limit that survives server
        # restarts — unlike an in-memory dict, this table persists across
        # Render deploys and works correctly even with multiple workers.
        # tool_category is either 'data' (cheap, generous limit) or
        # 'ai' (expensive, strict limit) — this is the two-bucket split
        # you asked for, so an MCP client hammering find_leads can't
        # silently drain your OpenRouter/Groq/Cerebras/Mistral quota
        # under cover of the same limit that governs harmless searches.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mcp_usage (
                key_id        INTEGER NOT NULL REFERENCES mcp_api_keys(id),
                hour_bucket   TIMESTAMPTZ NOT NULL,
                tool_category TEXT NOT NULL,
                call_count    INTEGER DEFAULT 0,
                PRIMARY KEY (key_id, hour_bucket, tool_category)
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("MCP tables ready")
    except Exception as e:
        print(f"setup_mcp_tables error: {e}")


def _hash_key(raw_key: str) -> str:
    """
    One-way hash of an API key. SHA-256 is fine here (unlike passwords,
    API keys are already high-entropy random strings, not something a
    human chose — there's no need for a slow hash like bcrypt).
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(client_name: str) -> dict:
    """
    Generates a brand new API key for a prospect/client, stores only its
    hash, and returns the RAW key exactly once. This is the one and only
    moment the raw key exists outside the person's own records — after
    this function returns, Signalwatch itself can never recover it,
    only verify it. That's the correct security property for API keys:
    you (the operator) should not be able to read out a customer's key
    either, same as you can't read out their hashed password.
    """
    # secrets.token_urlsafe generates a cryptographically secure random
    # string, URL-safe (no characters that need escaping in headers).
    raw_key = "sw_mcp_" + secrets.token_urlsafe(32)
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:14]  # e.g. "sw_mcp_AbCdEf" — enough to recognise, not enough to guess

    conn = _get_conn()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO mcp_api_keys (key_hash, key_prefix, client_name)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (key_hash, key_prefix, client_name))
        key_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {
            "key_id": key_id,
            "raw_key": raw_key,
            "message": "Store this key now — it cannot be shown again."
        }
    except Exception as e:
        print(f"create_api_key error: {e}")
        return {"error": "Could not create key"}


def verify_api_key(raw_key: str) -> dict:
    """
    Checks whether a raw key (as sent by an MCP client in the
    Authorization header) is valid and active.

    Returns {"valid": True, "key_id": ..., "client_name": ...} or
    {"valid": False, "reason": "..."}.
    """
    if not raw_key:
        return {"valid": False, "reason": "No key provided"}

    key_hash = _hash_key(raw_key)
    conn = _get_conn()
    if not conn:
        # Fail CLOSED, not open — for a paid data product, an outage
        # should never silently turn into "everyone gets free access."
        return {"valid": False, "reason": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, client_name FROM mcp_api_keys
            WHERE key_hash = %s AND is_active = TRUE AND revoked_at IS NULL
        """, (key_hash,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return {"valid": False, "reason": "Invalid or revoked key"}

        return {"valid": True, "key_id": row[0], "client_name": row[1]}
    except Exception as e:
        print(f"verify_api_key error: {e}")
        return {"valid": False, "reason": "Verification error"}


def check_and_increment_usage(key_id: int, tool_category: str, hourly_limit: int) -> dict:
    """
    Atomically checks whether this key has exceeded its hourly limit for
    this tool_category ('data' or 'ai'), and if not, records this call.

    Why one hour_bucket column instead of a live sliding window:
    truncating "now" down to the start of the current hour turns rate
    limiting into a simple counter increment, the same
    INSERT ... ON CONFLICT DO UPDATE pattern your increment_count()
    in app.py already uses for daily search limits. It's not a
    perfectly smooth rolling window, but it's simple, correct, and
    persists across restarts — which an in-memory dict cannot do.
    """
    conn = _get_conn()
    if not conn:
        # Fail closed here too — if we can't verify the limit, don't allow the call
        return {"allowed": False, "reason": "Database unavailable"}
    try:
        # Truncate current time down to the start of this hour.
        # e.g. 14:37:52 becomes 14:00:00 — every call within the same
        # hour maps to the same row.
        now = datetime.now()
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)

        cur = conn.cursor()

        # First, check current count WITHOUT incrementing, so a call
        # that would exceed the limit is rejected rather than counted.
        cur.execute("""
            SELECT call_count FROM mcp_usage
            WHERE key_id = %s AND hour_bucket = %s AND tool_category = %s
        """, (key_id, hour_bucket, tool_category))
        row = cur.fetchone()
        current_count = row[0] if row else 0

        if current_count >= hourly_limit:
            cur.close()
            conn.close()
            return {
                "allowed": False,
                "reason": f"Rate limit exceeded: {hourly_limit} {tool_category} calls per hour"
            }

        # Under the limit — record this call
        cur.execute("""
            INSERT INTO mcp_usage (key_id, hour_bucket, tool_category, call_count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (key_id, hour_bucket, tool_category)
            DO UPDATE SET call_count = mcp_usage.call_count + 1
            RETURNING call_count
        """, (key_id, hour_bucket, tool_category))
        new_count = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        return {"allowed": True, "count": new_count, "limit": hourly_limit}
    except Exception as e:
        print(f"check_and_increment_usage error: {e}")
        return {"allowed": False, "reason": "Rate limit check failed"}