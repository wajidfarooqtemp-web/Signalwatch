from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import date, datetime, timedelta
import requests
import re
import xml.etree.ElementTree as ET
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
NEWSDATA_API_KEY = os.getenv("NEWSDATA_API_KEY", "")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DAILY_LIMIT = 3


# ─── DATABASE ────────────────────────────────────────────────────────────────
# We use PostgreSQL on Railway to store how many searches each browser token
# has done today. This persists across server restarts — unlike the old file
# system approach which reset every time Railway restarted the server.

def get_db():
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        return conn
    except Exception as e:
        print("DB connection error:", e)
        return None

def setup_db():
    conn = get_db()
    if not conn:
        print("No DB — rate limiting will not work")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                token TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (token, search_date)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database ready")
    except Exception as e:
        print("DB setup error:", e)

def get_count(token):
    conn = get_db()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count FROM searches WHERE token = %s AND search_date = CURRENT_DATE",
            (token,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print("DB get error:", e)
        return 0

def increment_count(token):
    conn = get_db()
    if not conn:
        return 1
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO searches (token, search_date, count)
            VALUES (%s, CURRENT_DATE, 1)
            ON CONFLICT (token, search_date)
            DO UPDATE SET count = searches.count + 1
            RETURNING count
        """, (token,))
        count = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return count
    except Exception as e:
        print("DB increment error:", e)
        return 1

setup_db()


# ─── DATA SOURCES ─────────────────────────────────────────────────────────────

def fetch_reddit(query):
    results = []
    cutoff = datetime.now() - timedelta(days=90)
    headers = {"User-Agent": "signalwatch/1.0"}
    sorts = ["new", "relevance", "hot"]
    seen = set()
    for sort in sorts:
        try:
            url = f"https://www.reddit.com/search.json?q={requests.utils.quote(query)}&limit=100&sort={sort}&t=year"
            res = requests.get(url, headers=headers, timeout=10)
            data = res.json()
            for item in data["data"]["children"]:
                d = item["data"]
                title = d.get("title", "")
                if title in seen:
                    continue
                seen.add(title)
                created = d.get("created_utc", 0)
                if created and datetime.fromtimestamp(created) < cutoff:
                    continue
                results.append({
                    "title": title,
                    "source": "reddit",
                    "url": f"https://reddit.com{d['permalink']}",
                    "created": created
                })
        except Exception as e:
            print(f"Reddit {sort} error:", e)
    print(f"Reddit total: {len(results)}")
    return results

def fetch_hackernews(query):
    url = f"https://hn.algolia.com/api/v1/search?query={requests.utils.quote(query)}&tags=story&hitsPerPage=100"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        cutoff = datetime.now() - timedelta(days=90)
        results = []
        for hit in data.get("hits", []):
            if not hit.get("title"):
                continue
            created = hit.get("created_at_i", 0)
            if created and datetime.fromtimestamp(created) < cutoff:
                continue
            results.append({
                "title": hit["title"],
                "source": "hackernews",
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "created": created
            })
        return results
    except Exception as e:
        print("HackerNews error:", e)
        return []

def fetch_newsapi(query):
    from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    url = f"https://newsapi.org/v2/everything?q={requests.utils.quote(query)}&pageSize=100&language=en&sortBy=publishedAt&from={from_date}&apiKey={NEWS_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        results = []
        for a in data.get("articles", []):
            if not a.get("title") or a["title"] == "[Removed]":
                continue
            published = a.get("publishedAt", "")
            ts = 0
            if published:
                try:
                    dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                    ts = int(dt.timestamp())
                except:
                    pass
            results.append({
                "title": a["title"],
                "source": "newsapi",
                "url": a.get("url", ""),
                "created": ts
            })
        return results
    except Exception as e:
        print("NewsAPI error:", e)
        return []

def fetch_newsdata(query):
    url = f"https://newsdata.io/api/1/news?apikey={NEWSDATA_API_KEY}&q={requests.utils.quote(query)}&language=en"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        results = []
        for a in data.get("results", []):
            if not a.get("title"):
                continue
            results.append({
                "title": a["title"],
                "source": "newsdata",
                "url": a.get("link", ""),
                "created": 0
            })
        return results
    except Exception as e:
        print("NewsData error:", e)
        return []

def fetch_rss(query):
    feeds = [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.theguardian.com/theguardian/world/rss",
        "https://feeds.skynews.com/feeds/rss/world.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    ]
    results = []
    keywords = query.lower().split()
    for feed_url in feeds:
        try:
            res = requests.get(feed_url, timeout=8, headers={"User-Agent": "signalwatch/1.0"})
            root = ET.fromstring(res.content)
            for item in root.iter("item"):
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    title = title_el.text.strip()
                    if any(k in title.lower() for k in keywords):
                        results.append({"title": title, "source": "rss", "url": "", "created": 0})
        except Exception as e:
            print(f"RSS error {feed_url}:", e)
    return results

def fetch_youtube(query):
    published_after = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = []
    seen = set()
    page_token = None
    for _ in range(4):
        try:
            url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={requests.utils.quote(query)}&type=video&maxResults=50&order=date&publishedAfter={published_after}&key={YOUTUBE_API_KEY}"
            if page_token:
                url += f"&pageToken={page_token}"
            res = requests.get(url, timeout=10)
            data = res.json()
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                title = snippet.get("title", "")
                if not title or title in seen:
                    continue
                seen.add(title)
                published = snippet.get("publishedAt", "")
                ts = 0
                if published:
                    try:
                        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                        ts = int(dt.timestamp())
                    except:
                        pass
                results.append({
                    "title": title,
                    "source": "youtube",
                    "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
                    "created": ts
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            print("YouTube error:", e)
            break
    return results

def fetch_mastodon(query):
    try:
        url = f"https://mastodon.social/api/v2/search?q={requests.utils.quote(query)}&type=statuses&limit=40&resolve=false"
        res = requests.get(url, timeout=8, headers={"User-Agent": "signalwatch/1.0"})
        data = res.json()
        results = []
        cutoff = datetime.now() - timedelta(days=90)
        for status in data.get("statuses", []):
            content = status.get("content", "")
            content = re.sub(r'<[^>]+>', '', content).strip()
            if not content or len(content) < 20:
                continue
            created_at = status.get("created_at", "")
            ts = 0
            try:
                dt = datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
                ts = int(dt.timestamp())
                if dt < cutoff:
                    continue
            except:
                pass
            results.append({
                "title": content[:200],
                "source": "mastodon",
                "url": status.get("url", ""),
                "created": ts
            })
        return results
    except Exception as e:
        print("Mastodon error:", e)
        return []
    # ─── TRUSTPILOT ───────────────────────────────────────────────────────────────
# Trustpilot has a public web endpoint that returns review data.
# We search for the company name and pull recent reviews.
# No API key needed for basic public data.
# This is important because reviews are high-signal — customers who bother
# to write a review have strong opinions. This is what the Google engineer
# meant by "commercially relevant signal".

def fetch_trustpilot(query):
    # ── What this function does ──────────────────────────────────────────────
    # Fetches reviews from Trustpilot for a given brand or company name.
    # Trustpilot has a public consumer API that does not require authentication
    # for basic searches. We search for the company, find their profile,
    # then fetch their recent reviews.
    #
    # Why reviews matter: A brand with 2.1 stars has a very different story
    # than one with 4.8 stars. Trustpilot reviews are written by real customers
    # which makes them high-quality signal for brand intelligence.
    # ─────────────────────────────────────────────────────────────────────────

    results = []

    try:
        # Step 1: Search Trustpilot for the business
        # This is their public business search endpoint
        # "query" is the search term we pass in
        headers = {
            # We identify ourselves as a browser to avoid being blocked
            # Some websites block requests that do not look like browsers
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            # Accept-Language tells the server we want English content
            "Accept-Language": "en-US,en;q=0.9"
        }

        # Trustpilot's public business unit search
        # This returns JSON with matching companies
        search_url = f"https://www.trustpilot.com/api/categoriespages/search/businessunits?query={requests.utils.quote(query)}&language=en&perPage=5"

        search_res = requests.get(search_url, headers=headers, timeout=10)

        # Check if we got a successful response
        # Status code 200 means OK, anything else means something went wrong
        if search_res.status_code != 200:
            # Try the alternative approach — scrape the search page directly
            alt_url = f"https://www.trustpilot.com/search?query={requests.utils.quote(query)}"
            alt_res = requests.get(alt_url, headers=headers, timeout=10)

            # Look for business profile links in the HTML
            # Trustpilot profile URLs follow this pattern: /review/companyname.com
            profiles = re.findall(
                r'href="(/review/[a-zA-Z0-9._-]+)"',
                alt_res.text
            )

            if not profiles:
                print(f"Trustpilot: no results found for '{query}'")
                return []

            # Take the first profile found
            profile_path = profiles[0]
            print(f"Trustpilot: found profile {profile_path}")

        else:
            # Parse the JSON response from the API
            search_data = search_res.json()

            # Navigate to the list of businesses
            # .get() safely retrieves a value from a dictionary
            # If the key does not exist, it returns the default value ([] here)
            businesses = search_data.get("businessUnits", [])

            if not businesses:
                print(f"Trustpilot: no businesses found for '{query}'")
                return []

            # Get the identifier of the first business
            # identifyingName is like a slug: "nike.com" or "apple.com"
            profile_path = f"/review/{businesses[0].get('identifyingName', '')}"

        # Step 2: Fetch the actual review page for this business
        review_page_url = f"https://www.trustpilot.com{profile_path}?sort=recency"
        review_res = requests.get(review_page_url, headers=headers, timeout=10)

        # Look for the __NEXT_DATA__ script tag which contains structured data
        # This is how Next.js websites embed their data — as JSON in a script tag
        # re.search finds the first match of a pattern in a string
        # re.DOTALL makes . match newlines too (for multi-line JSON)
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            review_res.text,
            re.DOTALL
        )

        if not match:
            print("Trustpilot: could not find review data on page")
            return []

        # Parse the JSON we found inside the script tag
        page_data = json.loads(match.group(1))

        # Navigate deep into the nested structure to find reviews
        # Each .get() call goes one level deeper into the nested dictionaries
        # If any level is missing we get {} (empty dict) and continue safely
        page_props = page_data.get("props", {}).get("pageProps", {})

        # Reviews can be in different locations depending on page version
        # We try multiple possible locations
        reviews_list = (
            page_props.get("reviews", []) or
            page_props.get("businessUnit", {}).get("reviews", []) or
            []
        )

        if not reviews_list:
            # Try finding reviews another way — look for review JSON in the page
            # Trustpilot sometimes embeds individual review data differently
            review_matches = re.findall(
                r'"text":"([^"]{20,300})".*?"rating":(\d)',
                review_res.text
            )
            for text, rating in review_matches[:15]:
                results.append({
                    "title": f"[{rating}★ Trustpilot] {text}",
                    "source": "trustpilot",
                    "url": f"https://www.trustpilot.com{profile_path}",
                    "created": 0
                })
            print(f"Trustpilot: {len(results)} reviews via fallback method")
            return results

        # Process each review from the structured data
        cutoff = datetime.now() - timedelta(days=90)

        for review in reviews_list[:20]:
            # Extract the pieces we care about
            title = review.get("title", "")
            text = review.get("text", "")
            rating = review.get("rating", 0)
            date_str = review.get("dates", {}).get("publishedDate", "")

            # Combine title and text for a fuller picture
            # The colon : separates them visually
            full_text = f"{title}: {text[:150]}" if title and text else (title or text[:150])

            if not full_text or len(full_text) < 10:
                continue  # Skip empty reviews

            # Parse the publication date
            ts = 0
            if date_str:
                try:
                    # Date comes as "2026-01-15T10:30:00.000Z"
                    # We take the first 10 characters: "2026-01-15"
                    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                    if dt < cutoff:
                        continue  # Skip reviews older than 90 days
                    ts = int(dt.timestamp())
                except:
                    pass  # If date parsing fails, keep ts as 0

            # Add star rating label at the start of the title
            star_label = f"[{rating}★ Trustpilot] " if rating else "[Trustpilot] "

            results.append({
                "title": f"{star_label}{full_text}",
                "source": "trustpilot",
                "url": f"https://www.trustpilot.com{profile_path}",
                "created": ts
            })

        print(f"Trustpilot: {len(results)} reviews found")
        return results

    except Exception as e:
        print(f"Trustpilot error: {e}")
        return []
    
    def fetch_appstore(query):
    # ── What this function does ──────────────────────────────────────────────
    # Searches Apple's App Store for apps matching the query.
    # Then fetches recent reviews for the most relevant app found.
    # Apple provides a free public RSS feed for reviews — no API key needed.
    # This is valuable because App Store reviews are verified purchases,
    # meaning they are more trustworthy than random social media posts.
    # ─────────────────────────────────────────────────────────────────────────

     results = []

    try:
        # Step 1: Search the App Store for apps matching the query
        # Apple has a public search endpoint that returns JSON
        # "term" is the search query
        # "entity=software" means we want apps, not music or books
        # "limit=5" means get up to 5 app results
        search_url = f"https://itunes.apple.com/search?term={requests.utils.quote(query)}&entity=software&limit=5"

        # Make the HTTP request
        # requests.get() sends a GET request to a URL and returns the response
        search_res = requests.get(search_url, timeout=10)

        # .json() converts the response text into a Python dictionary
        search_data = search_res.json()

        # Check if we got any results
        # .get("resultCount", 0) safely gets resultCount, defaulting to 0 if missing
        if search_data.get("resultCount", 0) == 0:
            print(f"App Store: no apps found for '{query}'")
            return []

        # Get the first (most relevant) app result
        # search_data["results"] is a list of apps
        # [0] gets the first item in that list
        app = search_data["results"][0]

        # Extract the app ID — Apple uses numeric IDs like 284882215
        app_id = app.get("trackId")
        app_name = app.get("trackName", query)

        if not app_id:
            return []

        print(f"App Store: found app '{app_name}' (ID: {app_id})")

        # Step 2: Fetch reviews using Apple's RSS feed
        # Apple provides reviews as a JSON feed for any app
        # The URL structure is always the same — just change the app ID
        # "page=1" gets the first page of reviews
        # "json" at the end means get JSON format instead of XML
        review_url = f"https://itunes.apple.com/rss/customerreviews/page=1/id={app_id}/sortby=mostrecent/json"

        review_res = requests.get(review_url, timeout=10)
        review_data = review_res.json()

        # Navigate the nested data structure Apple returns
        # This is like opening a box inside a box inside a box
        # feed → entry is where the actual reviews live
        entries = review_data.get("feed", {}).get("entry", [])

        # The first entry is actually the app info, not a review
        # So we skip it by starting from index 1
        # entries[1:] means "give me everything from position 1 onwards"
        for entry in entries[1:20]:  # Maximum 20 reviews

            # Each entry has nested dictionaries with "label" keys
            # This is Apple's specific data format
            title = entry.get("title", {}).get("label", "")
            content = entry.get("content", {}).get("label", "")
            rating = entry.get("im:rating", {}).get("label", "")

            # Combine title and first 150 characters of review content
            # We limit content length so titles are not too long
            full_text = f"{title}: {content[:150]}" if title else content[:150]

            if not full_text or len(full_text) < 10:
                continue  # Skip empty or very short reviews

            # Add star rating to make it visible in results
            # f-string: the {} parts get replaced with variable values
            star_label = f"[{rating}★ App Store] " if rating else "[App Store] "

            results.append({
                "title": f"{star_label}{full_text}",
                "source": "appstore",
                # Link to the app's review page
                "url": f"https://apps.apple.com/app/id{app_id}",
                "created": 0  # Apple RSS does not always include timestamps
            })

        print(f"App Store: {len(results)} reviews found for {app_name}")
        return results

    except Exception as e:
        # If anything goes wrong, print why and return empty list
        # This way other sources still work even if App Store fails
        print(f"App Store error: {e}")
        return []
    
    def fetch_playstore(query):
    # ── What this function does ──────────────────────────────────────────────
    # Searches Google Play Store for apps matching the query.
    # Then fetches recent reviews using google-play-scraper library.
    # Play Store reviews are important because Android has the majority
    # of smartphone market share globally, especially in India and Asia.
    # ─────────────────────────────────────────────────────────────────────────

     results = []

    try:
        # Import the Play Store scraper library
        # We import inside the function so if the library fails to import,
        # only this function fails — not the whole server
        from google_play_scraper import search, reviews, Sort

        # Step 1: Search for the app on Play Store
        # search() returns a list of apps matching our query
        # n_hits=3 means get top 3 results
        # lang="en" means English language results
        # country="us" means US store
        search_results = search(
            query,
            n_hits=3,
            lang="en",
            country="us"
        )

        if not search_results:
            print(f"Play Store: no apps found for '{query}'")
            return []

        # Take the first (most relevant) result
        app = search_results[0]
        app_id = app.get("appId")  # Like "com.nike.snkrs"
        app_name = app.get("title", query)

        if not app_id:
            return []

        print(f"Play Store: found app '{app_name}' (ID: {app_id})")

        # Step 2: Fetch recent reviews for this app
        # reviews() returns a list of review dictionaries
        # count=20 means fetch 20 reviews
        # sort=Sort.NEWEST means get the most recent reviews first
        # This is important — we want current sentiment, not old reviews
        review_list, _ = reviews(
            app_id,
            count=20,
            sort=Sort.NEWEST,
            lang="en",
            country="us"
        )

        # Calculate 90-day cutoff for filtering old reviews
        cutoff = datetime.now() - timedelta(days=90)

        for review in review_list:
            # Each review has: content, score (1-5), at (date), userName
            content = review.get("content", "")
            score = review.get("score", 0)  # 1 to 5 stars
            review_date = review.get("at")  # This is a datetime object

            if not content or len(content) < 10:
                continue  # Skip empty reviews

            # Skip reviews older than 90 days
            if review_date and review_date < cutoff:
                continue

            # Convert datetime to Unix timestamp for consistency
            # All our sources use timestamps, so we keep the same format
            ts = int(review_date.timestamp()) if review_date else 0

            # Add star rating label
            star_label = f"[{score}★ Play Store] " if score else "[Play Store] "

            results.append({
                "title": f"{star_label}{content[:200]}",
                "source": "playstore",
                "url": f"https://play.google.com/store/apps/details?id={app_id}",
                "created": ts
            })

        print(f"Play Store: {len(results)} reviews found for {app_name}")
        return results

    except ImportError:
        # This means the library was not installed properly
        print("Play Store: google-play-scraper not installed")
        return []
    except Exception as e:
        print(f"Play Store error: {e}")
        return []

def fetch_wikipedia(query):
    results = []
    clean = re.sub(r'".*?"', '', query).lower()
    stop = {"not", "or", "and", "the", "a", "is", "in", "of", "to", "complaints"}
    keywords = [w for w in clean.split() if w not in stop]
    search_term = " ".join(keywords[:3])
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={requests.utils.quote(search_term)}&limit=3&format=json"
    try:
        res = requests.get(url, timeout=8)
        data = res.json()
        for title, desc in zip(data[1], data[2]):
            if desc:
                results.append({
                    "title": f"Wikipedia: {title} — {desc[:120]}",
                    "source": "wikipedia",
                    "url": f"https://en.wikipedia.org/wiki/{requests.utils.quote(title)}",
                    "created": 0
                })
    except Exception as e:
        print("Wikipedia error:", e)
    return results


# ─── AI ───────────────────────────────────────────────────────────────────────
# We fetch the live list of free models from OpenRouter instead of hardcoding
# names. Free model names change constantly. Hardcoding breaks. This way we
# always have the latest working ones.

def get_free_models():
    try:
        res = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10
        )
        data = res.json()
        free_models = []
        for model in data.get("data", []):
            model_id = model.get("id", "")
            pricing = model.get("pricing", {})
            prompt_cost = float(pricing.get("prompt", "1") or "1")
            if ":free" in model_id or prompt_cost == 0:
                free_models.append(model_id)
        print(f"Found {len(free_models)} free models")
        return free_models[:6]
    except Exception as e:
        print("Could not fetch model list:", e)
        return [
            "meta-llama/llama-3.2-3b-instruct:free",
            "qwen/qwen-2-7b-instruct:free",
            "google/gemma-2-9b-it:free"
        ]

def strip_markdown(text):
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def ai_call(prompt):
    models = get_free_models()
    print(f"Trying {len(models)} models")

    for model in models:
        try:
            print(f"Trying: {model}")
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://signalwatch.netlify.app",
                    "X-Title": "Signalwatch"
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400
                },
                timeout=40
            )

            print(f"Status {res.status_code} from {model}")

            if res.status_code in [429, 502, 503]:
                continue

            if res.status_code == 401:
                print("Bad API key — stopping")
                return None

            data = res.json()

            if "choices" not in data:
                continue

            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            else:
                text = content or ""

            text = strip_markdown(text.strip())

            if len(text) > 50:
                print(f"Got response from {model}")
                return text

        except Exception as e:
            print(f"Error with {model}: {e}")
            continue

    print("All models failed")
    return None


# ─── SCORING AND RANKING ──────────────────────────────────────────────────────

# ─── SCORE EXPLANATION ────────────────────────────────────────────────────────
# The Google engineer said scoring needs to be explainable.
# Right now a result gets a score of 9 and nobody knows why.
# This function generates a plain English reason for each score.
# Example: "Score 9 — mentions 'battery' twice and 'iphone' once, all in same title"
# This builds trust. Users understand why something ranked high.

def explain_score(title, keywords, phrases, score):
    reasons = []
    t = title.lower()
    
    # Check which keywords matched and how many times
    for w in keywords:
        count = t.count(w.lower())
        if count == 1:
            reasons.append(f"contains '{w}'")
        elif count > 1:
            reasons.append(f"mentions '{w}' {count} times")
    
    # Check if all keywords appeared together (the +3 bonus)
    if len(keywords) > 1:
        all_present = all(w.lower() in t for w in keywords)
        if all_present:
            reasons.append("all search terms in one result")
    
    # Check phrase matches
    for p in phrases:
        if p.lower() in t:
            reasons.append(f"exact phrase match: '{p}'")
    
    if not reasons:
        return ""
    
    return "Ranked high because: " + ", ".join(reasons)

def score_post(text, keywords):
    t = text.lower()
    score = 0
    for w in keywords:
        count = t.count(w.lower())
        score += count * 2
    if len(keywords) > 1:
        if sum(1 for w in keywords if w.lower() in t) == len(keywords):
            score += 3
    return score

def extract_keywords(query):
    stop = {"not", "or", "and", "the", "a", "is", "in", "of", "to"}
    phrases = re.findall(r'"(.*?)"', query)
    clean = re.sub(r'".*?"', '', query).lower()
    words = [w for w in clean.split() if w not in stop]
    return words, phrases

def filter_and_rank(posts, query):
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
        if any(w in text for w in exclude):
            continue
        s = score_post(title, keywords)
        if phrases:
            for p in phrases:
                if p.lower() in text:
                    s += 5
        if s == 0:
            continue
        results.append({
    "title": title,
    "score": s,
    "score_reason": explain_score(title, keywords, phrases, s),
    "source": post["source"],
    "url": post.get("url", ""),
    "created": post.get("created", 0)
})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─── INSIGHT ──────────────────────────────────────────────────────────────────

def generate_insight(results, query):
    if not results:
        return {
            "briefing": "Nothing meaningful came up — try a broader search or a slightly different angle.",
            "questions": []
        }

    today = datetime.now().strftime("%d %B %Y")
    titles = [r["title"] for r in results[:20]]
    titles_text = "\n".join(f"- {t}" for t in titles)
    sources_used = list(set(r["source"] for r in results))

    timed = [r for r in results if r.get("created", 0) > 0]
    time_context = ""
    if timed:
        newest = max(timed, key=lambda x: x["created"])
        oldest = min(timed, key=lambda x: x["created"])
        newest_date = datetime.fromtimestamp(newest["created"]).strftime("%d %b %Y")
        oldest_date = datetime.fromtimestamp(oldest["created"]).strftime("%d %b %Y")
        time_context = f"Mentions span {oldest_date} to {newest_date}."

    prompt = f"""You are a brand analyst at a London agency. Today is {today}. A client asked about "{query}".

Latest mentions from {', '.join(sources_used)}:
{titles_text}

{time_context}

Return a JSON object with exactly two keys: "briefing" and "questions".

"briefing": Four plain sentences in British English. Conversational, like telling a colleague what you found over coffee. No bullet points, no headers, no asterisks, no labels. Under 120 words. Cover what is happening, why it matters, where it is heading, and what to do in the next day or two.

"questions": An array of exactly 3 strategic questions that a senior executive or brand manager should be asking RIGHT NOW based purely on the patterns you see in the data above. Not generic questions. Questions that emerge directly from what these specific mentions reveal. Each question should be one sentence and feel like something a sharp CMO would ask in a board meeting. Include a one-sentence reason why that question matters based on the pattern you spotted.

Format each question as an object with "question" and "reason" keys.

Return only valid JSON. No markdown. No extra text."""

    result = ai_call(prompt)

    if not result:
        return {
            "briefing": f"Found {len(results)} mentions across {len(sources_used)} sources. The briefing engine is under heavy load right now — raw signals below tell the story.",
            "questions": []
        }

    try:
        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r'^```[a-z]*\n?', '', clean)
            clean = re.sub(r'\n?```$', '', clean)
        parsed = json.loads(clean)
        return {
            "briefing": parsed.get("briefing", ""),
            "questions": parsed.get("questions", [])
        }
    except:
        return {
            "briefing": result,
            "questions": []
        }


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

def get_word_frequencies(results):
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
        words = re.findall(r'\b[a-zA-Z]{4,}\b', r["title"].lower())
        for w in words:
            if w not in stop_words:
                freq[w] = freq.get(w, 0) + 1
    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [{"word": w, "count": c} for w, c in sorted_freq[:40]]

@app.get("/")
def home():
    return {"status": "Signalwatch running — beta"}

@app.get("/search")
def search(query: str, request: Request, token: str = ""):
    if not token or not token.startswith("sw_"):
        return {"error": "invalid", "limit_reached": True}

    current_count = get_count(token)

    if current_count >= DAILY_LIMIT:
        return {"error": "limit", "limit_reached": True}

    new_count = increment_count(token)
    remaining = max(0, DAILY_LIMIT - new_count)
    print(f"Token search {new_count}/{DAILY_LIMIT}")

    reddit = fetch_reddit(query)
    hn = fetch_hackernews(query)
    newsapi = fetch_newsapi(query)
    newsdata = fetch_newsdata(query)
    rss = fetch_rss(query)
    youtube = fetch_youtube(query)
    mastodon = fetch_mastodon(query)
    wikipedia = fetch_wikipedia(query)
    trustpilot = fetch_trustpilot(query)
    appstore = fetch_appstore(query)
    playstore = fetch_playstore(query)

    all_posts = reddit + hn + newsapi + newsdata + rss + youtube + mastodon + wikipedia + trustpilot + appstore + playstore
    print(f"Total: {len(all_posts)} — Reddit:{len(reddit)} HN:{len(hn)} News:{len(newsapi)} NewsData:{len(newsdata)} RSS:{len(rss)} YT:{len(youtube)} Mastodon:{len(mastodon)} Wiki:{len(wikipedia)} Trustpilot:{len(trustpilot)} AppStore:{len(appstore)} PlayStore:{len(playstore)}")

    ranked = filter_and_rank(all_posts, query)
    insight = generate_insight(ranked, query)

    return {
        "query": query,
        "total": len(ranked),
        "searches_remaining": remaining,
        "sources": {
            "reddit": len(reddit),
            "hackernews": len(hn),
            "newsapi": len(newsapi),
            "newsdata": len(newsdata),
            "rss": len(rss),
            "youtube": len(youtube),
            "mastodon": len(mastodon),
            "wikipedia": len(wikipedia),
            "trustpilot": len(trustpilot)
        },
        "insight": insight.get("briefing", "") if isinstance(insight, dict) else insight,
"questions": insight.get("questions", []) if isinstance(insight, dict) else [],
        "results": ranked[:20],
"word_frequencies": get_word_frequencies(ranked[:50])
    }