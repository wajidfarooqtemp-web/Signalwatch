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
from fastapi import FastAPI, Request   # FastAPI builds our server and handles requests
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
    allow_origins=["*"],    # "*" means allow requests from any website
    allow_methods=["*"],    # Allow any HTTP method (GET, POST, etc.)
    allow_headers=["*"],    # Allow any HTTP headers
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
    # Reddit's API is closed to new apps as of late 2025
    # Returning empty list gracefully — other 10 sources cover the signal
    print("Reddit: skipped (API access closed)")
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


# ─── INSIGHT GENERATION ───────────────────────────────────────────────────────

def generate_insight(results, query):
    # Sends the top results to AI and asks for a briefing + strategic questions
    # Returns a dictionary with "briefing" and "questions" keys
    if not results:
        return {
            "briefing":  "Nothing meaningful came up — try a broader search or a slightly different angle.",
            "questions": []
        }

    today      = datetime.now().strftime("%d %B %Y")
    titles     = [r["title"] for r in results[:20]]
    titles_text = "\n".join(f"- {t}" for t in titles)
    sources_used = list(set(r["source"] for r in results))

    timed = [r for r in results if r.get("created", 0) > 0]
    time_context = ""
    if timed:
        newest      = max(timed, key=lambda x: x["created"])
        oldest      = min(timed, key=lambda x: x["created"])
        newest_date = datetime.fromtimestamp(newest["created"]).strftime("%d %b %Y")
        oldest_date = datetime.fromtimestamp(oldest["created"]).strftime("%d %b %Y")
        time_context = f"Mentions span {oldest_date} to {newest_date}."

    prompt = f"""You are a brand analyst at a London agency. Today is {today}. A client asked about "{query}".

Latest mentions from {', '.join(sources_used)}:
{titles_text}

{time_context}

Return a JSON object with exactly two keys: "briefing" and "questions".

"briefing": Four plain sentences in British English. Conversational, like telling a colleague what you found over coffee. No bullet points, no headers, no asterisks, no labels. Under 120 words. Cover: what is happening now, why it matters, where it is heading, and what to do in the next day or two.

"questions": Array of exactly 3 strategic questions a senior executive should be asking RIGHT NOW, based purely on patterns in the data above. Not generic. Each is an object with "question" and "reason" keys.

Return only valid JSON. No markdown. No extra text outside the JSON."""

    result = ai_call(prompt)

    if not result:
        return {
            "briefing":  f"Found {len(results)} mentions across {len(sources_used)} sources. Briefing engine is under load — raw signals below tell the story.",
            "questions": []
        }

    try:
        clean = result.strip()
        # Remove markdown code fences if present
        if clean.startswith("```"):
            clean = re.sub(r'^```[a-z]*\n?', '', clean)
            clean = re.sub(r'\n?```$',       '', clean)
        parsed = json.loads(clean)
        return {
            "briefing":  parsed.get("briefing", ""),
            "questions": parsed.get("questions", [])
        }
    except:
        # If JSON parsing fails, return the raw text as the briefing
        return {
            "briefing":  result,
            "questions": []
        }


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
# Endpoints are the URLs your frontend can call
# @app.get("/") means: when someone visits / run this function

@app.get("/")
def home():
    # Simple health check — tells us the server is running
    return {"status": "Signalwatch running — beta"}


@app.get("/search")
def search(query: str, request: Request, token: str = ""):
    # Main search endpoint — called by the frontend when user searches
    # query: what the user typed
    # request: contains information about who is making the request
    # token: the browser's unique identifier (for rate limiting)

    # Validate the token — must start with "sw_"
    if not token or not token.startswith("sw_"):
        return {"error": "invalid", "limit_reached": True}

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
            "googlenews": len(googlenews)
        },
        # insight is a dict — we extract briefing and questions separately
        "insight":           insight.get("briefing", "") if isinstance(insight, dict) else insight,
        "questions":         insight.get("questions", []) if isinstance(insight, dict) else [],
        "results":           ranked[:20],
        "word_frequencies":  get_word_frequencies(ranked[:50])
    }