# Signalwatch — Project Memory

## What it is
Free real-time brand intelligence decision engine. USP: tells you what to DO with signal data, not just what people are saying. Built after being fired from Brandwatch.

Tagline: "From raw signal to clear decision"

## Tech stack
- Backend: Python + FastAPI + Uvicorn → deployed on Railway (free)
- Frontend: HTML + CSS + JS + Chart.js → deployed on Vercel (free)
- AI: OpenRouter API (openrouter/auto — free rotating models)
- Analytics: Umami (cloud.umami.is — free)

## Data sources (all last 90 days only)
- Reddit: public JSON API, sort=new, 90-day cutoff
- HackerNews: Algolia API, 90-day cutoff
- NewsAPI: from= param, last 30 days, sortBy=publishedAt
- NewsData.io: latest news
- YouTube: publishedAfter= param, order=date, last 90 days
- RSS: BBC, Guardian, Sky News, Al Jazeera
- Wikipedia: opensearch for context only

## File structure
Signalwatch/
├── app.py (backend)
├── requirements.txt
├── Procfile (web: uvicorn app:app --host 0.0.0.0 --port $PORT)
├── claude.md
└── frontend/
    └── index.html

## Environment variables on Railway
OPENROUTER_API_KEY, NEWS_API_KEY, NEWSDATA_API_KEY, YOUTUBE_API_KEY

## Deployment
- Railway: app.py and Procfile at repo ROOT (no subfolder setting needed)
- Vercel: root directory = frontend
- Both redeploy on git push automatically

## Rate limiting
3 searches per IP per day. Stored in /tmp/search_counts.json (survives restarts). Resets midnight.

## AI prompt structure
5 sentences: SITUATION · SIGNIFICANCE · MOMENTUM · DECISION · RISK IF IGNORED
Critical rules in prompt: only use signals from last 90 days, state limitations honestly, no recommendations based on old content. Today's date injected into prompt. Date range of signals shown to AI.

## Scoring logic
+2 per keyword occurrence in title, +3 bonus if all keywords present together, +5 for exact phrase match. Score 0 = dropped.

## Boolean support
NOT, "phrase", OR — no NEAR (no indexing)

## Frontend pages
- Search page (default): hero, stats, search box, results
- About page: photo, bio, three feature cards
- Navigation between pages via showPage() function, no reload

## Chart
Timeline scatter plot with jitter so line is never flat. Each dot = one result. Click dot = opens source URL. Falls back to bar chart if fewer than 2 timed results.

## Known issues
- RSS: Reuters/AP URLs broken, replaced with Sky News/Al Jazeera
- Wikipedia returns 0 for generic words
- Rate limiting resets if /tmp cleared (rare on Railway)
- OpenRouter 429 handled by auto model rotation

## Next planned
1. Signal memory (compare today vs last week same query)
2. Multi-brand comparison (/compare endpoint)
3. Email alerts when signal changes
4. User accounts

## How to continue
Share this file and say: "Continue building Signalwatch. Read claude.md for context. Current status: [describe what you just did]"