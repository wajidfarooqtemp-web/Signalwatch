# ─────────────────────────────────────────────────────────────────────────────
# SIGNALWATCH — app.py
# This is the backend brain of Signalwatch.
# It receives search queries, fetches data from multiple sources,
# scores and ranks the results, and uses AI to generate an intelligence briefing.
#
# How it all connects:
# 1. Browser (index.html) sends a search query to this file
# 2. This file fetches data from 11 sources simultaneously
# 3. Results are scored and ranked
# 4. Top results are sent to AI to generate a briefing + strategic questions
# 5. Everything is sent back to the browser as JSON
# ─────────────────────────────────────────────────────────────────────────────

# These lines import tools we need
# "from X import Y" means: from library X, get tool Y
# "import X" means: get the entire library X
from fastapi.responses import StreamingResponse # Tool for sending streaming responses (used for SSE)
from fastapi import FastAPI, Request # tool for creating the web server and handling requests
import asyncio  # NEW — needed for async SSE generator
from fastapi.middleware.cors import CORSMiddleware  # CORS allows browser to talk to server
from datetime import date, datetime, timedelta  # Tools for working with dates and times
import requests   # Tool for making HTTP requests to other websites
import re         # Tool for finding patterns in text (regex)
import xml.etree.ElementTree as ET  # Tool for reading XML files (used for RSS feeds)
import os         # Tool for reading environment variables (our API keys)
import json       # Tool for reading and writing JSON data

# Create the FastAPI application object
# Think of this as turning on the server engine
app = FastAPI()

# Allow browsers to talk to this server from different domains
# Without this, the browser would block requests from Vercel to Railway
# This is called CORS — Cross-Origin Resource Sharing
app.add_middleware(
    CORSMiddleware,

    # allow_origins lists every domain that is allowed to talk to this backend.
    # The browser checks this automatically before sending your search request.
    # If a domain is not on this list, the browser blocks the request entirely.
    # "*" means everyone — we are replacing that with only our actual domains.
    allow_origins=[
        "https://signalwatch.vercel.app",   # Vercel deployment (keep as fallback)
        "https://www.signalwatch.in",       # Your live domain WITH www
        "https://signalwatch.in",           # Your live domain WITHOUT www
        "http://localhost:5500",            # VS Code Live Server
        "http://127.0.0.1:5500",            # Same, different notation
    ],

    allow_methods=["GET", "POST", "OPTIONS"],  # Only the methods your app actually uses
    allow_headers=["*"],                        # Headers are fine to leave open
    allow_credentials=False,
    expose_headers=["*"],
)

# ─── API KEYS ────────────────────────────────────────────────────────────────
# os.getenv() reads environment variables — these are stored securely on Railway/render
# The second argument "" is the default value if the variable is not found
# Never hardcode real API keys in code — always use environment variables
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")
NEWSDATA_API_KEY   = os.getenv("NEWSDATA_API_KEY", "")
YOUTUBE_API_KEY    = os.getenv("YOUTUBE_API_KEY", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")

# How many searches each person gets per day
DAILY_LIMIT = 3


# Google OAuth client ID — get this from console.cloud.google.com
# Create a project → Credentials → OAuth 2.0 Client ID → Web application
# Add your Vercel domain to "Authorised JavaScript origins"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

def verify_google_token(id_token_str):
    # Verifies a Google ID token and returns the stable user ID (sub)
    # The sub field is a permanent unique string tied to the Google account
    # It never changes even if the user changes their email or name
    # We use this as the rate limit key instead of localStorage tokens
    # which users can delete
    #
    # We verify by calling Google's tokeninfo endpoint — no library needed
    # Google checks the signature and expiry for us
    try:
        res = requests.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token_str}",
            timeout=8
        )
        if res.status_code != 200:
            return None
        data = res.json()
        # aud must match our client ID — prevents tokens from other apps being used
        if GOOGLE_CLIENT_ID and data.get("aud") != GOOGLE_CLIENT_ID:
            print(f"Token aud mismatch: {data.get('aud')}")
            return None
        sub = data.get("sub")  # permanent unique Google user ID
        if not sub:
            return None
        # Prefix so we can distinguish Google tokens from legacy sw_ tokens in DB
        email = data.get("email", "unknown")
        print(f"Google login: {email} (sub: {sub[:8]}...)")

        # Store this login in the database
        # This lets you query who has used Signalwatch from the Render console
        try:
            conn = get_db()
            if conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO logins (email, google_sub) VALUES (%s, %s)",
                    (email, sub)
                )
                conn.commit()
                cur.close()
                conn.close()
        except Exception as db_err:
            print(f"Login log error: {db_err}")

        return f"g_{sub}"
    except Exception as e:
        print(f"Google token verify error: {e}")
        return None


# ─── DATABASE ────────────────────────────────────────────────────────────────
# We use PostgreSQL (a database) to track how many searches each browser has done today
# A database is like a spreadsheet that persists even when the server restarts
# Our table has 3 columns: token (who), search_date (when), count (how many)

def get_db():
    # Opens a connection to the database
    # A connection is like picking up a phone to call the database
    try:
        import psycopg2  # psycopg2 is the Python library for PostgreSQL
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        return conn  # Return the open connection
    except Exception as e:
        print("DB connection error:", e)
        return None  # Return None if connection failed

def setup_db():
    # Creates the searches table if it does not already exist
    # Runs once when the server starts
    conn = get_db()
    if not conn:
        print("No DB — rate limiting will not work")
        return
    try:
        cur = conn.cursor()  # A cursor lets us run SQL commands
        # SQL command: CREATE TABLE IF NOT EXISTS
        # This creates a table but only if it does not already exist
        # PRIMARY KEY (token, search_date) means no two rows can have the same token+date
        cur.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                token TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (token, search_date)
            )
        """)

        # Tracks which tokens have used their one free lead scan
        # Once a token appears here, they cannot use /find-leads again
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_scans (
                token      TEXT PRIMARY KEY,
                used_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Stores every Google login so you can see who used your product
        # Query anytime from Render PostgreSQL console:
        # SELECT * FROM logins ORDER BY logged_in_at DESC;
        cur.execute("""
            CREATE TABLE IF NOT EXISTS logins (
                id          SERIAL PRIMARY KEY,
                email       TEXT NOT NULL,
                google_sub  TEXT NOT NULL,
                logged_in_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()  # Save the changes
        cur.close()    # Close the cursor
        conn.close()   # Close the connection
        print("Database ready")
    except Exception as e:
        print("DB setup error:", e)

def get_count(token):
    # Asks the database: how many searches has this token done today?
    # Returns 0 if no record found (first search today)
    conn = get_db()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        # %s is a placeholder — psycopg2 fills it in safely to prevent injection attacks
        # CURRENT_DATE is today's date in the database's timezone
        cur.execute(
            "SELECT count FROM searches WHERE token = %s AND search_date = CURRENT_DATE",
            (token,)  # The comma makes this a tuple — required by psycopg2
        )
        row = cur.fetchone()  # Get one row from results
        cur.close()
        conn.close()
        return row[0] if row else 0  # Return the count, or 0 if no row found
    except Exception as e:
        print("DB get error:", e)
        return 0

def increment_count(token):
    # Adds 1 to the search count for this token today
    # If no record exists yet, creates one
    conn = get_db()
    if not conn:
        return 1
    try:
        cur = conn.cursor()
        # INSERT ... ON CONFLICT ... DO UPDATE is one smart SQL command
        # It means: try to insert a new row
        # If a row already exists for this token+date, just add 1 to the count instead
        # RETURNING count gives us back the new count value
        cur.execute("""
            INSERT INTO searches (token, search_date, count)
            VALUES (%s, CURRENT_DATE, 1)
            ON CONFLICT (token, search_date)
            DO UPDATE SET count = searches.count + 1
            RETURNING count
        """, (token,))
        count = cur.fetchone()[0]  # Get the returned count value
        conn.commit()
        cur.close()
        conn.close()
        return count
    except Exception as e:
        print("DB increment error:", e)
        return 1

# Run setup_db() once when the server starts
# This creates the table if it does not exist
setup_db()

def try_consume_lead_allowance(token: str) -> bool:
    """
    Atomically checks and consumes the lead allowance in one DB round-trip.
    Returns True if the token was allowed (first use).
    Returns False if already used.
    """
    conn = get_db()
    if not conn:
        return True  # Fail open if DB is down
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO lead_scans (token)
               VALUES (%s)
               ON CONFLICT (token) DO NOTHING""",
            (token,)
        )
        # rowcount == 1 means the row was inserted (first use)
        # rowcount == 0 means it already existed (already used)
        allowed = cur.rowcount == 1
        conn.commit()
        cur.close()
        conn.close()
        return allowed
    except Exception as e:
        print(f"try_consume_lead_allowance error: {e}")
        return True

def consume_lead_allowance(token: str):
    """
    Records that this token has used their lead scan.
    Uses INSERT ... ON CONFLICT DO NOTHING so calling twice is safe.
    """
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO lead_scans (token)
               VALUES (%s)
               ON CONFLICT (token) DO NOTHING""",
            (token,)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"consume_lead_allowance error: {e}")


# ─── DATA SOURCES ─────────────────────────────────────────────────────────────
# Each fetch_ function goes to one data source and returns a list of results
# Every result is a dictionary with these keys:
#   title:   the headline or text of the post/article/review
#   source:  which platform it came from (e.g. "reddit", "youtube")
#   url:     link to the original content
#   created: when it was published, as a Unix timestamp (seconds since 1970)
#            0 means we do not know the date

def fetch_reddit(query):
    """
    Reddit via RSS — replaced JSON API (which 403s from Render datacenter IPs).
    Reddit's search.rss endpoint is served via CDN and not IP-blocked.
    Returns same dict format as before so nothing else needs changing.
    """
    results = []
    try:
        # Atom namespace — Reddit RSS uses the Atom feed standard
        # Every tag in the XML is prefixed with this namespace
        ATOM = "http://www.w3.org/2005/Atom"

        url = (
            f"https://www.reddit.com/search.rss"
            f"?q={requests.utils.quote(query)}"
            f"&sort=relevance&t=month&limit=100"
        )

        res = requests.get(
            url,
            timeout=15,
            headers={
                # Reddit still requires a descriptive User-Agent even for RSS
                "User-Agent": "signalwatch/1.0 (brand intelligence; contact wajidfarooqtemp@gmail.com)"
            }
        )

        if res.status_code == 429:
            print("Reddit RSS: rate limited")
            return []
        if res.status_code != 200:
            print(f"Reddit RSS: status {res.status_code}")
            return []
        if not res.content:
            print("Reddit RSS: empty response")
            return []

        root = ET.fromstring(res.content)
        cutoff = datetime.now() - timedelta(days=90)
        seen = set()

        # Atom feeds use <entry> not <item>
        for entry in root.iter(f"{{{ATOM}}}entry"):

            # Title is in <title> with Atom namespace
            title_el = entry.find(f"{{{ATOM}}}title")
            if title_el is None or not title_el.text:
                continue
            title = title_el.text.strip()
            if not title or title in seen:
                continue
            seen.add(title)

            # URL is in <link href="..."> — it's an attribute, not text
            link_el = entry.find(f"{{{ATOM}}}link")
            post_url = link_el.get("href", "") if link_el is not None else ""

            # Published date is in <updated> or <published>
            date_el = (
                entry.find(f"{{{ATOM}}}updated") or
                entry.find(f"{{{ATOM}}}published")
            )
            ts = 0
            if date_el is not None and date_el.text:
                try:
                    # Atom dates are ISO 8601: 2024-01-15T12:00:00+00:00
                    dt_str = date_el.text.strip()
                    # Remove timezone offset for naive comparison
                    dt_str = dt_str[:19]
                    dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
                    if dt < cutoff:
                        continue
                    ts = int(dt.timestamp())
                except Exception:
                    pass  # Keep post even if date parse fails

            results.append({
                "title":   title,
                "source":  "reddit",
                "url":     post_url,
                "created": ts
            })

        print(f"Reddit RSS: {len(results)} posts")
        return results

    except ET.ParseError as e:
        print(f"Reddit RSS: XML parse error: {e}")
        return []
    except Exception as e:
        print(f"Reddit RSS error: {e}")
        return []


def fetch_hackernews(query):
    # Searches HackerNews using their Algolia search API
    # HackerNews is where tech people discuss things early
    # No API key needed
    url = f"https://hn.algolia.com/api/v1/search?query={requests.utils.quote(query)}&tags=story&hitsPerPage=100"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        cutoff = datetime.now() - timedelta(days=90)
        results = []

        for hit in data.get("hits", []):
            if not hit.get("title"):
                continue

            created = hit.get("created_at_i", 0)  # Unix timestamp

            if created and datetime.fromtimestamp(created) < cutoff:
                continue

            results.append({
                "title":   hit["title"],
                "source":  "hackernews",
                # If no URL provided, link to the HN discussion page instead
                "url":     hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "created": created
            })

        return results
    except Exception as e:
        print("HackerNews error:", e)
        return []


def fetch_newsapi(query):
    # Fetches news articles from thousands of publications via NewsAPI
    # Requires a free API key from newsapi.org
    # We limit to the last 30 days and sort by publication date (newest first)
    from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    url = f"https://newsapi.org/v2/everything?q={requests.utils.quote(query)}&pageSize=100&language=en&sortBy=publishedAt&from={from_date}&apiKey={NEWS_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        results = []

        for a in data.get("articles", []):
            if not a.get("title") or a["title"] == "[Removed]":
                continue

            # Convert the ISO date string to a Unix timestamp
            published = a.get("publishedAt", "")
            ts = 0
            if published:
                try:
                    dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                    ts = int(dt.timestamp())
                except:
                    pass  # If date parsing fails, keep ts as 0

            results.append({
                "title":   a["title"],
                "source":  "newsapi",
                "url":     a.get("url", ""),
                "created": ts
            })

        return results
    except Exception as e:
        print("NewsAPI error:", e)
        return []


def fetch_newsdata(query):
    # Fetches news from NewsData.io — a different news aggregator
    # Gives us broader international coverage
    # Requires a free API key from newsdata.io
    url = f"https://newsdata.io/api/1/news?apikey={NEWSDATA_API_KEY}&q={requests.utils.quote(query)}&language=en"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        results = []

        for a in data.get("results", []):
            if not a.get("title"):
                continue
            results.append({
                "title":   a["title"],
                "source":  "newsdata",
                "url":     a.get("link", ""),
                "created": 0  # NewsData free tier does not always include dates
            })

        return results
    except Exception as e:
        print("NewsData error:", e)
        return []


def fetch_rss(query):
    """
    Reads RSS feeds from major news and industry outlets.
    
    Why RSS is your most reliable source:
    RSS is structured XML — it never blocks, never rate limits,
    needs no API key, and almost every news site offers it free.
    Expanding from 5 to 25 feeds significantly increases your signal count.
    
    How it works:
    We fetch each RSS feed, then check every article title for
    your search keywords. Only articles containing your keywords are kept.
    This keeps results relevant even from general news feeds.
    """
    feeds = [
        # ── UK news ──────────────────────────────────────────────────────────
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://feeds.theguardian.com/theguardian/business/rss",
        "https://feeds.theguardian.com/theguardian/technology/rss",
        "https://feeds.theguardian.com/theguardian/money/rss",
        "https://feeds.skynews.com/feeds/rss/business.xml",
        "https://feeds.skynews.com/feeds/rss/technology.xml",
        "https://www.independent.co.uk/rss",

        # ── US news ───────────────────────────────────────────────────────────
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
        "https://feeds.washingtonpost.com/rss/business",
        "https://feeds.washingtonpost.com/rss/technology",

        # ── International news ────────────────────────────────────────────────
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://feeds.reuters.com/reuters/businessNews".replace("feeds.reuters.com", "news.google.com/rss/search?q=reuters+business"),

        # ── Tech and industry ─────────────────────────────────────────────────
        # TechCrunch covers startup and tech brand news extensively
        "https://techcrunch.com/feed/",
        # The Verge covers consumer tech products and brands
        "https://www.theverge.com/rss/index.xml",
        # Wired covers tech culture and brand stories
        "https://www.wired.com/feed/rss",
        # Forbes covers business and brand stories
        "https://www.forbes.com/feeds/forbesrss/",

        # ── Business and consumer ─────────────────────────────────────────────
        "https://www.ft.com/?format=rss",
        "https://feeds.skynews.com/feeds/rss/world.xml",

        # ── Global coverage ───────────────────────────────────────────────────
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://feeds.theguardian.com/theguardian/world/rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ]

    # Remove any duplicate feeds that snuck in
    # dict.fromkeys preserves order while removing duplicates
    feeds = list(dict.fromkeys(feeds))

    results = []
    # Split query into individual keywords for matching
    # e.g. "nike complaints" → ["nike", "complaints"]
    keywords = query.lower().split()

    for feed_url in feeds:
        try:
            res = requests.get(
                feed_url,
                timeout=8,
                headers={"User-Agent": "signalwatch/1.0"}
            )
            if res.status_code != 200:
                continue

            # ET.fromstring() parses the XML — RSS is just structured XML
            root = ET.fromstring(res.content)

            for item in root.iter("item"):
                title_el = item.find("title")
                link_el  = item.find("link")

                if title_el is not None and title_el.text:
                    title = title_el.text.strip()
                    # Only include if at least one search keyword appears
                    if any(k in title.lower() for k in keywords):
                        link = link_el.text.strip() if link_el and link_el.text else ""
                        results.append({
                            "title":   title,
                            "source":  "rss",
                            "url":     link,
                            "created": 0
                        })

        except Exception as e:
            # One feed failing does not stop the others
            print(f"RSS error {feed_url[:40]}: {e}")
            continue

    print(f"RSS: {len(results)} articles across {len(feeds)} feeds")
    return results


def fetch_youtube(query):
    # Searches YouTube for recent videos matching the query
    # Requires a free YouTube Data API v3 key from Google Cloud Console
    # We fetch up to 4 pages of 50 results each = up to 200 videos
    published_after = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = []
    seen = set()
    page_token = None

    for _ in range(4):  # Try up to 4 pages
        try:
            url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={requests.utils.quote(query)}&type=video&maxResults=50&order=date&publishedAfter={published_after}&key={YOUTUBE_API_KEY}"
            if page_token:
                url += f"&pageToken={page_token}"  # Add page token for subsequent pages

            res = requests.get(url, timeout=10)
            data = res.json()

            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                title = snippet.get("title", "")

                if not title or title in seen:
                    continue
                seen.add(title)

                # Convert YouTube's date format to Unix timestamp
                published = snippet.get("publishedAt", "")
                ts = 0
                if published:
                    try:
                        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                        ts = int(dt.timestamp())
                    except:
                        pass

                results.append({
                    "title":   title,
                    "source":  "youtube",
                    "url":     f"https://youtube.com/watch?v={item['id']['videoId']}",
                    "created": ts
                })

            # Get the next page token — if None, there are no more pages
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        except Exception as e:
            print("YouTube error:", e)
            break

    return results


def fetch_mastodon(query):
    # Searches Mastodon — an open-source social network
    # mastodon.social is the largest public instance
    # No API key needed for public search
    try:
        url = f"https://mastodon.social/api/v2/search?q={requests.utils.quote(query)}&type=statuses&limit=40&resolve=false"
        res = requests.get(url, timeout=8, headers={"User-Agent": "signalwatch/1.0"})
        data = res.json()
        results = []
        cutoff = datetime.now() - timedelta(days=90)

        for status in data.get("statuses", []):
            content = status.get("content", "")
            # Strip HTML tags — Mastodon content comes with HTML formatting
            # re.sub() replaces the pattern <...> with empty string ""
            content = re.sub(r'<[^>]+>', '', content).strip()

            if not content or len(content) < 20:
                continue

            created_at = status.get("created_at", "")
            ts = 0
            try:
                # Parse the timestamp — take first 19 characters to remove timezone info
                dt = datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
                ts = int(dt.timestamp())
                if dt < cutoff:
                    continue
            except:
                pass

            results.append({
                "title":   content[:200],  # Limit to 200 characters
                "source":  "mastodon",
                "url":     status.get("url", ""),
                "created": ts
            })

        return results
    except Exception as e:
        print("Mastodon error:", e)
        return []


def fetch_trustpilot(query):
    # Fetches customer reviews from Trustpilot
    # Trustpilot is a major review platform — reviews are high-quality signal
    # because real customers write them after using a product or service
    # No API key needed — we read the public web page
    results = []

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }

        # Step 1: Search for the business on Trustpilot
        search_url = f"https://www.trustpilot.com/api/categoriespages/search/businessunits?query={requests.utils.quote(query)}&language=en&perPage=5"
        search_res = requests.get(search_url, headers=headers, timeout=10)

        if search_res.status_code != 200:
            # Try scraping search page directly as fallback
            alt_url = f"https://www.trustpilot.com/search?query={requests.utils.quote(query)}"
            alt_res = requests.get(alt_url, headers=headers, timeout=10)
            profiles = re.findall(r'href="(/review/[a-zA-Z0-9._-]+)"', alt_res.text)
            if not profiles:
                print(f"Trustpilot: no results for '{query}'")
                return []
            profile_path = profiles[0]
        else:
            search_data = search_res.json()
            businesses = search_data.get("businessUnits", [])
            if not businesses:
                print(f"Trustpilot: no businesses found for '{query}'")
                return []
            profile_path = f"/review/{businesses[0].get('identifyingName', '')}"

        # Step 2: Fetch reviews from the company's Trustpilot page
        review_page_url = f"https://www.trustpilot.com{profile_path}?sort=recency"
        review_res = requests.get(review_page_url, headers=headers, timeout=10)

        # Trustpilot is built with Next.js which embeds all data in a JSON script tag
        # re.search() finds the first match of the pattern in the page HTML
        # re.DOTALL makes . match newline characters too
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            review_res.text,
            re.DOTALL
        )

        if not match:
            print("Trustpilot: could not find review data")
            return []

        page_data = json.loads(match.group(1))
        page_props = page_data.get("props", {}).get("pageProps", {})
        reviews_list = page_props.get("reviews", []) or page_props.get("businessUnit", {}).get("reviews", []) or []

        if not reviews_list:
            # Fallback: find reviews using regex pattern matching
            review_matches = re.findall(r'"text":"([^"]{20,300})".*?"rating":(\d)', review_res.text)
            for text, rating in review_matches[:15]:
                results.append({
                    "title":   f"[{rating}★ Trustpilot] {text}",
                    "source":  "trustpilot",
                    "url":     f"https://www.trustpilot.com{profile_path}",
                    "created": 0
                })
            print(f"Trustpilot: {len(results)} reviews (fallback method)")
            return results

        cutoff = datetime.now() - timedelta(days=90)
        for review in reviews_list[:20]:
            title    = review.get("title", "")
            text     = review.get("text", "")
            rating   = review.get("rating", 0)
            date_str = review.get("dates", {}).get("publishedDate", "")

            full_text = f"{title}: {text[:150]}" if title and text else (title or text[:150])
            if not full_text or len(full_text) < 10:
                continue

            ts = 0
            if date_str:
                try:
                    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                    if dt < cutoff:
                        continue
                    ts = int(dt.timestamp())
                except:
                    pass

            star_label = f"[{rating}★ Trustpilot] " if rating else "[Trustpilot] "
            results.append({
                "title":   f"{star_label}{full_text}",
                "source":  "trustpilot",
                "url":     f"https://www.trustpilot.com{profile_path}",
                "created": ts
            })

        print(f"Trustpilot: {len(results)} reviews found")
        return results

    except Exception as e:
        print(f"Trustpilot error: {e}")
        return []


def fetch_appstore(query):
    # Fetches customer reviews from Apple App Store
    # Uses Apple's iTunes search API to find the app
    # Then uses their customer review RSS feed for the actual reviews
    results = []

    try:
        # Search for the app — entity=software means apps only
        search_url = f"https://itunes.apple.com/search?term={requests.utils.quote(query)}&entity=software&limit=5&country=us"
        search_res = requests.get(search_url, timeout=10)
        search_data = search_res.json()

        if search_data.get("resultCount", 0) == 0:
            print(f"App Store: no apps found for '{query}'")
            return []

        app      = search_data["results"][0]
        app_id   = app.get("trackId")
        app_name = app.get("trackName", query)

        if not app_id:
            return []

        print(f"App Store: found '{app_name}' (ID: {app_id})")

        # Apple's review feed — try both JSON and XML formats
        # Format changed in 2024 — we try JSON first, fall back to XML
        review_url = f"https://itunes.apple.com/rss/customerreviews/page=1/id={app_id}/sortby=mostrecent/json"
        review_res = requests.get(review_url, timeout=10, headers={"User-Agent": "signalwatch/1.0"})

        if review_res.status_code != 200:
            print(f"App Store: review feed returned {review_res.status_code}")
            return []

        # Check what format we got back
        content_type = review_res.headers.get("content-type", "")

        if "json" in content_type or review_res.text.strip().startswith("{"):
            # JSON format
            try:
                review_data = review_res.json()
                entries = review_data.get("feed", {}).get("entry", [])

                # First entry is app info not a review — skip it
                for entry in entries[1:25]:
                    title   = entry.get("title", {}).get("label", "")
                    content = entry.get("content", {}).get("label", "")
                    rating  = entry.get("im:rating", {}).get("label", "")

                    full_text = f"{title}: {content[:150]}" if title else content[:150]
                    if not full_text or len(full_text) < 10:
                        continue

                    star_label = f"[{rating}★ App Store] " if rating else "[App Store] "
                    results.append({
                        "title":   f"{star_label}{full_text}",
                        "source":  "appstore",
                        "url":     f"https://apps.apple.com/app/id{app_id}",
                        "created": 0
                    })
            except Exception as e:
                print(f"App Store JSON parse error: {e}")

        else:
            # XML format — parse differently
            try:
                root = ET.fromstring(review_res.content)
                # Apple RSS namespace
                ns = {
                    "atom":  "http://www.w3.org/2005/Atom",
                    "im":    "http://itunes.apple.com/rss"
                }
                entries = root.findall("atom:entry", ns)

                for entry in entries[1:25]:  # Skip first — it is app info
                    title_el   = entry.find("atom:title", ns)
                    content_el = entry.find("atom:content", ns)
                    rating_el  = entry.find("im:rating", ns)

                    title   = title_el.text   if title_el   and title_el.text   else ""
                    content = content_el.text if content_el and content_el.text else ""
                    rating  = rating_el.text  if rating_el  and rating_el.text  else ""

                    full_text = f"{title}: {content[:150]}" if title else content[:150]
                    if not full_text or len(full_text) < 10:
                        continue

                    star_label = f"[{rating}★ App Store] " if rating else "[App Store] "
                    results.append({
                        "title":   f"{star_label}{full_text}",
                        "source":  "appstore",
                        "url":     f"https://apps.apple.com/app/id{app_id}",
                        "created": 0
                    })
            except Exception as e:
                print(f"App Store XML parse error: {e}")

        print(f"App Store: {len(results)} reviews for {app_name}")
        return results

    except Exception as e:
        print(f"App Store error: {e}")
        return []


def fetch_playstore(query):
    # Searches Google Play Store for apps matching the query
    # Then fetches recent customer reviews
    # Uses the google-play-scraper library (added to requirements.txt)
    results = []

    try:
        # Import inside function — if library is missing, only this function fails
        from google_play_scraper import search, reviews, Sort

        # Step 1: Search Play Store
        # n_hits=3 means return top 3 matching apps
        search_results = search(query, n_hits=3, lang="en", country="us")

        if not search_results:
            print(f"Play Store: no apps found for '{query}'")
            return []

        app    = search_results[0]
        app_id   = app.get("appId")   # Like "com.nike.snkrs"
        app_name = app.get("title", query)

        if not app_id:
            return []

        print(f"Play Store: found '{app_name}' (ID: {app_id})")

        # Step 2: Fetch recent reviews
        # Sort.NEWEST gets the most recent reviews first
        # The function returns a tuple: (list of reviews, continuation token)
        # We only need the list, so we use _ for the token we do not need
        review_list, _ = reviews(app_id, count=20, sort=Sort.NEWEST, lang="en", country="us")

        cutoff = datetime.now() - timedelta(days=90)

        for review in review_list:
            content     = review.get("content", "")
            score       = review.get("score", 0)    # 1 to 5 stars
            review_date = review.get("at")           # datetime object

            if not content or len(content) < 10:
                continue

            if review_date and review_date < cutoff:
                continue

            ts = int(review_date.timestamp()) if review_date else 0
            star_label = f"[{score}★ Play Store] " if score else "[Play Store] "

            results.append({
                "title":   f"{star_label}{content[:200]}",
                "source":  "playstore",
                "url":     f"https://play.google.com/store/apps/details?id={app_id}",
                "created": ts
            })

        print(f"Play Store: {len(results)} reviews for {app_name}")
        return results

    except ImportError:
        print("Play Store: google-play-scraper not installed")
        return []
    except Exception as e:
        print(f"Play Store error: {e}")
        return []

def fetch_google_news(query):
    # Google News provides a free RSS feed for any search query
    # This is different from the paid Google News API
    # No API key needed — it is a public RSS feed
    # Very reliable — Google indexes news from thousands of sources

    results = []
    try:
        # Google News RSS URL format
        # q= is the search query
        # hl=en means English language results
        # gl=US means United States edition
        # ceid=US:en is required by Google News RSS
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en&gl=US&ceid=US:en"

        res = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; signalwatch/1.0)"}
        )

        if res.status_code != 200:
            print(f"Google News: status {res.status_code}")
            return []

        # Parse the RSS XML
        root = ET.fromstring(res.content)

        # Google News RSS: channel > item > title + pubDate
        # Google News RSS already only contains recent articles.
        # We do not apply a hard cutoff — if date parsing fails for any reason,
        # we still include the article rather than silently dropping it.
        # The ts=0 default is safe — it just won't show on the timeline chart.

        for item in root.iter("item"):
            title_el = item.find("title")
            date_el  = item.find("pubDate")
            link_el  = item.find("link")

            if not title_el or not title_el.text:
                continue

            title = title_el.text.strip()
            ts = 0

            if date_el and date_el.text:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_el.text)
                    ts = int(dt.timestamp())
                    # Only filter if we're confident the article is genuinely old
                    # Use 90 days to match other sources, not 30
                    cutoff = datetime.now() - timedelta(days=90)
                    if dt.replace(tzinfo=None) < cutoff:
                        continue
                except Exception:
                    pass  # Date unclear — include the article anyway

            url_text = link_el.text if link_el and link_el.text else ""

            results.append({
                "title":   title,
                "source":  "googlenews",
                "url":     url_text,
                "created": ts
            })

        print(f"Google News: {len(results)} articles")
        return results

    except Exception as e:
        print(f"Google News error: {e}")
        return []
    
def fetch_bing_news(query):
    # Bing News RSS feed — free, no API key needed.
    # Different index than Google News so catches different articles.
    # Format: news.bing.com/news/search?q=query&format=rss
    results = []

    try:
        url = f"https://www.bing.com/news/search?q={requests.utils.quote(query)}&format=rss"

        res = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; signalwatch/1.0)"
            }
        )

        if res.status_code != 200:
            print(f"Bing News: status {res.status_code}")
            return []

        root = ET.fromstring(res.content)
        cutoff = datetime.now() - timedelta(days=30)

        for item in root.iter("item"):
            title_el = item.find("title")
            link_el  = item.find("link")
            date_el  = item.find("pubDate")

            if not title_el or not title_el.text:
                continue

            title = title_el.text.strip()

            ts = 0
            if date_el and date_el.text:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_el.text)
                    dt_naive = dt.replace(tzinfo=None)
                    if dt_naive < cutoff:
                        continue
                    ts = int(dt.timestamp())
                except:
                    pass

            link = link_el.text.strip() if link_el and link_el.text else ""

            results.append({
                "title":   title,
                "source":  "bingnews",
                "url":     link,
                "created": ts
            })

        print(f"Bing News: {len(results)} articles")
        return results

    except Exception as e:
        print(f"Bing News error: {e}")
        return []
    
def fetch_wikipedia(query):
    # Uses Wikipedia's search API to find context about the query topic
    # This gives background knowledge that helps the AI understand the topic
    # No API key needed
    results = []
    clean = re.sub(r'".*?"', '', query).lower()  # Remove quoted phrases from query
    stop = {"not", "or", "and", "the", "a", "is", "in", "of", "to", "complaints"}
    keywords = [w for w in clean.split() if w not in stop]
    search_term = " ".join(keywords[:3])  # Use first 3 keywords

    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={requests.utils.quote(search_term)}&limit=3&format=json"
    try:
        res = requests.get(url, timeout=8)
        # Wikipedia sometimes returns 200 with empty body — guard before parsing
        if not res.content or not res.content.strip():
            return results
        data = res.json()
        # Wikipedia returns: [query, [titles], [descriptions], [urls]]
        for title, desc in zip(data[1], data[2]):
            if desc:
                results.append({
                    "title":   f"Wikipedia: {title} — {desc[:120]}",
                    "source":  "wikipedia",
                    "url":     f"https://en.wikipedia.org/wiki/{requests.utils.quote(title)}",
                    "created": 0
                })
    except Exception as e:
        print("Wikipedia error:", e)

    return results


# ─── AI ───────────────────────────────────────────────────────────────────────

def get_free_models():
    # Fetches free models but prioritises ones that are less likely to be rate-limited.
    # We shuffle the list so all models get used, not just the first few.
    import random
    try:
        res = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10
        )
        data = res.json()
        free_models = []

        for model in data.get("data", []):
            model_id    = model.get("id", "")
            pricing     = model.get("pricing", {})
            prompt_cost = float(pricing.get("prompt", "1") or "1")
            if ":free" in model_id or prompt_cost == 0:
                # Skip models that are consistently unreliable
                skip = ["owl-alpha", "laguna", "nemotron"]
                if any(s in model_id for s in skip):
                    continue
                free_models.append(model_id)

        # Shuffle so we spread load across models rather than hammering the same ones
        random.shuffle(free_models)
        print(f"Found {len(free_models)} free models")
        return free_models[:8]  # Try up to 8

    except Exception as e:
        print("Could not fetch model list:", e)
        return [
            "meta-llama/llama-3.1-8b-instruct:free",
            "meta-llama/llama-3.2-3b-instruct:free",
            "google/gemma-2-9b-it:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2-7b-instruct:free",
        ]


def strip_markdown(text):
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)   # Remove **bold**
    text = re.sub(r'\*([^*]+)\*',     r'\1', text)   # Remove *italic*
    text = re.sub(r'#{1,6}\s',        '',    text)   # Remove ## headers
    text = re.sub(r'`([^`]+)`',       r'\1', text)   # Remove `code`
    text = re.sub(r'\n{3,}',         '\n\n', text)   # Collapse blank lines

    # Remove dashes used as list starters — these look AI-generated
    # The pattern ^\s*[-–—]\s* means: start of line, optional space,
    # a dash (hyphen, en dash, or em dash), optional space
    # We replace with empty string — removing the dash entirely
    text = re.sub(r'^\s*[-–—]\s+', '', text, flags=re.MULTILINE)

    # Remove standalone em dashes used as separators mid-sentence
    # Replace " — " with ": " which reads more naturally
    text = re.sub(r'\s+[–—]\s+', ': ', text)

    return text.strip()

def strip_agent_language(text: str) -> str:
    """
    Removes technical internal words from any text shown to the user.
    
    Why this exists:
    The AI sometimes uses words like "loop", "cycle", "investigation"
    even when told not to. Instead of fighting the AI with longer prompts,
    we clean the output after the fact. This is more reliable.
    
    We replace each banned word with a natural alternative.
    re.sub with re.IGNORECASE catches Loop, LOOP, loop — all variants.
    The \b markers mean "word boundary" — so "loop" matches but
    "loophole" does not. We only replace the whole word.
    """
    if not text:
        return text
    
    # Each tuple is (pattern_to_find, replacement)
    # \b = word boundary — prevents replacing parts of other words
    replacements = [
        (r'\bloop\s+\d+\b',       'Agent'),        # "loop 3" → "Agent"
        (r'\bLoop\s+\d+\b',       'Agent'),        # "Loop 3" → "Agent"  
        (r'\bcycle\s+\d+\b',      'Agent'),        # "cycle 2" → "Agent"
        (r'\biteration\s+\d+\b',  'Agent'),        # "iteration 1" → "Agent"
        (r'\bloop\b',             'phase'),         # standalone "loop" → "phase"
        (r'\binvestigation\b',    'analysis'),      # "investigation" → "analysis"
        (r'\bInvestigate\b',      'Analyse'),       # "Investigate" → "Analyse"
        (r'\bInvestigating\b',    'Analysing'),     # "Investigating" → "Analysing"
    ]
    
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    return text.strip()


def ai_call(prompt):
    # Sends a prompt to the AI and returns the response text
    # Tries multiple free models in order — if one fails, tries the next
    models = get_free_models()
    print(f"Trying {len(models)} models")

    for model in models:
        try:
            print(f"Trying: {model}")
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization":  f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type":   "application/json",
                    "HTTP-Referer":   "https://signalwatch-r6s8.onrender.com",
                    "X-Title":        "Signalwatch"
                },
                json={
                    "model":    model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 600
                },
                timeout=40
            )

            print(f"Status {res.status_code} from {model}")

            # 429 = rate limited — wait briefly then try next model
            if res.status_code == 429:
                import time
                time.sleep(1)
                continue

            # 502/503 = server error — try next model immediately
            if res.status_code in [502, 503]:
                continue

            # 400 = bad request for this model — skip it
            if res.status_code == 400:
                continue

            # 401 = bad API key — no point trying other models
            if res.status_code == 401:
                print("Bad API key — stopping")
                return None

            data = res.json()

            if "choices" not in data:
                continue

            # content can be a string or a list of objects depending on the model
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            else:
                text = content or ""

            text = strip_markdown(text.strip())

            # Only accept responses longer than 50 characters
            if len(text) > 50:
                print(f"Got response from {model}")
                return text

        except Exception as e:
            print(f"Error with {model}: {e}")
            continue

    print("All models failed")
    return None


# ─── SCORING AND RANKING ──────────────────────────────────────────────────────

def explain_score(title, keywords, phrases, score):
    # Generates a plain English explanation of why a result scored the way it did
    # This makes the ranking transparent and trustworthy
    reasons = []
    t = title.lower()

    for w in keywords:
        count = t.count(w.lower())
        if count == 1:
            reasons.append(f"contains '{w}'")
        elif count > 1:
            reasons.append(f"mentions '{w}' {count} times")

    # Check for the all-keywords-together bonus
    if len(keywords) > 1:
        if all(w.lower() in t for w in keywords):
            reasons.append("all search terms appear together")

    for p in phrases:
        if p.lower() in t:
            reasons.append(f"exact phrase: '{p}'")

    if not reasons:
        return ""

    return "Ranked high: " + ", ".join(reasons)


def score_post(text, keywords):
    # Calculates a relevance score for one result
    # +2 for each time a keyword appears in the title
    # +3 bonus if ALL keywords appear together in the same title
    t = text.lower()
    score = 0
    for w in keywords:
        count = t.count(w.lower())
        score += count * 2  # Each mention of a keyword adds 2 points
    if len(keywords) > 1:
        if sum(1 for w in keywords if w.lower() in t) == len(keywords):
            score += 3  # Bonus for having all keywords
    return score


def extract_keywords(query):
    # Splits the query into: keywords, excluded words, and exact phrases
    # Exact phrases are surrounded by "quotes"
    stop = {"not", "or", "and", "the", "a", "is", "in", "of", "to"}
    phrases = re.findall(r'"(.*?)"', query)        # Find "quoted phrases"
    clean   = re.sub(r'".*?"', '', query).lower()  # Remove phrases from query
    words   = [w for w in clean.split() if w not in stop]
    return words, phrases


def filter_and_rank(posts, query):
    # Filters and ranks all results by relevance score
    # Results with score 0 (no keyword matches) are removed
    raw_words = query.split()
    exclude = []
    i = 0
    while i < len(raw_words):
        if raw_words[i].upper() == "NOT" and i + 1 < len(raw_words):
            exclude.append(raw_words[i + 1].lower())
            i += 2
        else:
            i += 1

    keywords, phrases = extract_keywords(query)
    results = []
    seen_titles = set()

    for post in posts:
        title = post["title"]
        if title in seen_titles:
            continue
        seen_titles.add(title)

        text = title.lower()

        # Skip if title contains an excluded word (from NOT operator)
        if any(w in text for w in exclude):
            continue

        s = score_post(title, keywords)

        # Bonus points for exact phrase matches
        if phrases:
            for p in phrases:
                if p.lower() in text:
                    s += 5

        if s == 0:
            continue  # Drop results with no keyword matches

        results.append({
            "title":        title,
            "score":        s,
            "score_reason": explain_score(title, keywords, phrases, s),
            "source":       post["source"],
            "url":          post.get("url", ""),
            "created":      post.get("created", 0)
        })

    # Sort by score, highest first
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─── WORD FREQUENCIES ─────────────────────────────────────────────────────────

def get_word_frequencies(results):
    # Counts how often each word appears across all result titles
    # Used to build the word cloud showing dominant themes
    stop_words = {
        "the","a","an","and","or","but","in","on","at","to","for","of","with",
        "is","it","its","this","that","was","are","be","been","have","has","had",
        "not","from","by","as","i","my","we","you","he","she","they","their",
        "our","your","his","her","which","who","what","how","when","where","why",
        "will","would","could","should","may","might","can","do","did","does",
        "about","after","before","more","also","just","than","then","so","if",
        "up","out","all","new","one","two","time","get","got","us","me","him",
        "them","been","into","over","after","under","re","via","per","vs"
    }
    freq = {}
    for r in results:
        # \b[a-zA-Z]{4,}\b matches whole words of 4+ letters
        words = re.findall(r'\b[a-zA-Z]{4,}\b', r["title"].lower())
        for w in words:
            if w not in stop_words:
                freq[w] = freq.get(w, 0) + 1

    # Sort by frequency, most common first
    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [{"word": w, "count": c} for w, c in sorted_freq[:40]]

def extract_briefing_and_questions(raw_text):
    # ── What this does ───────────────────────────────────────────────────────
    # Extracts briefing and questions from whatever the AI returned.
    # The AI sometimes returns clean JSON, sometimes JSON with code fences,
    # sometimes broken JSON. This function handles every case.
    # Returns (briefing_string, questions_list) in all cases.
    # ─────────────────────────────────────────────────────────────────────────
    print(f"AI raw response (first 200 chars): {repr(raw_text[:200])}")

    briefing  = ""
    action    = ""
    questions = []

    if not raw_text:
        return briefing, action, questions

    # Step 1: Aggressively remove all markdown formatting
    clean = raw_text.strip()
    # If the model reasoned before returning JSON, discard everything before the first {
    brace = clean.find('{')
    if brace > 0:
        clean = clean[brace:]
    clean = re.sub(r'```[a-zA-Z]*\n?', '', clean)  # Remove opening ```json or ```
    clean = re.sub(r'```',             '', clean)   # Remove any remaining ```
    clean = re.sub(r'^\s*`+\s*',      '', clean)   # Remove leading backticks
    clean = re.sub(r'\s*`+\s*$',      '', clean)   # Remove trailing backticks
    clean = clean.strip()

    # Step 2: Try to find and parse a JSON object anywhere in the text
    # Sometimes the AI puts text before or after the JSON
    json_match = re.search(r'\{[\s\S]*\}', clean)

    if json_match:
        try:
            parsed    = json.loads(json_match.group())
            briefing  = parsed.get("briefing",  "")
            # If the model snuck reasoning into the briefing value, take only the last 2 sentences
            # Reasoning always appears first — the actual briefing is at the end
            if len(briefing) > 400:
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', briefing) if s.strip()]
                briefing = ' '.join(sentences[:2]) if len(sentences) >= 2 else sentences[0] if sentences else briefing
            action    = parsed.get("action",    "")
            # Same guard — take last sentence if model padded the action
            if len(action) > 300:
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', action) if s.strip()]
                action = sentences[-1] if sentences else action
            questions = parsed.get("questions", [])

            # Clean internal algorithm labels from question reasons
            # Users should see strategic language, not technical labels
            cleaned_questions = []
            for q in questions:
                if isinstance(q, dict):
                    reason = q.get("reason", "")
                    # Remove co-occurrence labels like "[price, switching]"
                    reason = re.sub(r'Co-occurrence of \[[^\]]+\]', '', reason)
                    reason = re.sub(r'\[[^\]]+\]', '', reason)
                    reason = re.sub(r'co-occur\w*', '', reason, flags=re.IGNORECASE)
                    reason = re.sub(r'\s+', ' ', reason).strip()
                    cleaned_questions.append({
                        "question": q.get("question", ""),
                        "reason": reason
                    })
                else:
                    cleaned_questions.append(q)
            questions = cleaned_questions

            # Clean the briefing of any remaining markdown
            briefing = re.sub(r'\*\*([^*]+)\*\*', r'\1', briefing)
            briefing = re.sub(r'\*([^*]+)\*',     r'\1', briefing)
            briefing = re.sub(r'`([^`]+)`',       r'\1', briefing)
            briefing = briefing.strip()

            return briefing, action, questions
        except json.JSONDecodeError:
            pass

    # Step 3: JSON parsing failed — extract briefing text directly
    # Look for text after "briefing": or just use the whole cleaned text
    briefing_match = re.search(r'"briefing"\s*:\s*"(.*?)"(?:\s*,|\s*\})', clean, re.DOTALL)
    if briefing_match:
        briefing = briefing_match.group(1)
        # Unescape JSON string escapes
        briefing = briefing.replace('\\"', '"').replace('\\n', ' ').replace('\\\\', '\\')
    else:
        # Last resort — but strip any question/action sections first
        # The AI sometimes returns a flat text with "Briefing:", "Action:", "Questions:"
        if not clean.startswith('{') and not clean.startswith('"briefing"'):
            # Try to extract just the briefing section if the AI used labels
            labelled = re.search(
                r'(?:briefing|summary)[:\s]+(.+?)(?:action|questions|$)',
                clean, re.IGNORECASE | re.DOTALL
            )
            if labelled:
                briefing = labelled.group(1).strip()
            else:
                # Take only the first 2 sentences — questions always come after
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if s.strip()]
                briefing = ' '.join(sentences[:2]) if sentences else clean
        else:
            # Strip the JSON structure and take just the text content
            briefing = re.sub(r'[{}":\[\]]', ' ', clean)
            briefing = re.sub(r'briefing|questions|question|reason', '', briefing)
            briefing = re.sub(r'\s+', ' ', briefing).strip()

    # Clean any remaining markdown from briefing
    briefing = re.sub(r'\*\*([^*]+)\*\*', r'\1', briefing)
    briefing = re.sub(r'\*([^*]+)\*',     r'\1', briefing)
    briefing = briefing.strip()

    return briefing, action, questions

# ─── PATTERN LIBRARY ─────────────────────────────────────────────────────────
# 15 documented patterns from real brand intelligence cases.
# Each pattern uses THEME-BASED detection — looking for groups of related
# concepts rather than exact words. This makes detection much more robust.
#
# How scoring works:
# Each pattern has multiple signal GROUPS.
# A group fires if ANY word in that group appears in the results.
# Pattern fires if at least 2 groups fire.
# This catches "consuming content" and "watching" and "streaming" as the same theme.

# ─── INSIGHT GENERATION ───────────────────────────────────────────────────────
#
# Architecture:
# Step 1 — Co-occurrence detection (algorithm, no AI)
#           Finds results where multiple significant concepts appear together.
#           This is what a human analyst actually does: spots the result that
#           mentions BOTH "price" AND "switching" in the same sentence.
#           Co-occurrence is real signal. Word frequency across all results is noise.
#
# Step 2 — Blind spot detection (algorithm, no AI)
#           Finds what customers are asking that the brand has not answered.
#           Finds complaint clusters with no visible brand response.
#           This is the USP: not what everyone knows, but what was ignored.
#
# Step 3 — AI briefing (uses OpenRouter)
#           Reads the co-occurrence clusters and blind spots.
#           Writes a specific, non-generic briefing.
#           Generates questions based on what the algorithm actually found.
#           AI is given the algorithm output as context — not raw titles.
#           This makes AI output specific rather than generic.

# ── CONCEPT GROUPS ──────────────────────────────────────────────────────────
# These are the building blocks of co-occurrence detection.
# Each group is a cluster of semantically related words.
# When a single result contains words from TWO different groups,
# that is a meaningful signal worth surfacing.

# ─── QUERY SANITISATION ───────────────────────────────────────────────────────
# This function runs on every search query before it touches any fetcher.
# It removes characters that could be used to attack your scrapers or
# inject content into responses.
#
# Think of it like a security guard at the door —
# it checks what comes in before letting it through.

def sanitise_query(query: str) -> str:
    """
    Cleans a search query by removing dangerous characters.

    What we remove and why:
    - Null bytes (\x00): used to confuse parsers, never in real searches
    - HTML tags (<script>, <img> etc): prevent XSS if query appears in a response
    - Shell characters (`, $, |, ;): prevent command injection if query reaches a shell
    - Excess length: no real brand query needs more than 200 characters

    What we keep:
    - Letters, numbers, spaces — obviously
    - Hyphens and apostrophes — brand names like "L'Oreal", "Coca-Cola"
    - Quotes — your users use "exact phrase" syntax which is legitimate
    - Question marks, exclamation marks — legitimate in queries
    """

    if not query:
        return ""

    # Step 1: Remove null bytes — these are never in legitimate queries
    # A null byte is a special character (value zero) used to trick parsers
    query = query.replace('\x00', '')

    # Step 2: Remove HTML tags using regex
    # re.sub replaces anything matching the pattern with ""
    # The pattern <[^>]*> means: a < followed by anything, followed by >
    query = re.sub(r'<[^>]*>', '', query)

    # Step 3: Remove shell injection characters
    # These characters have special meaning in command lines
    # If your query ever reaches a shell command (it shouldn't but defence in depth),
    # these prevent it being used maliciously
    for char in ['`', '$', '|', ';', '\\']:
        query = query.replace(char, '')

    # Step 4: Limit length to 200 characters
    # Slicing a string in Python: string[:200] means "first 200 characters"
    query = query[:200]

    # Step 5: Remove leading and trailing spaces
    return query.strip()

CONCEPT_GROUPS = {
    "switching":    ["switched", "switching", "left", "moved", "replaced",
                     "cancelled", "quit", "abandoned", "dropped", "chose instead"],
    "price":        ["price", "expensive", "cheap", "cost", "afford", "overpriced",
                     "value", "worth", "budget", "pricing", "pay", "hike"],
    "quality":      ["quality", "broken", "defective", "poor", "excellent",
                     "durable", "lasts", "falls apart", "best", "worst"],
    "emotion":      ["love", "hate", "frustrated", "disappointed", "angry",
                     "happy", "satisfied", "obsessed", "annoyed", "disgusted"],
    "comparison":   ["vs", "versus", "compared", "better than", "worse than",
                     "beats", "unlike", "over", "instead of", "rather than"],
    "service":      ["customer service", "support", "helpline", "waiting",
                     "ignored", "no response", "useless", "helpful", "agent"],
    "trust":        ["trust", "honest", "misleading", "fake", "lied",
                     "transparent", "hiding", "greenwashing", "authentic"],
    "innovation":   ["new", "feature", "update", "wish", "want", "need",
                     "missing", "would be better", "should add", "idea"],
    "crisis":       ["boycott", "cancel", "scandal", "outrageous", "unacceptable",
                     "disgusting", "exposed", "shame", "never again"],
    "context":      ["while", "during", "with", "alongside", "watching",
                     "eating", "commute", "morning", "evening", "gym", "travel"],
    "recommend":    ["recommend", "told", "shared", "showed", "convinced",
                     "suggested", "mentioned to", "forwarded"],
    "question":     ["which", "should i", "help me", "thinking of", "advice",
                     "anyone used", "thoughts on", "is it worth", "deciding"],
}


def find_cooccurrences(results):
    # ── What this does ───────────────────────────────────────────────────────
    # Scans each individual result title for words from multiple concept groups.
    # When a single title contains words from 2+ different groups,
    # that title is a meaningful co-occurrence signal.
    #
    # Example:
    # "Switched from Nokia to Samsung after price hike"
    # → hits "switching" group AND "price" group in the SAME title
    # → this is real signal: price drove a switching decision
    #
    # Compare to old approach:
    # "switching" appears in title 4, "price" appears in title 12
    # → old system would call this a pattern
    # → this system would NOT because they are different results
    # ─────────────────────────────────────────────────────────────────────────

    cooccurrences = []  # List of meaningful individual results with their concept labels

    for r in results:
        text = r["title"].lower()
        hit_groups = []

        for group_name, words in CONCEPT_GROUPS.items():
            # Check if any word from this concept group appears in this title
            if any(w in text for w in words):
                hit_groups.append(group_name)

        # Only flag results that hit 2 or more concept groups
        # Single-concept results are not interesting
        if len(hit_groups) >= 2:
            cooccurrences.append({
                "title":    r["title"],
                "url":      r.get("url", ""),
                "source":   r["source"],
                "concepts": hit_groups,  # Which concept groups appeared together
                "richness": len(hit_groups)  # More concepts = richer signal
            })

    # Sort by richness — results with most concept groups first
    cooccurrences.sort(key=lambda x: x["richness"], reverse=True)
    return cooccurrences[:10]  # Top 10 richest results


def find_question_clusters(results):
    # ── What this does ───────────────────────────────────────────────────────
    # Finds results where customers are explicitly asking questions.
    # These are the highest-value signals: real people, real confusion,
    # real unmet needs — often unanswered by the brand.
    #
    # A brand that knows what questions its customers are asking
    # and can answer them has an enormous advantage.
    # ─────────────────────────────────────────────────────────────────────────

    question_signals = [
        "which", "should i", "help me", "anyone know", "is it worth",
        "thinking of", "advice", "recommend", "vs", "or", "difference between",
        "why does", "how do i", "what is", "anyone used", "thoughts on",
        "deciding between", "can't decide", "opinions on"
    ]

    questions_found = []
    for r in results:
        text = r["title"].lower()
        if any(q in text for q in question_signals):
            questions_found.append(r["title"])

    return questions_found[:8]  # Top 8 question-type results


def generate_insight(results, query):
    # ── What this does ───────────────────────────────────────────────────────
    # Combines algorithm output with AI to produce specific, non-generic output.
    #
    # Step 1: Algorithm finds co-occurrences and question clusters.
    #         This gives AI something specific to work with.
    #
    # Step 2: AI receives the algorithm output as structured context.
    #         Instead of reading 200 titles, AI reads:
    #         - the 10 richest co-occurrence results
    #         - the 8 question-type results
    #         - what concept groups co-occurred
    #         This forces specific output rather than generic summaries.
    #
    # Step 3: AI writes briefing + generates questions from actual patterns found.
    # ─────────────────────────────────────────────────────────────────────────

    if not results:
        return {
            "briefing":  "Nothing meaningful came up. Try a broader search.",
            "questions": [],
            "patterns":  [],
            "cooccurrences_found": 0,
            "questions_found": 0
        }

    # Step 1 — Run algorithms
    cooccurrences  = find_cooccurrences(results)
    question_results = find_question_clusters(results)

    today = datetime.now().strftime("%d %B %Y")
    sources_used = list(set(r["source"] for r in results))

    timed = [r for r in results if r.get("created", 0) > 0]
    date_range = ""
    if timed:
        newest = datetime.fromtimestamp(max(r["created"] for r in timed)).strftime("%d %b %Y")
        oldest = datetime.fromtimestamp(min(r["created"] for r in timed)).strftime("%d %b %Y")
        date_range = f"Data covers {oldest} to {newest}."

    # Step 2 — Build AI context from algorithm output
    # AI gets the rich results, not all 200 titles
    if cooccurrences:
        rich_results_text = "\n".join(
            f'- [{", ".join(c["concepts"])}] {c["title"]}'
            for c in cooccurrences
        )
    else:
        # Fallback: send top 15 results if no co-occurrences found
        rich_results_text = "\n".join(f"- {r['title']}" for r in results[:15])

    question_text = ""
    if question_results:
        question_text = "\nCustomers asking questions:\n" + "\n".join(
            f"- {q}" for q in question_results
        )

    # Step 3 — AI prompt that uses algorithm output as input
    prompt = f"""Today is {today}. You are analysing signal data about: {query}

Sources: {', '.join(sources_used)}. {date_range}

Signal data (concept labels show what themes appear together in the same result):
{rich_results_text}
{question_text}

Return a JSON object with exactly THREE keys: "briefing", "action", and "questions".

"briefing": 2 to 3 sentences. Plain British English. Conversational. No labels, no asterisks, no dashes, no brand name in quotes. No hedging words like suggests, indicates, appears, seems. Start with a specific observation from the data. Second sentence says why it matters commercially.

"action": One sentence. The single most important thing a brand manager should do in the next 48 hours. Start with a verb. Be specific. Not "monitor" or "consider" — an actual action like "Contact the App Store reviewers complaining about refunds and offer direct resolution" or "Publish a clear comparison page addressing the three most common competitor questions in the data."

"questions": Array of exactly 3 objects with "question" and "reason" keys. Each question must come directly from a specific pattern you see in the data. Strategic language only. No technical terms like co-occurrence or algorithm. The reason should explain the business implication, not the technical method.

Return only raw JSON. No markdown. No backticks. No code fences."""

    ai_result = ai_call(prompt)

    # Parse AI response
    briefing   = ""
    action     = ""
    questions  = []

    if ai_result:
        briefing, action, questions = extract_briefing_and_questions(ai_result)

    # If the first AI call returned no action or no briefing,
    # retry once with a much simpler prompt.
    # A simpler prompt is less likely to produce malformed JSON.
    # We do not retry infinitely — once is enough.
    # If the retry also fails, we accept empty and move on.
    # Empty is honest. A fake action destroys trust.
    if not action or not briefing:
        print("generate_insight: retrying with simpler prompt")

        # Build a minimal context — just the top 5 titles
        top_titles = "\n".join(f"- {r['title']}" for r in results[:5])

        simple_prompt = f"""Analyse these mentions about "{query}":
{top_titles}

Return JSON with two keys only:
"briefing": one sentence stating the most important thing happening right now.
"action": one sentence starting with a verb — the single most important thing to do in 48 hours.

Raw JSON only. No markdown. No backticks. Example:
{{"briefing": "Complaints about delivery speed are rising.", "action": "Publish a delivery update on your main channels within 24 hours."}}"""

        retry_result = ai_call(simple_prompt)

        if retry_result:
            retry_briefing, retry_action, retry_questions = extract_briefing_and_questions(retry_result)
            # Only use retry values if the original was missing
            if not briefing and retry_briefing:
                briefing = retry_briefing
            if not action and retry_action:
                action = retry_action
            # Do not overwrite questions if we already have them
            if not questions and retry_questions:
                questions = retry_questions

    # Final fallback for briefing only — never force a fake action
    if not briefing:
        briefing = (
            f"Found {len(results)} mentions about {query} across "
            f"{len(sources_used)} sources. Raw signals below tell the story."
        )
    # action stays empty if both calls failed — that is correct behaviour

    if cooccurrences:
        concept_pairs = []
        for c in cooccurrences[:5]:
            if len(c["concepts"]) >= 2:
                concept_pairs.append(f"{c['concepts'][0]} + {c['concepts'][1]}")
        patterns_display = list(set(concept_pairs))[:3]
    else:
        patterns_display = []

    return {
        "briefing":  briefing,
        "action":    action if 'action' in dir() else "",
        "questions": questions,
        "patterns":  patterns_display,
        "cooccurrences_found": len(cooccurrences),
        "questions_found":     len(question_results)
    }

# ─────────────────────────────────────────────────────────────────────────────
# SIGNALWATCH CHIEF OF STAFF AGENT SYSTEM
#
# What this is:
# A multi-agent system that runs CONTINUOUSLY while the user is on the page.
# It does not stop after one result. It thinks, evaluates, decides to go
# deeper, and keeps streaming new findings until the user leaves.
#
# Why this is genuinely an agent and not a scraper:
# A scraper fetches once and returns. This system:
# 1. Reads what the existing sources found
# 2. Decides what angles have not been covered
# 3. Dispatches specialist agents to investigate those angles
# 4. Evaluates what came back
# 5. Decides whether to go deeper or conclude
# 6. Loops up to 3 times, each time building on what it learned
#
# How it connects to existing code:
# - Imports your existing fetch functions — zero duplication
# - Yields through the same SSE connection as existing sources
# - Adds one new message type: "agent_update"
# - Does not touch any existing endpoint or function
#
# Cost:
# - 3 LLM calls maximum per search (Chief of Staff only)
# - Specialist agents use your existing fetch functions — free
# - Total extra cost per search: effectively zero on free models
# ─────────────────────────────────────────────────────────────────────────────

import asyncio  # already imported above — this comment is just a reminder


# ── SPECIALIST AGENT 1: SIGNAL AGENT ─────────────────────────────────────────
# Uses your existing fetch functions to dig deeper on a specific angle
# that the Chief of Staff identifies as underexplored.
# It does NOT duplicate your existing sources — it runs them on a
# MORE SPECIFIC query derived from what was already found.

async def signal_agent(specific_query: str, original_query: str) -> dict:
    """
    Runs a deeper signal search on a specific angle.

    Why a specific_query separate from original_query:
    If user searched "Nike" and the Chief of Staff noticed complaints
    about a specific product, specific_query might be "Nike Air Max defect".
    This finds signals the original broad search missed.

    Returns a dict with title, source, url for the most relevant findings.
    """
    try:
        # Import only the fastest, most reliable sources for the agent loop
        # We skip slow ones (YouTube, Play Store) to keep the loop responsive
        results = []

        reddit_results = await asyncio.to_thread(fetch_reddit, specific_query)
        results += reddit_results[:5]  # Top 5 only — quality over quantity

        hn_results = await asyncio.to_thread(fetch_hackernews, specific_query)
        results += hn_results[:5]

        news_results = await asyncio.to_thread(fetch_google_news, specific_query)
        results += news_results[:5]

        # Rank using your existing scoring function
        ranked = await asyncio.to_thread(filter_and_rank, results, specific_query)

        return {
            "agent":    "signal",
            "query":    specific_query,
            "findings": ranked[:5],  # Return top 5 most relevant
            "count":    len(ranked)
        }

    except Exception as e:
        print(f"Signal agent error: {e}")
        return {"agent": "signal", "query": specific_query, "findings": [], "count": 0}


# ── SPECIALIST AGENT 2: CONTEXT AGENT ────────────────────────────────────────
# Finds academic and encyclopaedic context for the query.
# This is the agent that searches what Claude cannot — papers published
# this week, Wikipedia sections that are newly updated.

async def context_agent(query: str) -> dict:
    """
    Finds academic context and background knowledge for the query.

    Sources used:
    - Semantic Scholar: free academic search API, no key needed
    - Wikipedia: your existing fetch_wikipedia function
    - CrossRef: free academic metadata API, no key needed

    Why these:
    Semantic Scholar and CrossRef index millions of papers and return
    structured JSON. No scraping. No blocks. Completely free.
    """
    findings = []

    # ── Semantic Scholar ──────────────────────────────────────────────────────
    # Free academic search API. No API key needed.
    # Returns paper titles, abstracts, authors, citation counts, year.
    # This is genuinely what Claude cannot do — find papers from this week.
    try:
        semantic_url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={requests.utils.quote(query)}"
            f"&limit=5"
            f"&fields=title,abstract,year,citationCount,externalIds"
        )
        res = await asyncio.to_thread(
            lambda: requests.get(
                semantic_url,
                timeout=10,
                headers={"User-Agent": "signalwatch/1.0"}
            )
        )
        if res.status_code == 200:
            data = res.json()
            for paper in data.get("data", [])[:5]:
                title = paper.get("title", "")
                year  = paper.get("year", "")
                cites = paper.get("citationCount", 0)
                if title:
                    findings.append({
                        "title":   f"[Research {year}] {title} — {cites} citations",
                        "source":  "scholar",
                        "url":     f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}",
                        "created": 0
                    })
    except Exception as e:
        print(f"Context agent — Semantic Scholar error: {e}")

    # ── Wikipedia (your existing function) ───────────────────────────────────
    try:
        wiki_results = await asyncio.to_thread(fetch_wikipedia, query)
        findings += wiki_results[:2]
    except Exception as e:
        print(f"Context agent — Wikipedia error: {e}")

    # ── CrossRef — academic paper metadata ───────────────────────────────────
    # Another free academic API. Different index from Semantic Scholar.
    # Specialises in journal articles and conference papers.
    try:
        crossref_url = (
            f"https://api.crossref.org/works"
            f"?query={requests.utils.quote(query)}"
            f"&rows=3"
            f"&sort=relevance"
        )
        res = await asyncio.to_thread(
            lambda: requests.get(
                crossref_url,
                timeout=10,
                headers={"User-Agent": "signalwatch/1.0 (mailto:wajidfarooqtemp@gmail.com)"}
            )
        )
        if res.status_code == 200:
            data = res.json()
            for item in data.get("message", {}).get("items", [])[:3]:
                titles = item.get("title", [])
                title  = titles[0] if titles else ""
                year   = item.get("published", {}).get("date-parts", [[""]])[0][0]
                doi    = item.get("DOI", "")
                if title:
                    findings.append({
                        "title":   f"[Journal {year}] {title}",
                        "source":  "scholar",
                        "url":     f"https://doi.org/{doi}" if doi else "",
                        "created": 0
                    })
    except Exception as e:
        print(f"Context agent — CrossRef error: {e}")

    return {
        "agent":    "context",
        "query":    query,
        "findings": findings,
        "count":    len(findings)
    }


# ── SPECIALIST AGENT 3: RISK AGENT ───────────────────────────────────────────
# Looks for regulatory, legal, and financial risk signals.
# Uses public APIs that nobody is reading systematically.

async def risk_agent(query: str) -> dict:
    """
    Finds regulatory and risk signals from public sources.

    Sources used:
    - Companies House UK API: free, no key needed for basic search
    - SEC EDGAR full-text search: free, no key needed
    - FDA warning letters: public RSS feed, no key needed

    Why these matter:
    A regulatory warning against a brand appears here days before
    any journalist covers it. This is the earliest possible signal.
    """
    findings = []

    # ── Companies House UK ────────────────────────────────────────────────────
    # Free public API. Returns company filings, insolvency notices,
    # director changes. No API key for basic search.
    try:
        ch_url = (
            f"https://api.company-information.service.gov.uk/search/companies"
            f"?q={requests.utils.quote(query)}&items_per_page=3"
        )
        res = await asyncio.to_thread(
            lambda: requests.get(
                ch_url,
                timeout=10,
                headers={"User-Agent": "signalwatch/1.0"}
            )
        )
        if res.status_code == 200:
            data = res.json()
            for company in data.get("items", [])[:3]:
                name   = company.get("title", "")
                status = company.get("company_status", "")
                ctype  = company.get("company_type", "")
                number = company.get("company_number", "")
                if name:
                    # Flag dissolved or liquidated companies — risk signal
                    flag = "⚠ " if status in ["dissolved", "liquidation"] else ""
                    findings.append({
                        "title":   f"{flag}[Companies House] {name} — {status} ({ctype})",
                        "source":  "regulatory",
                        "url":     f"https://find-and-update.company-information.service.gov.uk/company/{number}",
                        "created": 0
                    })
    except Exception as e:
        print(f"Risk agent — Companies House error: {e}")

    # ── SEC EDGAR full-text search ────────────────────────────────────────────
    # The SEC is the US financial regulator. All public company filings
    # are searchable for free via EDGAR. No key needed.
    # This catches: earnings warnings, lawsuits, regulatory actions.
    try:
        edgar_url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q={requests.utils.quote(query)}"
            f"&dateRange=custom"
            f"&startdt={(datetime.now()-timedelta(days=90)).strftime('%Y-%m-%d')}"
            f"&enddt={datetime.now().strftime('%Y-%m-%d')}"
            f"&hits.hits._source=period_of_report,display_names,file_date,form_type"
            f"&hits.hits.total=true"
        )
        res = await asyncio.to_thread(
            lambda: requests.get(
                edgar_url,
                timeout=10,
                headers={"User-Agent": "signalwatch/1.0 wajidfarooqtemp@gmail.com"}
            )
        )
        if res.status_code == 200:
            data = res.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:3]:
                source = hit.get("_source", {})
                names  = source.get("display_names", [])
                ftype  = source.get("form_type", "")
                fdate  = source.get("file_date", "")
                # names[0] is sometimes a string, sometimes a dict depending on EDGAR's response
                # We handle both cases defensively
                if names:
                    first = names[0]
                    name = first.get("name", "") if isinstance(first, dict) else str(first)
                else:
                    name = query
                if name:
                    findings.append({
                        "title":   f"[SEC Filing {fdate}] {name} — Form {ftype}",
                        "source":  "regulatory",
                        "url":     "https://efts.sec.gov/LATEST/search-index?q=" + requests.utils.quote(query),
                        "created": 0
                    })
    except Exception as e:
        print(f"Risk agent — SEC EDGAR error: {e}")

    # ── FDA Warning Letters RSS ───────────────────────────────────────────────
    # FDA publishes warning letters as a public RSS feed.
    # If a brand received an FDA warning, it appears here first.
    try:
        fda_url = "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters-rss-feed"
        res = await asyncio.to_thread(
            lambda: requests.get(
                fda_url,
                timeout=8,
                headers={"User-Agent": "signalwatch/1.0"}
            )
        )
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            keywords = query.lower().split()[:3]  # Use first 3 words
            for item in root.iter("item"):
                title_el = item.find("title")
                link_el  = item.find("link")
                if title_el and title_el.text:
                    title = title_el.text.strip()
                    # Only include if query keywords appear in the warning
                    if any(k in title.lower() for k in keywords):
                        findings.append({
                            "title":   f"⚠ [FDA Warning] {title}",
                            "source":  "regulatory",
                            "url":     link_el.text if link_el else "",
                            "created": 0
                        })
    except Exception as e:
        print(f"Risk agent — FDA error: {e}")

    return {
        "agent":    "risk",
        "query":    query,
        "findings": findings,
        "count":    len(findings)
    }


# ── CHIEF OF STAFF AGENT ──────────────────────────────────────────────────────
# The orchestrator. Takes all existing results, decides what angles
# have not been covered, dispatches specialist agents, evaluates
# what came back, and loops if needed.
# This is what makes it an agent — the loop with evaluation.
async def competitive_agent(query: str, existing_results: list) -> dict:
    """
    Competitive Movement Agent — Agent 4.

    What it does that the other three do not:
    The first three agents investigate the queried brand itself.
    This agent looks at what competitors are doing RIGHT NOW.

    Why this matters for ROI and EBITDA:
    A brand manager seeing complaints about their delivery speed
    needs to know if a competitor just launched same-day delivery.
    That context changes the urgency and the response entirely.

    How it works:
    Step 1 — Use AI to identify 2-3 competitors from the query
    Step 2 — Search Google News and HackerNews for those competitors
    Step 3 — Return the most significant competitor moves found

    Cost: one AI call (free model) + two existing fetch functions
    """
    findings = []

    try:
        # Step 1: Ask AI who the competitors are
        # We use the top result titles as context so the AI
        # identifies relevant competitors not generic ones
        top_titles = "\n".join(
            f"- {r['title']}" for r in existing_results[:8]
        )

        competitor_prompt = f"""The brand or topic being researched is: "{query}"

Context from recent mentions:
{top_titles}

Name exactly 2 competitors. Return JSON only:
{{"competitors": ["Competitor One", "Competitor Two"]}}

No markdown. No explanation. Raw JSON only."""

        competitor_result = await asyncio.to_thread(ai_call, competitor_prompt)

        competitors = []
        if competitor_result:
            try:
                clean = re.sub(r'```[a-z]*\n?', '', competitor_result)
                clean = re.sub(r'```', '', clean).strip()
                parsed = json.loads(clean)
                competitors = parsed.get("competitors", [])[:2]
            except Exception:
                # If JSON fails, extract any capitalised words as a fallback
                # This is imperfect but better than nothing
                words = re.findall(r'\b[A-Z][a-z]+\b', competitor_result)
                competitors = words[:2]

        if not competitors:
            print("Competitive agent: could not identify competitors")
            # Return a structured response so frontend shows a clear message
            # rather than silently hiding the container
            return {
                "agent":     "competitive",
                "findings":  [],
                "count":     0,
                "synthesis": "",
                "no_competitors_found": True
            }

        print(f"Competitive agent: tracking {competitors}")

        # Step 2: Search for competitor news using existing functions
        # We reuse fetch_google_news and fetch_hackernews — no new code
        for competitor in competitors:
            try:
                # Google News for press and announcements
                news = await asyncio.to_thread(
                    fetch_google_news, competitor
                )
                # Take only the 3 most recent news items per competitor
                for item in news[:3]:
                    item["source"] = "competitive"  # relabel source
                    findings.append(item)

                # HackerNews for tech moves — important for B2B brands
                hn = await asyncio.to_thread(
                    fetch_hackernews, competitor
                )
                for item in hn[:2]:
                    item["source"] = "competitive"
                    findings.append(item)

            except Exception as e:
                print(f"Competitive agent: fetch failed for {competitor}: {e}")
                continue

        # Step 3: Synthesise what we found into one commercial insight
        if findings:
            findings_text = "\n".join(
                f"- {f['title']}" for f in findings[:6]
            )

            synthesise_prompt = f"""You are a competitive intelligence analyst.

Brand being researched: "{query}"
Competitor activity found this month:
{findings_text}

Write exactly one sentence.
State the most commercially significant thing a competitor is doing
that the brand being researched needs to know about.
Be specific. Name the competitor. State what they did.
No hedging. No dashes. Plain English."""

            synthesis = await asyncio.to_thread(ai_call, synthesise_prompt)
        else:
            synthesis = None

        # If we found competitors but no news about them, still return a useful message
        # "No news" is itself signal — it means competitors are quiet right now
        if not findings and competitors:
            return {
                "agent":       "competitive",
                "findings":    [],
                "count":       0,
                "synthesis":   f"No significant public activity found for {' or '.join(competitors)} in the last 90 days.",
                "competitors": competitors
            }

        return {
            "agent":       "competitive",
            "findings":    findings[:5],
            "synthesis":   synthesis or "",
            "count":       len(findings),
            "competitors": competitors
        }

    except Exception as e:
        print(f"Competitive agent error: {e}")
        return {"agent": "competitive", "findings": [], "count": 0}

async def chief_of_staff(query: str, existing_results: list, max_loops: int = 3):
    """
    The Chief of Staff agent. Runs continuously, yielding updates.
    This is an async generator — it yields SSE events as it works.

    Why an async generator:
    It lets us stream updates to the browser while the agent is still
    working. The user sees "Agent investigating pricing angle..." while
    the agent is actually doing it.

    max_loops: how many investigation cycles to run (default 3)
    Each loop takes ~30-60 seconds. At 3 loops, the agent runs
    for up to 3 minutes while the user reads the results.
    """

    # ── Loop state ────────────────────────────────────────────────────────────
    # The agent keeps track of what it has investigated so far
    # so it does not repeat itself across loops
    investigated_angles = set()
    all_agent_findings  = []
    loop_count          = 0

    while loop_count < max_loops:
        loop_count += 1
        print(f"Chief of Staff: Loop {loop_count}/{max_loops}")

        # ── Step 1: Chief of Staff thinks about what to investigate ──────────
        # It reads the existing results and decides what angles are missing.
        # This is one LLM call — the thinking step.

        # Build a summary of what we already know
        existing_titles = [r["title"] for r in existing_results[:10]]
        already_found   = [f["title"] for f in all_agent_findings[:5]]

        think_prompt = f"""You are a chief of staff at a brand intelligence firm.

Query: "{query}"

What we already know from {len(existing_results)} signals:
{chr(10).join(f"- {t}" for t in existing_titles[:8])}

{"What has already been investigated: " + chr(10).join(f"- {t}" for t in already_found[:5]) if already_found else ""}

Identify ONE specific angle that has NOT been covered yet.
It must be something a brand intelligence team would genuinely want to know.
It must be investigable by searching for a specific phrase or company name.

CRITICAL RULES:
- Never use the words loop, cycle, iteration, agent, or investigation in your response
- The angle must be a plain English description of what to find out
- The search_query must be a real search phrase, not a description of a task

Return JSON only:
{{
  "angle": "one plain English sentence describing what to find out",
  "search_query": "3-5 word search phrase",
  "why": "one sentence on why this matters commercially"
}}

No markdown. No backticks. Raw JSON only."""

        think_result = await asyncio.to_thread(ai_call, think_prompt)

        if not think_result:
            # AI failed — skip this loop
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'Evaluating signal patterns...', 'loop': loop_count})}\n\n"
            # Send keepalive pings every 10 seconds during the wait
            # Render closes SSE connections idle for more than 55 seconds
            # Without pings, Agent 4 never arrives because the connection dies
            for _ in range(3):
                await asyncio.sleep(10)
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            continue

        # Parse the investigation angle
        try:
            clean = re.sub(r'```[a-z]*\n?', '', think_result)
            clean = re.sub(r'```', '', clean).strip()
            investigation = json.loads(clean)
        except Exception:
            # If JSON parse fails, extract manually
            # When AI returns malformed JSON, we fall back to a generic angle.
            # We never use the word "loop" — the user sees "Agent N" not "Loop N".
            investigation = {
                "angle":        "broader sentiment and reputation patterns",
                "search_query": query,
                "why":          "Surfacing signals the initial scan may have missed"
            }

        angle        = investigation.get("angle", "")
        search_query = investigation.get("search_query", query)
        why          = investigation.get("why", "")

        # Skip if we already investigated this angle
        if search_query in investigated_angles:
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'Scanning for new angles...'})}\n\n"
            for _ in range(2):
                await asyncio.sleep(10)
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            continue

        investigated_angles.add(search_query)

        # Tell the frontend what the agent is doing right now
        # "Agent N" not "Loop N" — strategic language, not technical
        # Clean angle and why before sending to frontend
        # strip_agent_language removes any technical words the AI snuck in
        clean_angle = strip_agent_language(angle)
        clean_why   = strip_agent_language(why)
        yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'investigating', 'message': f'Agent {loop_count} — {clean_angle}', 'why': clean_why, 'loop': loop_count})}\n\n"

        # ── Step 2: Run all three specialists simultaneously ──────────────────
        # asyncio.gather runs all three at the same time
        # Total wait = slowest agent, not sum of all three
        signal_task      = signal_agent(search_query, query)
        context_task     = context_agent(search_query)
        risk_task        = risk_agent(search_query)
        # Agent 4 runs on every loop alongside the other three.
        # It uses the original query and existing_results to find competitors.
        # It does not use search_query because competitors relate to the
        # original brand, not the specific angle being investigated.
        # competitive_agent is NOT in this loop
        # It runs separately after all loops complete — see below
        signal_result, context_result, risk_result = await asyncio.gather(
            signal_task,
            context_task,
            risk_task,
            return_exceptions=True
        )

        loop_findings = []

        for result in [signal_result, context_result, risk_result]:
            if isinstance(result, Exception):
                continue  # Skip failed agents silently
            if isinstance(result, dict):
                loop_findings += result.get("findings", [])

        all_agent_findings += loop_findings
        # Agent 4 — Competitive Movement Agent
        # We send a "investigating" event first so the frontend
        # knows Agent 4 is working, then "complete" with findings.
        # This also ensures the agent panel is visible before results arrive.
    
        if not loop_findings:
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'No new signals found on this angle. Trying another...'})}\n\n"
            await asyncio.sleep(10)
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            await asyncio.sleep(5)
            continue

        # ── Step 3: Chief of Staff synthesises what the agents found ─────────
        # One more LLM call to turn raw findings into a conclusion.

        findings_text = "\n".join(
            f"- [{f.get('source','')}] {f.get('title','')}"
            for f in loop_findings[:8]
        )

        synthesise_prompt = f"""You are a chief of staff writing a one-paragraph intelligence update.

Original query: "{query}"
Investigation angle: "{angle}"
Why it matters: "{why}"

What the agents found:
{findings_text}

Write exactly 2 sentences.
Sentence 1: What this specific investigation found. Be specific, not generic.
Sentence 2: What it means commercially for the brand or their competitors.

Plain British English. No hedging. No asterisks. No labels. Just 2 sentences."""

        synthesis = await asyncio.to_thread(ai_call, synthesise_prompt)

        if not synthesis:
            synthesis = f"Agents found {len(loop_findings)} signals on {angle}."

        # Stream the complete loop result to the frontend
        # Clean synthesis and angle before sending to frontend
        clean_synthesis = strip_agent_language(strip_markdown(synthesis))
        clean_angle     = strip_agent_language(angle)
        yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'complete', 'message': clean_synthesis, 'angle': clean_angle, 'findings': loop_findings[:5], 'loop': loop_count, 'total_found': len(loop_findings)})}\n\n"

        # ── Step 4: Wait before next loop ────────────────────────────────────
        # We wait 45 seconds between loops.
        # This gives the user time to read the finding before the next one arrives.
        # It also means the agent runs for ~3 minutes total — enough to be
        # genuinely useful without being annoying.
        if loop_count < max_loops:
            next_num = loop_count + 1
            # Send the exact wait seconds so the frontend can show a live countdown
            # Send a ping every 10 seconds during the 30 second wait (I have changed the timings)
            # This keeps the SSE connection alive on Render's free tier
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'waiting', 'message': f'Agent {loop_count} complete. Agent {next_num} starting shortly...', 'loop': loop_count, 'wait_seconds': 30})}\n\n"
            for _ in range(3):
                await asyncio.sleep(10)
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            await asyncio.sleep(5)

    # Agent has completed all loops
    # ── AGENT 4 runs here — after all three loop agents complete ─────────────
    # Running it here means:
    # 1. It never gets overwritten by loop iterations
    # 2. The user has had 3-4 minutes to read main results before it arrives
    # 3. It has the full existing_results context from all three loops
    # 4. Its own SSE type "agent_4" means the frontend handles it separately

    yield f"data: {json.dumps({'type': 'agent_4', 'phase': 'investigating', 'message': 'Agent 4 — tracking what competitors are doing right now'})}\n\n"

    try:
        # Run Agent 4 and a keepalive ping loop simultaneously
        # asyncio.gather runs both at the same time
        # The ping loop sends a ping every 15 seconds while Agent 4 works
        # Without this, Render closes the SSE connection before Agent 4 finishes

        async def ping_while_working():
            # Agent 4 takes roughly 60-90 seconds
            # We ping every 15 seconds = up to 6 pings = keeps connection alive
            for _ in range(6):
                await asyncio.sleep(15)
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

        # Run competitive_agent in a thread (it is a regular async function)
        comp_task = asyncio.create_task(
            competitive_agent(query, existing_results + all_agent_findings)
        )

        # Send pings while waiting for Agent 4 to complete
        # We check every 15 seconds if Agent 4 is done
        # Wait up to 120 seconds total, pinging every 10s
        # Previously 6 x 15s = 90s which was not enough
        for _ in range(12):
            if comp_task.done():
                break
            await asyncio.sleep(10)
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"

        # If still not done, wait for it — do not abandon
        if not comp_task.done():
            competitive_result = await asyncio.wait_for(comp_task, timeout=90)
        else:
            competitive_result = comp_task.result()

        if isinstance(competitive_result, dict):
            synthesis = competitive_result.get("synthesis", "")
            findings  = competitive_result.get("findings", [])
            count     = competitive_result.get("count", 0)
            
            # Clean any technical language from the synthesis
            clean_synthesis = strip_agent_language(strip_markdown(synthesis)) if synthesis else ""

            if clean_synthesis:
                yield f"data: {json.dumps({'type': 'agent_4', 'phase': 'complete', 'message': clean_synthesis, 'findings': findings[:5], 'total_found': count})}\n\n"
            elif count > 0:
                yield f"data: {json.dumps({'type': 'agent_4', 'phase': 'complete', 'message': f'Found {count} competitor signals.', 'findings': findings[:5], 'total_found': count})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'agent_4', 'phase': 'none', 'message': 'No significant competitor signals found.'})}\n\n"

    except Exception as e:
        print(f"Agent 4 error: {e}")
        yield f"data: {json.dumps({'type': 'agent_4', 'phase': 'none', 'message': 'Competitor scan unavailable.'})}\n\n"

    # Now send the finished event
    yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'finished', 'message': f'All agents complete. {len(all_agent_findings)} additional signals found.', 'total_findings': len(all_agent_findings)})}\n\n"

# ─── LEAD GENERATION AGENT ───────────────────────────────────────────────────
# Triggered when user clicks "Find Leads" button on the frontend.
# Takes the already-crawled results and scores each one for buying intent.
# Returns only high-intent leads (score 3, 4, or 5 out of 5).
#
# Why this earns money:
# A plumber searches "plumbers London complaints". They get a briefing.
# They click "Find Leads" and see 5 people who said this week they need
# a plumber in London, with a ready-to-send cold outreach message.
# That is worth paying for immediately.
#
# Cost: one AI call per result scored. We limit to top 8 results maximum.
# Free models handle this fine.

async def score_lead(mention_title: str, mention_source: str, query: str) -> dict:
    """
    Scores one mention for buying intent using your lead generation prompt.
    Returns a dict with intent_score, pain, pitch, and the original mention.
    Returns None if intent score is below 3 (not worth showing).
    """
    prompt = f"""You are a Lead Generation Sniper. Analyze this social media mention and turn it into a lead.

Platform: {mention_source}
Mention: "{mention_title}"
Context (what was searched): {query}

Perform these 3 tasks:

1. BUYING INTENT (Score 1-5): Rate how close this person is to spending money.
   1 = just complaining, 5 = actively asking for a vendor/tool right now.

2. THE CORE PAIN: Summarize exactly what problem they are trying to solve in 1 sentence.

3. THE PERFECT PITCH: Write a 3-4 line cold outreach message.
   British English. Conversational. Like a colleague who spotted something useful.
   No buzzwords. No "I hope this finds you well." No AI filler.
   Get to the point in the first sentence. Offer something specific.
   Sound like a person, not a tool.

Return JSON only:
{{
  "intent_score": <number 1-5>,
  "pain": "<one sentence>",
  "pitch": "<three sentences as one string>"
}}

No markdown. No backticks. Raw JSON only."""

    result = await asyncio.to_thread(ai_call, prompt)
    if not result:
        return None

    try:
        clean = re.sub(r'```[a-z]*\n?', '', result)
        clean = re.sub(r'```', '', clean).strip()
        parsed = json.loads(clean)

        score = int(parsed.get("intent_score", 0))

        # Only return leads with intent score 3 or above
        # Score 1-2 are complaints with no buying intent — useless to a business
        if score < 3:
            return None

        return {
            "intent_score": score,
            "pain":         parsed.get("pain", ""),
            "pitch":        parsed.get("pitch", ""),
            "mention":      mention_title,
            "source":       mention_source,
            "score_label":  ["", "", "", "Considering", "Ready to buy", "Actively seeking"][min(score, 5)]
        }

    except Exception as e:
        print(f"Lead scoring error: {e}")
        return None


@app.get("/find-leads")
async def find_leads(query: str, request: Request, token: str = ""):
    """
    Lead generation endpoint — triggered when user clicks Find Leads.

    Takes the same query the user already searched.
    Fetches fresh results from the fastest sources (Reddit + HN + Google News).
    Scores top 8 for buying intent.
    Returns only those scoring 3 or above.

    Why we fetch fresh instead of reusing results:
    The main search results are not stored anywhere (no database cost).
    A fresh fetch takes 15-20 seconds and costs nothing.
    """
    # Sanitise and validate
    query = sanitise_query(query)
    if not query:
        return {"error": "invalid query", "leads": []}

    # Resolve token — same as search endpoint
    resolved_token = None
    if token and token.startswith("sw_"):
        resolved_token = token
    elif token and token.startswith("google_"):
        id_token_str = token[len("google_"):]
        google_uid = verify_google_token(id_token_str)
        if google_uid:
            resolved_token = google_uid
    if not resolved_token:
        return {"error": "invalid token", "leads": []}

    # One lead generation per token. Ever. Not per day — ever.
    # This prevents abuse and creates urgency to pay for more.
    # We store used tokens in a separate table so it survives server restarts.
    if not try_consume_lead_allowance(resolved_token):
        return {"error": "used", "leads": [], "message": "You have used your one free lead scan."}

    try:
        # Fetch from the three fastest sources in parallel
        reddit_results, hn_results, news_results = await asyncio.gather(
            asyncio.to_thread(fetch_reddit, query),
            asyncio.to_thread(fetch_hackernews, query),
            asyncio.to_thread(fetch_google_news, query),
            return_exceptions=True
        )

        all_results = []
        for r in [reddit_results, hn_results, news_results]:
            if isinstance(r, list):
                all_results += r

        # Rank and take top 8 only
        # More than 8 AI calls would be slow and wasteful
        ranked = filter_and_rank(all_results, query)[:8]

        if not ranked:
            return {"leads": [], "message": "No mentions found to score."}

        # Score each mention for buying intent concurrently
        # asyncio.gather runs all scoring calls at the same time
        # Total time = slowest single AI call, not sum of all calls
        score_tasks = [
            score_lead(r["title"], r["source"], query)
            for r in ranked
        ]
        scored = await asyncio.gather(*score_tasks, return_exceptions=True)

        # Filter out None (low intent) and exceptions
        leads = [
            s for s in scored
            if s and isinstance(s, dict)
        ]

        # Sort by intent score — highest first
        leads.sort(key=lambda x: x["intent_score"], reverse=True)

        print(f"Lead generation: {len(ranked)} mentions scored, {len(leads)} leads found")

        return {
            "leads":   leads,
            "total":   len(leads),
            "scanned": len(ranked),
            "query":   query
        }

    except Exception as e:
        print(f"Find leads error: {e}")
        return {"error": "Lead generation failed", "leads": []}


# ─── SECURITY HEADERS ────────────────────────────────────────────────────────
# Runs on every single response automatically.
# Each header closes a specific attack vector.

from collections import defaultdict

# In-memory IP rate limiter — second layer after token rate limiting
# Prevents someone hammering the API without a valid token
_ip_log: dict = defaultdict(list)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """
    Adds security headers to every response and rate-limits by IP.

    Content-Security-Policy: browser whitelist — blocks injected scripts
    X-Frame-Options: prevents clickjacking via iframes
    X-Content-Type-Options: prevents MIME sniffing attacks
    Referrer-Policy: stops search queries leaking to third parties
    Strict-Transport-Security: forces HTTPS, prevents SSL stripping
    """
    # Allow Vercel frontend to stream data from this Render backend
    # OPTIONS = CORS preflight check the browser sends before the real request.
    # The browser asks: "is this origin allowed?"
    # We must respond with the EXACT origin that made the request, not a hardcoded one.
    # If we return "signalwatch.vercel.app" when the request came from "signalwatch.in",
    # the browser blocks it. So we read the incoming Origin header and echo it back,
    # but only if it is on our approved list.
    ALLOWED_ORIGINS = {
        "https://signalwatch.vercel.app",
        "https://www.signalwatch.in",
        "https://signalwatch.in",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    }
    if request.method == "OPTIONS":
        incoming_origin = request.headers.get("origin", "")
        allowed = incoming_origin if incoming_origin in ALLOWED_ORIGINS else "https://signalwatch.vercel.app"
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    # IP rate limit — 60 requests per hour per IP
    # IP rate limit — 60 requests per hour per IP
    # Gets the real client IP from Render's proxy headers
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"

    # Skip rate limiting for OPTIONS requests — these are CORS preflight checks
    # Blocking them breaks all cross-origin requests from the browser
    if request.url.path != "/" and request.method != "OPTIONS":
        now = datetime.now()
        cutoff = now - timedelta(hours=1)
        _ip_log[client_ip] = [t for t in _ip_log[client_ip] if t > cutoff]

        if len(_ip_log[client_ip]) >= 60:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests. Try again later."},
                headers={"Retry-After": "3600"}
            )
        _ip_log[client_ip].append(now)

    response = await call_next(request)

    # Content Security Policy
    # default-src 'self': only load from our own domain by default
    # script-src: only these exact script sources are allowed to run
    # frame-ancestors 'none': nobody can embed us in an iframe (clickjacking)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
        "https://cdnjs.cloudflare.com "
        "https://cdn.jsdelivr.net "
        "https://cloud.umami.is "
        "https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self' "
        "https://signalwatch-r6s8.onrender.com "
        "https://signalwatch-production.up.railway.app "
        "https://accounts.google.com "
        "https://oauth2.googleapis.com "
        "https://api-gateway.umami.dev; "
        "frame-src https://accounts.google.com; "
        "img-src 'self' data: https:; "
        "frame-ancestors 'none'"
    )

    # Prevents your page being embedded in iframes — blocks clickjacking
    response.headers["X-Frame-Options"] = "DENY"

    # Stops browsers guessing content types — prevents MIME confusion attacks
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Only sends your domain name in Referer header — not the full URL
    # Prevents search queries leaking to third-party scripts
    response.headers["Referrer-Policy"] = "strict-origin"

    # Forces HTTPS for 1 year — prevents SSL stripping attacks
    # Only effective when you have a custom domain with HTTPS
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )

    # Remove headers that reveal server information to attackers safely
    # MutableHeaders does not have .delete() — use dict-style deletion
    # Wrapped in try/except because some headers are read-only in certain contexts
    for header in ["Server", "X-Powered-By"]:
        try:
            del response.headers[header]
        except Exception:
            pass

    return response
    
# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
# Endpoints are the URLs your frontend can call
# @app.get("/") means: when someone visits / run this function

@app.get("/")
def home():
    # Simple health check — tells us the server is running
    return {"status": "Signalwatch running — beta"}


@app.get("/search-stream")
async def search_stream(query: str, request: Request, token: str = ""):
    # ── What this endpoint does ──────────────────────────────────────────────
    # This is the SSE (Server-Sent Events) version of search.
    # Instead of waiting for all sources to finish before responding,
    # it sends progress updates as each source completes.
    #
    # SSE format: each message must start with "data: " and end with "\n\n"
    # The browser's EventSource API reads these messages automatically.
    #
    # Example of what gets sent:
    # data: {"type": "progress", "source": "youtube", "count": 192}
    # data: {"type": "progress", "source": "newsapi", "count": 26}
    # data: {"type": "complete", "results": [...], "insight": "..."}
    # ─────────────────────────────────────────────────────────────────────────

    # Validate token first — same as regular search
    # Accept either legacy sw_ browser tokens OR new Google g_ tokens
    # Google tokens are verified against Google's servers first
    resolved_token = None

# Sanitise the query first — before rate limiting, before token checks,
    # before anything else. If the query is empty after sanitisation, reject it.
    query = sanitise_query(query)
    if not query:
        async def bad_query():
            yield f"data: {json.dumps({'type': 'error', 'message': 'invalid query'})}\n\n"
        return StreamingResponse(bad_query(), media_type="text/event-stream")
    
    if token and token.startswith("sw_"):
        # Legacy browser token — still works for backwards compat
        resolved_token = token

    elif token and token.startswith("google_"):
        # Frontend sends "google_{id_token}" — we strip the prefix and verify
        id_token_str = token[len("google_"):]
        google_uid = verify_google_token(id_token_str)
        if google_uid:
            resolved_token = google_uid
        else:
            async def invalid_google():
                yield f"data: {json.dumps({'type': 'error', 'message': 'google_auth_failed'})}\n\n"
            return StreamingResponse(invalid_google(), media_type="text/event-stream")

    if not resolved_token:
        async def invalid():
            yield f"data: {json.dumps({'type': 'error', 'message': 'invalid'})}\n\n"
        return StreamingResponse(invalid(), media_type="text/event-stream")

    token = resolved_token  # Use the resolved token for rate limiting below

    current_count = get_count(token)
    if current_count >= DAILY_LIMIT:
        async def limited():
            yield f"data: {json.dumps({'type': 'limit', 'limit_reached': True})}\n\n"
        return StreamingResponse(limited(), media_type="text/event-stream")

    new_count = increment_count(token)
    remaining = max(0, DAILY_LIMIT - new_count)

    async def generate():
        # async generator — each source runs in a thread via asyncio.to_thread()
        # asyncio.to_thread() takes a blocking function and runs it in a
        # background thread, then gives control back to the event loop so
        # FastAPI can actually flush the yield to the browser immediately.
        # Without this, the sync requests calls block the event loop and
        # Railway's proxy buffers everything until the function returns.

        all_posts = []
        sources_counts = {}

        yield f"data: {json.dumps({'type': 'start', 'query': query})}\n\n"

        # Each line: run the fetch in a thread, yield the progress immediately
        reddit = await asyncio.to_thread(fetch_reddit, query)
        all_posts += reddit; sources_counts["reddit"] = len(reddit)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'reddit', 'count': len(reddit), 'label': 'Reddit'})}\n\n"

        hn = await asyncio.to_thread(fetch_hackernews, query)
        all_posts += hn; sources_counts["hackernews"] = len(hn)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'hackernews', 'count': len(hn), 'label': 'Tech Forums'})}\n\n"

        newsapi = await asyncio.to_thread(fetch_newsapi, query)
        all_posts += newsapi; sources_counts["newsapi"] = len(newsapi)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'newsapi', 'count': len(newsapi), 'label': 'News'})}\n\n"

        newsdata = await asyncio.to_thread(fetch_newsdata, query)
        all_posts += newsdata; sources_counts["newsdata"] = len(newsdata)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'newsdata', 'count': len(newsdata), 'label': 'Global News'})}\n\n"

        rss = await asyncio.to_thread(fetch_rss, query)
        all_posts += rss; sources_counts["rss"] = len(rss)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'rss', 'count': len(rss), 'label': 'RSS'})}\n\n"

        youtube = await asyncio.to_thread(fetch_youtube, query)
        all_posts += youtube; sources_counts["youtube"] = len(youtube)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'youtube', 'count': len(youtube), 'label': 'YouTube'})}\n\n"

        trustpilot = await asyncio.to_thread(fetch_trustpilot, query)
        all_posts += trustpilot; sources_counts["trustpilot"] = len(trustpilot)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'trustpilot', 'count': len(trustpilot), 'label': 'Trustpilot'})}\n\n"

        appstore = await asyncio.to_thread(fetch_appstore, query)
        all_posts += appstore; sources_counts["appstore"] = len(appstore)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'appstore', 'count': len(appstore), 'label': 'App Store'})}\n\n"

        playstore = await asyncio.to_thread(fetch_playstore, query)
        all_posts += playstore; sources_counts["playstore"] = len(playstore)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'playstore', 'count': len(playstore), 'label': 'Play Store'})}\n\n"

        mastodon = await asyncio.to_thread(fetch_mastodon, query)
        all_posts += mastodon; sources_counts["mastodon"] = len(mastodon)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'mastodon', 'count': len(mastodon), 'label': 'Mastodon'})}\n\n"

        wikipedia = await asyncio.to_thread(fetch_wikipedia, query)
        all_posts += wikipedia; sources_counts["wikipedia"] = len(wikipedia)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'wikipedia', 'count': len(wikipedia), 'label': 'Wikipedia'})}\n\n"

        googlenews = await asyncio.to_thread(fetch_google_news, query)
        all_posts += googlenews; sources_counts["googlenews"] = len(googlenews)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'googlenews', 'count': len(googlenews), 'label': 'Google News'})}\n\n"

        bingnews = await asyncio.to_thread(fetch_bing_news, query)
        all_posts += bingnews; sources_counts["bingnews"] = len(bingnews)
        yield f"data: {json.dumps({'type': 'progress', 'source': 'bingnews', 'count': len(bingnews), 'label': 'Bing News'})}\n\n"

        yield f"data: {json.dumps({'type': 'analysing', 'message': 'Generating intelligence briefing'})}\n\n"

        ranked  = await asyncio.to_thread(filter_and_rank, all_posts, query)
        insight = await asyncio.to_thread(generate_insight, ranked, query)

        final = {
            "type":                "complete",
            "query":               query,
            "total":               len(ranked),
            "searches_remaining":  remaining,
            "sources":             sources_counts,
            "insight":             insight.get("briefing", "") if isinstance(insight, dict) else insight,
            "action":              insight.get("action", "")   if isinstance(insight, dict) else "",
            "questions":           insight.get("questions", []) if isinstance(insight, dict) else [],
            "patterns":            insight.get("patterns", [])  if isinstance(insight, dict) else [],
            "cooccurrences_found": insight.get("cooccurrences_found", 0) if isinstance(insight, dict) else 0,
            "results":             ranked[:20],
            "word_frequencies":    get_word_frequencies(ranked[:50])
        }
        yield f"data: {json.dumps(final)}\n\n"

        # ── CHIEF OF STAFF AGENT ──────────────────────────────────────────────
        # Starts after the main results are delivered to the user.
        # Runs continuously while the SSE connection stays open.
        # The connection stays open as long as the user is on the page.
        # When they leave, the browser closes the connection and this stops.
        # max_loops=3 means up to 3 investigation cycles (~3 minutes total)
        async for agent_event in chief_of_staff(query, ranked, max_loops=3):
            yield agent_event

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            # These headers are required for SSE to work properly
            # Cache-Control: no-cache means do not store these events
            # Connection: keep-alive keeps the connection open while streaming
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no"  # Tells nginx not to buffer — send immediately
        }
    )

@app.get("/search")
def search(query: str, request: Request, token: str = ""):
    # Main search endpoint — called by the frontend when user searches
    # query: what the user typed
    # request: contains information about who is making the request
    # token: the browser's unique identifier (for rate limiting)

    # Validate the token — must start with "sw_"
    # Same sanitisation as the streaming endpoint — clean the query first
    query = sanitise_query(query)
    if not query:
        return {"error": "invalid query"}
    resolved_token = None
    if token and token.startswith("sw_"):
        resolved_token = token
    elif token and token.startswith("google_"):
        id_token_str = token[len("google_"):]
        google_uid = verify_google_token(id_token_str)
        if google_uid:
            resolved_token = google_uid
        else:
            return {"error": "google_auth_failed", "limit_reached": False}
    if not resolved_token:
        return {"error": "invalid", "limit_reached": True}
    token = resolved_token

    # Check how many searches this token has done today
    current_count = get_count(token)

    if current_count >= DAILY_LIMIT:
        return {"error": "limit", "limit_reached": True}

    # Add 1 to their count and get the new total
    new_count = increment_count(token)
    remaining = max(0, DAILY_LIMIT - new_count)
    print(f"Token search {new_count}/{DAILY_LIMIT}")

    # Fetch from all sources simultaneously (Python runs them in sequence
    # but each has a timeout so slow sources do not block fast ones)
    reddit     = fetch_reddit(query)
    hn         = fetch_hackernews(query)
    newsapi    = fetch_newsapi(query)
    newsdata   = fetch_newsdata(query)
    rss        = fetch_rss(query)
    youtube    = fetch_youtube(query)
    mastodon   = fetch_mastodon(query)
    wikipedia  = fetch_wikipedia(query)
    trustpilot = fetch_trustpilot(query)
    appstore   = fetch_appstore(query)
    playstore  = fetch_playstore(query)
    googlenews = fetch_google_news(query)
    bingnews   = fetch_bing_news(query)

    # Combine all results into one big list
    # The + operator joins lists together
    all_posts = (reddit + hn + newsapi + newsdata + rss +
                 youtube + mastodon + wikipedia + trustpilot +
                 appstore + playstore + googlenews)

    print(f"Total: {len(all_posts)} — Reddit:{len(reddit)} HN:{len(hn)} "
      f"News:{len(newsapi)} NewsData:{len(newsdata)} RSS:{len(rss)} "
      f"YT:{len(youtube)} Mastodon:{len(mastodon)} Wiki:{len(wikipedia)} "
      f"Trustpilot:{len(trustpilot)} AppStore:{len(appstore)} "
      f"PlayStore:{len(playstore)} GNews:{len(googlenews)}")

    ranked  = filter_and_rank(all_posts, query)
    insight = generate_insight(ranked, query)

    # Return everything as JSON — the frontend reads this
    return {
        "query":             query,
        "total":             len(ranked),
        "searches_remaining": remaining,
        "sources": {
            "reddit":     len(reddit),
            "hackernews": len(hn),
            "newsapi":    len(newsapi),
            "newsdata":   len(newsdata),
            "rss":        len(rss),
            "youtube":    len(youtube),
            "mastodon":   len(mastodon),
            "wikipedia":  len(wikipedia),
            "trustpilot": len(trustpilot),
            "appstore":   len(appstore),
            "playstore":  len(playstore),
            "googlenews": len(googlenews),
            "bingnews":   len(bingnews)
        },
        # insight is a dict — we extract briefing and questions separately
        "insight":           insight.get("briefing", "") if isinstance(insight, dict) else insight,
        "action":   insight.get("action", "") if isinstance(insight, dict) else "",
        "questions":         insight.get("questions", []) if isinstance(insight, dict) else [],
        "patterns_detected": insight.get("patterns", []) if isinstance(insight, dict) else [],
        "results":           ranked[:20],
        "word_frequencies":  get_word_frequencies(ranked[:50])
    }