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
    allow_origins=["*"], #* means allow all websites to talk to this server - we can lock this down later if needed 
    allow_methods=["*"], #allow 
    allow_headers=["*"], #allow all types of requests (Get, post)
    allow_credentials=False, # allow 
    expose_headers=["*"],
)

# ─── API KEYS ────────────────────────────────────────────────────────────────
# os.getenv() reads environment variables — these are stored securely on Railway
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


# ─── DATA SOURCES ─────────────────────────────────────────────────────────────
# Each fetch_ function goes to one data source and returns a list of results
# Every result is a dictionary with these keys:
#   title:   the headline or text of the post/article/review
#   source:  which platform it came from (e.g. "reddit", "youtube")
#   url:     link to the original content
#   created: when it was published, as a Unix timestamp (seconds since 1970)
#            0 means we do not know the date

def fetch_reddit(query):
    # Uses Reddit's own public JSON API — no API key needed.
    # reddit.com/search.json is a public endpoint that works from any server.
    # The only requirement is a custom User-Agent header — Reddit blocks
    # requests that use the default "python-requests" user agent.
    results = []

    try:
        cutoff = datetime.now() - timedelta(days=90)

        # t=month means last month; we filter stricter below
        url = (
            f"https://www.reddit.com/search.json"
            f"?q={requests.utils.quote(query)}"
            f"&sort=relevance"
            f"&t=month"
            f"&limit=100"
            f"&raw_json=1"
        )

        res = requests.get(
            url,
            timeout=15,
            headers={
                # Reddit requires a descriptive User-Agent
                # Format: AppName/Version (reason; contact)
                "User-Agent": "signalwatch/1.0 (brand intelligence tool; contact wajidfarooq3@gmail.com)"
            }
        )

        if res.status_code == 429:
            print("Reddit: rate limited")
            return []

        if res.status_code != 200:
            print(f"Reddit JSON API: status {res.status_code}")
            return []

        data = res.json()
        posts = data.get("data", {}).get("children", [])

        if not posts:
            print(f"Reddit: no results for '{query}'")
            return []

        seen = set()

        for post in posts:
            d = post.get("data", {})
            title = d.get("title", "")

            if not title or title in seen:
                continue
            seen.add(title)

            created = d.get("created_utc", 0)
            try:
                created = int(float(created))
            except:
                created = 0

            if created and datetime.fromtimestamp(created) < cutoff:
                continue

            subreddit = d.get("subreddit", "")
            post_id   = d.get("id", "")
            post_url  = f"https://reddit.com/r/{subreddit}/comments/{post_id}/" if post_id else ""

            results.append({
                "title":   title,
                "source":  "reddit",
                "url":     post_url,
                "created": created
            })

        print(f"Reddit (JSON API): {len(results)} posts")
        return results

    except Exception as e:
        print(f"Reddit JSON API error: {e}")
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
    # Reads RSS feeds from major news outlets
    # RSS (Really Simple Syndication) is a public format news sites use
    # No API key needed — it is like a public noticeboard
    feeds = [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.theguardian.com/theguardian/world/rss",
        "https://feeds.skynews.com/feeds/rss/world.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    ]
    results = []
    keywords = query.lower().split()  # Split query into individual words

    for feed_url in feeds:
        try:
            res = requests.get(feed_url, timeout=8, headers={"User-Agent": "signalwatch/1.0"})
            # ET.fromstring() parses the XML content of the RSS feed
            root = ET.fromstring(res.content)

            # RSS feeds contain <item> elements, each being one article
            for item in root.iter("item"):
                title_el = item.find("title")  # Find the <title> tag inside each item
                if title_el is not None and title_el.text:
                    title = title_el.text.strip()
                    # Only include articles that contain at least one search keyword
                    if any(k in title.lower() for k in keywords):
                        results.append({
                            "title":   title,
                            "source":  "rss",
                            "url":     "",
                            "created": 0
                        })
        except Exception as e:
            print(f"RSS error {feed_url}:", e)

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
        cutoff = datetime.now() - timedelta(days=30)  # Last 30 days only

        for item in root.iter("item"):
            title_el = item.find("title")
            date_el  = item.find("pubDate")
            link_el  = item.find("link")

            if not title_el or not title_el.text:
                continue

            title = title_el.text.strip()

            # Parse publication date
            ts = 0
            if date_el and date_el.text:
                try:
                    # Google News date format: "Mon, 21 Apr 2026 10:00:00 GMT"
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_el.text)
                    # Make datetime naive (remove timezone info) for comparison
                    dt_naive = dt.replace(tzinfo=None)
                    if dt_naive < cutoff:
                        continue
                    ts = int(dt.timestamp())
                except Exception:
                    pass

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
    # Fetches the current list of free AI models from OpenRouter
    # We do this dynamically instead of hardcoding model names
    # because free models change frequently — hardcoding causes breakage
    try:
        res = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10
        )
        data = res.json()
        free_models = []

        for model in data.get("data", []):
            model_id   = model.get("id", "")
            pricing    = model.get("pricing", {})
            # prompt cost of 0 means free
            prompt_cost = float(pricing.get("prompt", "1") or "1")
            if ":free" in model_id or prompt_cost == 0:
                free_models.append(model_id)

        print(f"Found {len(free_models)} free models")
        return free_models[:6]  # Use first 6 to avoid trying too many

    except Exception as e:
        print("Could not fetch model list:", e)
        # Fallback models if we cannot fetch the list
        return [
            "meta-llama/llama-3.2-3b-instruct:free",
            "qwen/qwen-2-7b-instruct:free",
            "google/gemma-2-9b-it:free"
        ]


def strip_markdown(text):
    # Removes markdown formatting characters from AI responses
    # Some models add **bold** or ## headers even when told not to
    # This function cleans all of that out
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Remove **bold**
    text = re.sub(r'\*([^*]+)\*',     r'\1', text)  # Remove *italic*
    text = re.sub(r'#{1,6}\s',        '',    text)  # Remove ## headers
    text = re.sub(r'`([^`]+)`',       r'\1', text)  # Remove `code`
    text = re.sub(r'\n{3,}',         '\n\n', text)  # Collapse extra blank lines
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
                    "HTTP-Referer":   "https://signalwatch-production.up.railway.app",
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

            # 429 = rate limited, 502/503 = server error — try next model
            if res.status_code in [429, 502, 503]:
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
            action    = parsed.get("action",    "")
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
        # Last resort: use the cleaned text as the briefing
        # But only if it does not look like raw JSON
        if not clean.startswith('{') and not clean.startswith('"briefing"'):
            briefing = clean
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

"briefing": Exactly 2 sentences. Plain British English. Conversational. No labels, no asterisks, no dashes, no brand name in quotes. No hedging words like suggests, indicates, appears, seems. Start with a specific observation from the data. Second sentence says why it matters commercially.

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

    if not briefing:
        briefing = (
            f"Found {len(results)} mentions about {query} across "
            f"{len(sources_used)} sources. Raw signals below tell the story."
        )

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
                headers={"User-Agent": "signalwatch/1.0 (mailto:wajidfarooq3@gmail.com)"}
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
                headers={"User-Agent": "signalwatch/1.0 wajidfarooq3@gmail.com"}
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
                name   = names[0].get("name", "") if names else query
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

{"What agents already investigated: " + chr(10).join(f"- {t}" for t in already_found[:5]) if already_found else ""}

Identify ONE specific angle that has NOT been covered yet.
It must be something a brand intelligence team would genuinely want to know.
It must be investigable by searching for a specific phrase or company name.

Return JSON only:
{{
  "angle": "one sentence describing what to investigate",
  "search_query": "3-5 word search query to find it",
  "why": "one sentence on why this matters commercially"
}}

No markdown. No backticks. Raw JSON only."""

        think_result = await asyncio.to_thread(ai_call, think_prompt)

        if not think_result:
            # AI failed — skip this loop
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'Evaluating signal patterns...'})}\n\n"
            await asyncio.sleep(30)
            continue

        # Parse the investigation angle
        try:
            clean = re.sub(r'```[a-z]*\n?', '', think_result)
            clean = re.sub(r'```', '', clean).strip()
            investigation = json.loads(clean)
        except Exception:
            # If JSON parse fails, extract manually
            investigation = {
                "angle":        f"Loop {loop_count} investigation",
                "search_query": query,
                "why":          "Deeper signal analysis"
            }

        angle        = investigation.get("angle", "")
        search_query = investigation.get("search_query", query)
        why          = investigation.get("why", "")

        # Skip if we already investigated this angle
        if search_query in investigated_angles:
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'Scanning for new angles...'})}\n\n"
            await asyncio.sleep(20)
            continue

        investigated_angles.add(search_query)

        # Tell the frontend what the agent is doing right now
        yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'investigating', 'message': f'Investigating: {angle}', 'why': why, 'loop': loop_count})}\n\n"

        # ── Step 2: Run all three specialists simultaneously ──────────────────
        # asyncio.gather runs all three at the same time
        # Total wait = slowest agent, not sum of all three
        signal_task  = signal_agent(search_query, query)
        context_task = context_agent(search_query)
        risk_task    = risk_agent(search_query)

        signal_result, context_result, risk_result = await asyncio.gather(
            signal_task,
            context_task,
            risk_task,
            return_exceptions=True  # If one fails, others still complete
        )

        # Collect all findings from this loop
        loop_findings = []

        for result in [signal_result, context_result, risk_result]:
            if isinstance(result, Exception):
                continue  # Skip failed agents silently
            if isinstance(result, dict):
                loop_findings += result.get("findings", [])

        all_agent_findings += loop_findings

        if not loop_findings:
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'thinking', 'message': 'No new signals found on this angle. Trying another...'})}\n\n"
            await asyncio.sleep(15)
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
        yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'complete', 'message': synthesis, 'angle': angle, 'findings': loop_findings[:5], 'loop': loop_count, 'total_found': len(loop_findings)})}\n\n"

        # ── Step 4: Wait before next loop ────────────────────────────────────
        # We wait 45 seconds between loops.
        # This gives the user time to read the finding before the next one arrives.
        # It also means the agent runs for ~3 minutes total — enough to be
        # genuinely useful without being annoying.
        if loop_count < max_loops:
            yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'waiting', 'message': f'Agent digesting findings. Next investigation in 45 seconds...', 'loop': loop_count})}\n\n"
            await asyncio.sleep(45)

    # Agent has completed all loops
    yield f"data: {json.dumps({'type': 'agent_update', 'phase': 'finished', 'message': f'Agent completed {loop_count} investigation cycles. {len(all_agent_findings)} additional signals found.', 'total_findings': len(all_agent_findings)})}\n\n"

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