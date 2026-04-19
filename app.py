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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "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")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "3ec90513ea2f485fbcc255116b5016aa")
NEWSDATA_API_KEY = os.getenv("NEWSDATA_API_KEY", "pub_b1d9ab0b879247059f926aad8f4b0d48")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "AIzaSyBbcFJq-jkQYAjujpBpbcL0vng5l-ZWv7Q")

DAILY_LIMIT = 3
COUNTS_FILE = "/tmp/search_counts.json"

def load_counts():
    try:
        with open(COUNTS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_counts(counts):
    try:
        with open(COUNTS_FILE, "w") as f:
            json.dump(counts, f)
    except:
        pass

def fetch_reddit(query):
    url = f"https://www.reddit.com/search.json?q={requests.utils.quote(query)}&limit=25&sort=new"
    headers = {"User-Agent": "signalwatch/1.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        cutoff = datetime.now() - timedelta(days=90)
        results = []
        for item in data["data"]["children"]:
            d = item["data"]
            created = d.get("created_utc", 0)
            if created and datetime.fromtimestamp(created) < cutoff:
                continue
            results.append({
                "title": d["title"],
                "source": "reddit",
                "url": f"https://reddit.com{d['permalink']}",
                "created": created
            })
        return results
    except Exception as e:
        print("Reddit error:", e)
        return []

def fetch_hackernews(query):
    url = f"https://hn.algolia.com/api/v1/search?query={requests.utils.quote(query)}&tags=story&hitsPerPage=25"
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
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}",
                "created": created
            })
        return results
    except Exception as e:
        print("HackerNews error:", e)
        return []

def fetch_newsapi(query):
    from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    url = f"https://newsapi.org/v2/everything?q={requests.utils.quote(query)}&pageSize=25&language=en&sortBy=publishedAt&from={from_date}&apiKey={NEWS_API_KEY}"
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
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={requests.utils.quote(query)}&type=video&maxResults=25&order=date&publishedAfter={published_after}&key={YOUTUBE_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        results = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            if not snippet.get("title"):
                continue
            published = snippet.get("publishedAt", "")
            ts = 0
            if published:
                try:
                    dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                    ts = int(dt.timestamp())
                except:
                    pass
            results.append({
                "title": snippet["title"],
                "source": "youtube",
                "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
                "created": ts
            })
        return results
    except Exception as e:
        print("YouTube error:", e)
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
                    "title": f"Wikipedia: {title} — {desc[:100]}",
                    "source": "wikipedia",
                    "url": f"https://en.wikipedia.org/wiki/{requests.utils.quote(title)}",
                    "created": 0
                })
    except Exception as e:
        print("Wikipedia error:", e)
    return results

def ai_call(prompt):
    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://signalwatch.vercel.app",
                "X-Title": "Signalwatch"
            },
            json={
                "model": "openrouter/auto",
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        data = res.json()
        text = data["choices"][0]["message"]["content"]
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        return text.strip()
    except Exception as e:
        print("AI error:", e)
        return None

def filter_relevant(posts, query):
    if not posts:
        return []
    titles = [p["title"] for p in posts[:30]]
    titles_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = f"""You are a signal relevance filter. The user is researching: "{query}"

Below are {len(titles)} raw results. Return ONLY the numbers of results that are genuinely about "{query}" as a brand, company, product, or topic in a real business or consumer context.

Exclude:
- Gaming slang, memes, or unrelated community events
- Results where the keyword appears coincidentally
- Spam, deals aggregators with no brand context
- Results clearly not about the query topic

Return only a comma-separated list of numbers. Nothing else. Example: 1,3,5,7

Results:
{titles_text}"""

    response = ai_call(prompt)
    if not response:
        return posts

    try:
        numbers = [int(x.strip()) for x in response.split(",") if x.strip().isdigit()]
        filtered = [posts[n-1] for n in numbers if 1 <= n <= len(posts)]
        print(f"Relevance filter: {len(posts)} → {len(filtered)} results")
        return filtered if filtered else posts[:10]
    except:
        return posts

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

    for post in posts:
        text = post["title"].lower()
        if any(w in text for w in exclude):
            continue
        s = score_post(post["title"], keywords)
        if phrases:
            for p in phrases:
                if p.lower() in text:
                    s += 5
        if s == 0:
            continue
        results.append({
            "title": post["title"],
            "score": s,
            "source": post["source"],
            "url": post.get("url", ""),
            "created": post.get("created", 0)
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results

def generate_insight(results, query):
    if not results:
        return "No signal found for this query."

    today = datetime.now().strftime("%d %B %Y")
    titles = [r["title"] for r in results[:12]]
    titles_text = "\n".join(f"- {t}" for t in titles)
    sources_used = list(set(r["source"] for r in results))

    timed = [r for r in results if r.get("created", 0) > 0]
    if timed:
        newest = max(timed, key=lambda x: x["created"])
        oldest = min(timed, key=lambda x: x["created"])
        newest_date = datetime.fromtimestamp(newest["created"]).strftime("%d %b %Y")
        oldest_date = datetime.fromtimestamp(oldest["created"]).strftime("%d %b %Y")
        time_context = f"These signals span {oldest_date} to {newest_date}."
    else:
        time_context = f"Signals are from recent sources."

    prompt = f"""You are a senior brand intelligence analyst. Today is {today}.

The user searched for: "{query}"
{time_context}
Sources: {', '.join(sources_used)}

Verified recent signals:
{titles_text}

Write exactly 5 sentences. No bullet points. No markdown formatting. No asterisks. Plain text only.

Sentence 1 - SITUATION: What is concretely happening right now based only on these signals.
Sentence 2 - SIGNIFICANCE: Why this matters to a brand or business today.
Sentence 3 - MOMENTUM: Whether this is accelerating, stable, or declining.
Sentence 4 - DECISION: One specific action to take in the next 24 to 48 hours.
Sentence 5 - RISK: The concrete cost of doing nothing.

If signals are insufficient for confident analysis, say so clearly in sentence 1 and adjust accordingly. Never invent events. Never reference anything not in the signals above."""

    result = ai_call(prompt)
    return result if result else "Insight unavailable at this time."

@app.get("/")
def home():
    return {"status": "Signalwatch running"}

@app.get("/search")
def search(query: str, request: Request):
    ip = request.client.host
    today = str(date.today())

    counts = load_counts()
    if ip not in counts or counts[ip]["date"] != today:
        counts[ip] = {"count": 0, "date": today}

    if counts[ip]["count"] >= DAILY_LIMIT:
        save_counts(counts)
        return {
            "error": "limit",
            "limit_reached": True
        }

    counts[ip]["count"] += 1
    remaining = DAILY_LIMIT - counts[ip]["count"]
    save_counts(counts)
    print(f"IP {ip} — search {counts[ip]['count']}/{DAILY_LIMIT}")

    reddit = fetch_reddit(query)
    hn = fetch_hackernews(query)
    newsapi = fetch_newsapi(query)
    newsdata = fetch_newsdata(query)
    rss = fetch_rss(query)
    youtube = fetch_youtube(query)
    wikipedia = fetch_wikipedia(query)

    all_posts = reddit + hn + newsapi + newsdata + rss + youtube + wikipedia
    print(f"Sources — Reddit:{len(reddit)} HN:{len(hn)} NewsAPI:{len(newsapi)} NewsData:{len(newsdata)} RSS:{len(rss)} YouTube:{len(youtube)} Wikipedia:{len(wikipedia)}")

    relevant_posts = filter_relevant(all_posts, query)
    ranked = filter_and_rank(relevant_posts, query)
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
            "wikipedia": len(wikipedia)
        },
        "insight": insight,
        "results": ranked[:15]
    }