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

Edit 1: 
# Signalwatch — Project Memory

## What it is
Real-time brand intelligence decision engine. USP: tells you what to DO with signal data in the next 24-48 hours, not just what people are saying. Built after being fired from Brandwatch.

Tagline: From raw signal to clear decision

## Tech stack
Backend: Python + FastAPI + Uvicorn → Railway (free tier)
Frontend: HTML + CSS + JS + Chart.js → Vercel (free)
AI: OpenRouter API (openrouter/auto)
Payment: Gumroad ($4.99/month, 100 searches)

## Data sources — last 90 days only
Reddit: sort=new, 90-day timestamp cutoff
HackerNews: Algolia API, 90-day cutoff
NewsAPI: from= param last 30 days, sortBy=publishedAt
NewsData.io: latest endpoint
YouTube: publishedAfter= last 90 days, order=date
RSS: BBC, Guardian, Sky News, Al Jazeera
Wikipedia: context only, no cutoff

## AI pipeline — two calls per search
Call 1 — filter_relevant(): sends raw titles to AI, asks it to return only result numbers genuinely about the query in real business context. Removes gaming slang, coincidental mentions, spam.
Call 2 — generate_insight(): sends filtered results, returns 5-sentence briefing. No markdown, no asterisks. Prompt explicitly bans formatting symbols.

## AI prompt structure
5 plain text sentences:
SITUATION — what is concretely happening now
SIGNIFICANCE — why it matters to a brand today
MOMENTUM — accelerating, stable, or declining
DECISION — one specific action in 24-48 hours (extracted to green action box)
RISK — cost of doing nothing

Rules in prompt: no markdown, no asterisks, no invented events, state limitations honestly if data is insufficient.

## Rate limiting
3 per IP per day stored in /tmp/search_counts.json (survives Railway restarts). When limit reached: payment upgrade card shown.

## Payment
Gumroad link shown when limit reached. $4.99/month = 100 searches. Accepts USD and INR.

## File structure
Signalwatch/
├── app.py
├── requirements.txt
├── Procfile
├── claude.md
└── frontend/
    └── index.html

## Deployment
Railway: app.py and Procfile at repo ROOT. No root directory setting.
Vercel: root directory = frontend
Both redeploy on git push.

## Environment variables on Railway
OPENROUTER_API_KEY, NEWS_API_KEY, NEWSDATA_API_KEY, YOUTUBE_API_KEY

## Design
White background (#ffffff). Blue accent (#2563eb). Professional light theme. Mobile-responsive with hamburger menu. Two pages: Search and About. About page has photo with W initial fallback.

## Known issues
RSS: Reuters and AP URLs broken, using Sky News and Al Jazeera
Rate limiting shared across carrier NAT on mobile (partial mitigation only)
OpenRouter free tier rotates models, quality varies

## Next planned
Signal memory (compare today vs last week)
Multi-brand comparison endpoint
Email alerts on signal change
User accounts with persistent search history

## Resume in new chat
Share this file and say: Continue building Signalwatch. Read claude.md for context. Current status: [what you just did]