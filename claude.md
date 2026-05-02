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

Edit 3: # Signalwatch — Project Memory

## What it is
Real-time brand intelligence decision engine. USP: tells you what to DO with signal data in the next 24-48 hours in plain British English. Built after being fired from Brandwatch.

Tagline: Raw signal. Clear decision.

## Current status
Beta. Working and deployed. 8 sources. 100+ mentions target per search. AI briefing in informal British English.

## Tech stack
Backend: Python + FastAPI + Uvicorn → Railway (free)
Frontend: HTML + CSS + JS + Chart.js → Vercel (free)
AI: OpenRouter — tries gemini-2.0-flash-exp:free, then llama-3.3-70b:free, then openrouter/auto
Analytics: Umami (cloud.umami.is)

## Data sources — last 90 days
Reddit: 3 sort methods (new/relevance/hot), 100 per call, deduplicated → up to 200+ raw
HackerNews: Algolia API, 100 results, 90-day cutoff
NewsAPI: last 30 days, 100 results, sortBy=publishedAt
NewsData.io: latest endpoint
YouTube: up to 4 pages of 50, order=date, last 90 days
RSS: BBC, Guardian, Sky News, Al Jazeera, NYT, FT
Mastodon: mastodon.social public API, no key, 40 results, 90-day cutoff
Wikipedia: context only

## AI briefing prompt style
4 sentences. Plain British English. Conversational like a London agency colleague over coffee. No labels, no markdown, no asterisks, no jargon. Under 120 words. Sounds human.

Model fallback order: gemini-2.0-flash-exp:free → llama-3.3-70b:free → openrouter/auto

## File structure
Signalwatch/
├── app.py
├── requirements.txt (fastapi, uvicorn[standard], requests, python-dotenv)
├── Procfile (web: uvicorn app:app --host 0.0.0.0 --port $PORT)
├── claude.md
└── frontend/
    └── index.html

## Environment variables on Railway
OPENROUTER_API_KEY, NEWS_API_KEY, NEWSDATA_API_KEY, YOUTUBE_API_KEY

## Deployment
Railway: app.py and Procfile at repo ROOT, no subfolder setting
Vercel: root directory = frontend
Both redeploy on git push

## Rate limiting
3 per IP per day, stored in /tmp/search_counts.json

## Design
White background. Teal (#0d9488) primary. Amber (#d97706) accent. SVG logo with magnifying glass and signal dot. BETA badge on logo. Animated signal bars in hero. Mobile responsive with hamburger menu. Two pages: Search and About.

## Known issues
Mastodon results sparse for brand queries — works better for topics
RSS NYT and FT may occasionally block
Rate limiting shared on carrier NAT mobile — partial mitigation only
YouTube quota 10k units/day — 4 pages = uses more quota

## Next planned
Signal memory (compare today vs last week same query)
Multi-brand comparison
Email alerts on signal change

## Resume in new chat
Share this file and say: Continue building Signalwatch. Read claude.md for context. Current status: [what you just did]

SIGNALWATCH — HANDOFF v2 (post Google engineer feedback)

Live: Frontend on Vercel, Backend on Railway
Repo: GitHub — app.py at root, frontend/index.html

ARCHITECTURE:
User query → app.py fetches 10 sources → filter_and_rank scores with explanation
→ generate_insight returns JSON {briefing, questions} → get_word_frequencies
→ frontend renders: briefing card, word cloud, questions card, ranked results

SOURCES (last 90 days):
Reddit (3 sorts x 100), HackerNews (100), NewsAPI (100), NewsData (10),
RSS (BBC/Guardian/Sky/AlJazeera/NYT), YouTube (4 pages x 50),
Mastodon (40), Wikipedia, Trustpilot (20 reviews — scrapes __NEXT_DATA__)

SCORING:
+2 per keyword occurrence, +3 all keywords together, +5 exact phrase
explain_score() returns plain English reason for each score
score_reason field on every result

AI PIPELINE:
get_free_models() — fetches live free model list from OpenRouter API
ai_call() — tries each model, skips 429/404, strips markdown
generate_insight() — returns JSON: briefing (4 British sentences) + questions (3 executive Qs with reasons)
get_word_frequencies() — top 40 words, excludes stop words

RATE LIMITING:
3 per browser token per day
Token in localStorage, count in PostgreSQL on Railway
DATABASE_URL must be PUBLIC Railway URL not internal

ENVIRONMENT VARIABLES ON RAILWAY:
OPENROUTER_API_KEY, NEWS_API_KEY, NEWSDATA_API_KEY, YOUTUBE_API_KEY, DATABASE_URL

DESIGN:
White bg, teal #0d9488 primary, amber #d97706 accent
Dark circle logo with satellite dots, multicolour Signal wordmark, BETA badge
Mobile responsive, hamburger menu, Search + About pages

KNOWN ISSUES:
Reddit blocked by Railway IP (0 results) — external, unfixable
Wikipedia occasionally empty — external, unfixable
Trustpilot scraping may break if they change HTML structure

NEXT TO BUILD (in order):
1. App Store + Play Store reviews (same pattern as Trustpilot)
2. Sharper ICP — rewrite UI copy for one specific customer type
3. Integration layer — user-configurable API sources
4. Case study mode — pre-built examples

GOOGLE ENGINEER FEEDBACK RECEIVED:
- Narrow ICP, show one concrete use case end to end
- Make scoring explainable (DONE)
- Filter noise more aggressively
- Add reviews as commercially relevant signal (DONE — Trustpilot)
- Integration layer for custom sources (NEXT)
- Show concrete business problem being solved