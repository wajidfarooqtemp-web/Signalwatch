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

OPENROUTER_API_KEY = os.getenv("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")
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
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}",
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
        "https://feeds.ft.com/rss/home/uk",
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

def strip_markdown(text):
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def ai_call(prompt):
    models_to_try = [
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openrouter/auto"
    ]
    for model in models_to_try:
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
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500
                },
                timeout=40
            )
            data = res.json()
            if "choices" not in data:
                print(f"Model {model} failed, trying next")
                continue
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = " ".join(c.get("text","") for c in content if c.get("type")=="text")
            else:
                text = content or ""
            text = strip_markdown(text.strip())
            if len(text) > 50:
                print(f"Used model: {model}")
                return text
        except Exception as e:
            print(f"AI error with {model}:", e)
            continue
    return None

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

def generate_insight(results, query):
    if not results:
        return "Nothing meaningful came up for this one — try a broader search or a different angle on the query."

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
        time_context = f"Mentions run from {oldest_date} to {newest_date}."

    prompt = f"""You are a sharp brand analyst at a London agency. Today is {today}. A client just asked about "{query}".

Here are the latest mentions from across the web ({', '.join(sources_used)}):
{titles_text}

{time_context}

Write a 4-sentence briefing in plain British English. Conversational, direct, no jargon. Like you are talking to a colleague over coffee, not writing a report.

Sentence 1: What is actually going on with {query} right now, based on these mentions.
Sentence 2: Why this matters — what is the real implication for the brand or anyone watching this space.
Sentence 3: Where this is heading — is it picking up, fading, or just ticking along.
Sentence 4: The one thing they should do about it in the next day or two.

Rules: No bullet points. No headers. No asterisks. No labels. No markdown. Just four plain sentences. Sound like a human who has read the data, not like a chatbot summarising it. If the mentions are a mixed bag or noisy, say so honestly in plain terms. Keep it under 120 words total."""

    result = ai_call(prompt)
    if not result:
        return f"Picked up {len(results)} mentions for {query} across {len(sources_used)} sources — but the briefing engine hit a snag. Raw signals are all below, sorted by relevance."
    return result

@app.get("/")
def home():
    return {"status": "Signalwatch running — beta"}

@app.get("/search")
def search(query: str, request: Request, token: str = ""):
    today = str(date.today())

    if not token or not token.startswith("sw_"):
        return {"error": "invalid", "limit_reached": True}

    counts = load_counts()

    if token not in counts or counts[token]["date"] != today:
        counts[token] = {"count": 0, "date": today}

    if counts[token]["count"] >= DAILY_LIMIT:
        save_counts(counts)
        return {"error": "limit", "limit_reached": True}

    counts[token]["count"] += 1
    remaining = DAILY_LIMIT - counts[token]["count"]
    save_counts(counts)
    print(f"Token used: {counts[token]['count']}/{DAILY_LIMIT}")

    reddit = fetch_reddit(query)
    hn = fetch_hackernews(query)
    newsapi = fetch_newsapi(query)
    newsdata = fetch_newsdata(query)
    rss = fetch_rss(query)
    youtube = fetch_youtube(query)
    mastodon = fetch_mastodon(query)
    wikipedia = fetch_wikipedia(query)

    all_posts = reddit + hn + newsapi + newsdata + rss + youtube + mastodon + wikipedia
    print(f"Total raw: {len(all_posts)} — Reddit:{len(reddit)} HN:{len(hn)} NewsAPI:{len(newsapi)} NewsData:{len(newsdata)} RSS:{len(rss)} YouTube:{len(youtube)} Mastodon:{len(mastodon)} Wiki:{len(wikipedia)}")

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