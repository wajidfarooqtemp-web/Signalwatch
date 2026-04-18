from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from datetime import date
import requests
import re
import xml.etree.ElementTree as ET
import os

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

import json
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
        return [
            {
                "title": item["data"]["title"],
                "source": "reddit",
                "url": f"https://reddit.com{item['data']['permalink']}",
                "created": item["data"].get("created_utc", 0)
            }
            for item in data["data"]["children"]
        ]
    except Exception as e:
        print("Reddit error:", e)
        return []


def fetch_hackernews(query):
    url = f"https://hn.algolia.com/api/v1/search?query={requests.utils.quote(query)}&tags=story&hitsPerPage=25"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        return [
            {
                "title": hit["title"],
                "source": "hackernews",
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}",
                "created": hit.get("created_at_i", 0)
            }
            for hit in data.get("hits", [])
            if hit.get("title")
        ]
    except Exception as e:
        print("HackerNews error:", e)
        return []


def fetch_newsapi(query):
    url = f"https://newsapi.org/v2/everything?q={requests.utils.quote(query)}&pageSize=25&language=en&sortBy=publishedAt&apiKey={NEWS_API_KEY}"
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
                    from datetime import datetime
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
        return [
            {
                "title": a["title"],
                "source": "newsdata",
                "url": a.get("link", "")
            }
            for a in data.get("results", [])
            if a.get("title")
        ]
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
                        results.append({"title": title, "source": "rss", "url": ""})
        except Exception as e:
            print(f"RSS error {feed_url}:", e)
    return results


def fetch_youtube(query):
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={requests.utils.quote(query)}&type=video&maxResults=25&key={YOUTUBE_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        return [
            {
                "title": item["snippet"]["title"],
                "source": "youtube",
                "url": f"https://youtube.com/watch?v={item['id']['videoId']}"
            }
            for item in data.get("items", [])
            if item.get("snippet", {}).get("title")
        ]
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
        titles = data[1]
        descriptions = data[2]
        for title, desc in zip(titles, descriptions):
            if desc:
                results.append({
                    "title": f"Wikipedia: {title} — {desc[:100]}",
                    "source": "wikipedia",
                    "url": f"https://en.wikipedia.org/wiki/{requests.utils.quote(title)}"
                })
    except Exception as e:
        print(f"Wikipedia error:", e)
    return results


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

    titles = [r["title"] for r in results[:15]]
    titles_text = "\n".join(f"- {t}" for t in titles)
    sources_used = list(set(r["source"] for r in results))

    prompt = f"""You are a senior brand intelligence analyst delivering a decision briefing to a brand manager. They searched for: "{query}"

Live signals collected from {', '.join(sources_used)}:
{titles_text}

Write a 5-sentence briefing in this exact structure:

Sentence 1 — SITUATION: What is actually happening right now based on these signals. Be specific, not vague.
Sentence 2 — SIGNIFICANCE: Why this matters to a brand or business. What is the real-world impact if ignored.
Sentence 3 — MOMENTUM: Is this signal accelerating, stable, or fading. Give a directional judgment.
Sentence 4 — DECISION: One specific action the brand should take in the next 24-48 hours. Not "monitor" — an actual decision or response.
Sentence 5 — RISK IF IGNORED: What happens if nothing is done. Make the cost of inaction concrete.

Rules: No bullet points. No hedging. No "it appears" or "it seems". Write like you are billing $500/hour and the client needs to act today. Be direct."""

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
        print("OpenRouter Status:", res.status_code)
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print("OpenRouter error:", e)
        return "Insight unavailable at this time."


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
            "error": f"Daily limit of {DAILY_LIMIT} searches reached. Come back tomorrow.",
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
            "wikipedia": len(wikipedia)
        },
        "insight": insight,
        "results": ranked[:15]
    }