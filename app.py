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
            "source": post["source"],
            "url": post.get("url", ""),
            "created": post.get("created", 0)
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─── INSIGHT ──────────────────────────────────────────────────────────────────

def generate_insight(results, query):
    if not results:
        return "Nothing meaningful came up — try a broader search or a slightly different angle."

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

Write exactly 4 sentences in plain British English. Conversational. Like telling a colleague what you found, not writing a report. No bullet points, no headers, no asterisks, no labels like SITUATION or DECISION. Just four plain sentences under 120 words.

Sentence 1: What is actually going on with {query} right now based on these mentions.
Sentence 2: Why it matters to a brand or anyone watching this space.
Sentence 3: Whether it is picking up, fading, or just ticking along.
Sentence 4: The one thing worth doing about it in the next day or two."""

    result = ai_call(prompt)
    if not result:
        return f"Found {len(results)} mentions across {len(sources_used)} sources. The briefing engine is under heavy load right now — the raw signals below tell the story."
    return result


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

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

    all_posts = reddit + hn + newsapi + newsdata + rss + youtube + mastodon + wikipedia
    print(f"Total: {len(all_posts)} — Reddit:{len(reddit)} HN:{len(hn)} News:{len(newsapi)} NewsData:{len(newsdata)} RSS:{len(rss)} YT:{len(youtube)} Mastodon:{len(mastodon)} Wiki:{len(wikipedia)}")

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
            "wikipedia": len(wikipedia)
        },
        "insight": insight,
        "results": ranked[:20]
    }