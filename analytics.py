# analytics.py
# GDPR-compliant analytics for Signalwatch.
# Stores only what's needed to understand usage: what pages people visit,
# what they search, when the payment modal opens, and when someone converts.
# Every identifier is the same pseudonymous token already used everywhere
# else in the app (sw_ for anonymous browsers, g_ for Google sign ins).
# We never store raw IP addresses or the full browser User Agent string,
# only a parsed category, for example "Chrome", "Windows", "Desktop", which
# is far less identifying than the raw string while still answering the question.

import os
import random
from datetime import datetime, timedelta

# How long we keep analytics data before automatic cleanup deletes it.
# 90 days matches the "last 90 days" window already used across Signalwatch,
# and keeps the free 500 MB database from filling up over time.
RETENTION_DAYS = 90


def _get_conn():
    """Opens a database connection. Returns None if unavailable."""
    try:
        import psycopg2
        return psycopg2.connect(os.getenv("DATABASE_URL", ""), connect_timeout=5)
    except Exception as e:
        print(f"Analytics DB connection error: {e}")
        return None


def setup_analytics_table():
    """
    Creates the analytics_events table if it does not exist.
    Called once when app.py starts, same pattern as setup_db() and
    setup_payment_tables().
    """
    conn = _get_conn()
    if not conn:
        print("Analytics: DB unavailable, table not created")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id           SERIAL PRIMARY KEY,
                user_token   TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                event_data   JSONB,
                browser      TEXT,
                os           TEXT,
                device       TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Indexes make filtering by date, by user, and by type fast even as
        # the table grows. Without these, every admin filter would scan
        # the whole table, which gets slow and expensive on a free tier.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_token   ON analytics_events(user_token)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_type    ON analytics_events(event_type)")
        conn.commit()
        cur.close()
        conn.close()
        print("Analytics table ready")
    except Exception as e:
        print(f"setup_analytics_table error: {e}")


def parse_user_agent(ua_string: str):
    """
    Turns a raw browser User Agent string into three simple categories:
    browser, operating system, and device type.

    Why we do this instead of storing the raw string:
    A raw User Agent string can be specific enough to fingerprint a single
    device. A category like Chrome, Windows, Desktop tells us what we
    actually need for analytics without storing anything close to identifying.
    """
    if not ua_string:
        return "Unknown", "Unknown", "Unknown"

    ua = ua_string

    # Browser. Order matters here, Edge and Opera both also contain the
    # word Chrome in their User Agent, so we check the more specific
    # markers first.
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Chrome/" in ua and "Chromium" not in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"
    else:
        browser = "Other"

    # Operating system
    if "Windows NT" in ua:
        os_name = "Windows"
    elif "Mac OS X" in ua and "Mobile" not in ua:
        os_name = "macOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua or "iOS" in ua:
        os_name = "iOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Other"

    # Device type
    if "iPad" in ua or "Tablet" in ua:
        device = "Tablet"
    elif "Mobile" in ua or "Android" in ua or "iPhone" in ua:
        device = "Mobile"
    else:
        device = "Desktop"

    return browser, os_name, device


def cleanup_old_events():
    """
    Deletes analytics events older than RETENTION_DAYS.
    This is what makes the system self maintaining on Render's free tier,
    which has no built in cron jobs. It runs once when the server starts,
    and again with a small random chance on every tracked event, so the
    table never grows unbounded even if the server stays up for months.
    """
    conn = _get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        cur.execute("DELETE FROM analytics_events WHERE created_at < %s", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted:
            print(f"Analytics cleanup: removed {deleted} events older than {RETENTION_DAYS} days")
    except Exception as e:
        print(f"cleanup_old_events error: {e}")


def log_event(token: str, event_type: str, event_data: dict, user_agent: str):
    """
    Records one analytics event.
    event_data is a small dictionary, for example {"query": "nike complaints"}
    or {"page": "pricing"}, stored as JSONB so the admin page can filter
    and read it without needing a rigid column for every event type.
    """
    conn = _get_conn()
    if not conn:
        return
    try:
        import json
        browser, os_name, device = parse_user_agent(user_agent)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO analytics_events (user_token, event_type, event_data, browser, os, device)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (token, event_type, json.dumps(event_data or {}), browser, os_name, device))
        conn.commit()
        cur.close()
        conn.close()

        # Self cleaning. Roughly 1 in 50 tracked events also triggers a
        # cleanup pass. Cheap because of the index on created_at, and
        # means we never need an external scheduled job on Render.
        if random.random() < 0.02:
            cleanup_old_events()

    except Exception as e:
        print(f"log_event error: {e}")


def get_email_for_token(token: str) -> str:
    """
    Looks up the email address behind a Google-authenticated token.
    Google tokens are stored as g_<sub> everywhere else in the app, but
    the logins table stores the bare sub, so we strip the prefix before
    looking it up. Returns an empty string for anonymous sw_ tokens,
    since those were never tied to an email in the first place.
    """
    if not token.startswith("g_"):
        return ""
    sub = token[len("g_"):]
    conn = _get_conn()
    if not conn:
        return ""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT email FROM logins WHERE google_sub = %s ORDER BY logged_in_at DESC LIMIT 1",
            (sub,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else ""
    except Exception as e:
        print(f"get_email_for_token error: {e}")
        return ""


def get_events(event_type: str = "", token: str = "", days: int = 30, limit: int = 500):
    """
    Fetches analytics events for the admin page, with optional filters.
    event_type filters to one event type, empty means all.
    token filters to one specific user, empty means all users.
    days only returns events from the last N days.
    limit is a safety cap so a huge date range cannot return the whole table.
    """
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)

        query = """
            SELECT id, user_token, event_type, event_data, browser, os, device, created_at
            FROM analytics_events
            WHERE created_at >= %s
        """
        params = [cutoff]

        if event_type:
            query += " AND event_type = %s"
            params.append(event_type)
        if token:
            query += " AND user_token = %s"
            params.append(token)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        events = []
        for r in rows:
            events.append({
                "id":         r[0],
                "token":      r[1],
                "event_type": r[2],
                "event_data": r[3],
                "browser":    r[4],
                "os":         r[5],
                "device":     r[6],
                "created_at": r[7].isoformat() if r[7] else "",
                "email":      get_email_for_token(r[1])
            })
        return events
    except Exception as e:
        print(f"get_events error: {e}")
        return []


def get_summary(days: int = 30):
    """
    Builds the aggregate numbers shown at the top of the admin page.
    Total events, unique users, event type breakdown, browser and OS and
    device breakdown, top searches, and top pages.
    """
    conn = _get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)

        cur.execute("SELECT COUNT(*) FROM analytics_events WHERE created_at >= %s", (cutoff,))
        total_events = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT user_token) FROM analytics_events WHERE created_at >= %s", (cutoff,))
        unique_users = cur.fetchone()[0]

        cur.execute("""
            SELECT event_type, COUNT(*) FROM analytics_events
            WHERE created_at >= %s GROUP BY event_type
        """, (cutoff,))
        by_type = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("""
            SELECT browser, COUNT(*) FROM analytics_events
            WHERE created_at >= %s GROUP BY browser ORDER BY COUNT(*) DESC
        """, (cutoff,))
        by_browser = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("""
            SELECT os, COUNT(*) FROM analytics_events
            WHERE created_at >= %s GROUP BY os ORDER BY COUNT(*) DESC
        """, (cutoff,))
        by_os = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("""
            SELECT device, COUNT(*) FROM analytics_events
            WHERE created_at >= %s GROUP BY device ORDER BY COUNT(*) DESC
        """, (cutoff,))
        by_device = {row[0]: row[1] for row in cur.fetchall()}

        # Top searched terms. This answers "what are people searching for".
        cur.execute("""
            SELECT event_data->>'query' AS q, COUNT(*) FROM analytics_events
            WHERE created_at >= %s AND event_type = 'search' AND event_data->>'query' IS NOT NULL
            GROUP BY q ORDER BY COUNT(*) DESC LIMIT 15
        """, (cutoff,))
        top_searches = [{"query": row[0], "count": row[1]} for row in cur.fetchall()]

        # Top pages visited
        cur.execute("""
            SELECT event_data->>'page' AS p, COUNT(*) FROM analytics_events
            WHERE created_at >= %s AND event_type = 'page_view' AND event_data->>'page' IS NOT NULL
            GROUP BY p ORDER BY COUNT(*) DESC LIMIT 15
        """, (cutoff,))
        top_pages = [{"page": row[0], "count": row[1]} for row in cur.fetchall()]

        cur.close()
        conn.close()

        return {
            "total_events":  total_events,
            "unique_users":  unique_users,
            "by_type":       by_type,
            "by_browser":    by_browser,
            "by_os":         by_os,
            "by_device":     by_device,
            "top_searches":  top_searches,
            "top_pages":     top_pages
        }
    except Exception as e:
        print(f"get_summary error: {e}")
        return {}


def delete_user_analytics(token: str) -> int:
    """
    Permanently deletes every analytics event for one user token.
    This is the right to erasure control. An admin can wipe one person's
    tracked activity completely, on request, without affecting anyone else.
    Returns how many rows were deleted.
    """
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM analytics_events WHERE user_token = %s", (token,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        print(f"Analytics: deleted {deleted} events for token {token[:12]}...")
        return deleted
    except Exception as e:
        print(f"delete_user_analytics error: {e}")
        return 0


def delete_all_analytics() -> int:
    """
    Permanently deletes every analytics event for every user.
    A full reset, used rarely. Returns how many rows were deleted.
    """
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM analytics_events")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        print(f"Analytics: wiped all {deleted} events")
        return deleted
    except Exception as e:
        print(f"delete_all_analytics error: {e}")
        return 0