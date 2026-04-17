# Signalwatch — Project Memory

## What this project is

Signalwatch is a real-time brand intelligence decision engine built after being fired from Brandwatch.

USP in one sentence: Every other social listening tool tells you what people are saying. Signalwatch tells you what it means and what to do about it in the next 24-48 hours.

LinkedIn story: "I got fired from Brandwatch. On my last day I started building the free version."

---

## The gap we fill

Brandwatch and competitors (Meltwater, Talkwalker, Sprinklr):
- Cost $800–$40,000 per year
- Require boolean syntax expertise
- Show dashboards and charts
- Tell you WHAT is happening
- Require weeks of onboarding

Signalwatch:
- Free to use
- Plain English input
- Shows a decision briefing, not a dashboard
- Tells you WHAT to do and WHAT happens if you ignore it
- Works in seconds with no training

The five-part briefing structure: Situation → Significance → Momentum → Decision → Risk if ignored.
The Decision sentence is extracted and shown in a separate green "Recommended action" box.
This box is the product differentiator. No other tool has it.

---

## Tech stack

Backend: Python, FastAPI, Uvicorn
Deployed: Railway (free tier)
Start command: uvicorn app:app --host 0.0.0.0 --port $PORT

Frontend: HTML, CSS, vanilla JavaScript, Chart.js
Deployed: Vercel (free tier, root directory = frontend)

AI layer: OpenRouter API, model = openrouter/auto (auto-selects best free model)

Analytics: Umami (cloud.umami.is, free, no cookies)

---

## Data sources

- Reddit — public JSON API, no key, returns 25 posts
- HackerNews — Algolia API, no key, returns 25 stories
- NewsAPI — free tier 100/day, key required
- NewsData.io — free tier 200/day, key required
- RSS — BBC, Guardian, Sky News, Al Jazeera, no key, unlimited
- YouTube Data API v3 — Google Cloud, free 10k units/day, key required
- Wikipedia — opensearch API, no key

---

## Environment variables (stored on Railway, never in code)

OPENROUTER_API_KEY
NEWS_API_KEY
NEWSDATA_API_KEY
YOUTUBE_API_KEY

---

## File structure



---

## Scoring logic

Each result scored by:
- Each keyword found in title: +2 per occurrence
- All keywords present together: +3 bonus
- Exact quoted phrase match: +5
- Score 0 = dropped from results

---

## Boolean support

- NOT word: excludes results
- "phrase": exact match, higher score
- OR word: alternative terms
- Limitation: live API calls, no indexing, true NEAR proximity not possible

---

## Rate limiting

10 searches per IP per day. Resets at midnight. In-memory only, resets on server restart. Prevents API quota abuse.

---

## AI prompt structure

Prompt asks for 5 sentences with strict labels:
1. SITUATION — what is happening right now
2. SIGNIFICANCE — why it matters if ignored
3. MOMENTUM — accelerating, stable, or fading
4. DECISION — one specific action in next 24-48 hours (extracted to green box)
5. RISK IF IGNORED — concrete cost of inaction

Rules: no hedging, no "it appears", write like $500/hour analyst

---

## Known issues

- RSS: Reuters and AP URLs broken, replaced with Sky News and Al Jazeera
- Wikipedia: returns 0 for generic words, works for proper nouns
- Rate limiting not persistent across server restarts
- No authentication yet
- OpenRouter free models occasionally rate limited (429), auto rotates

---

## Deployment notes

Railway: app.py and Procfile must be at repo ROOT, not in a subfolder. No root directory setting needed.
Vercel: root directory must be set to frontend.
Both redeploy automatically on git push.

---

## Next planned features

1. Signal memory — compare current vs previous searches on same topic
2. Change detection alerts
3. Multi-brand comparison (run two queries side by side)
4. Persistent rate limiting with database
5. User accounts and saved searches

---

## How to resume in a new chat

Share this file and say: Continue building Signalwatch. Read claude.md for full context. Current status: backend and frontend deployed and working. Last thing done: new decision-engine UI, new 5-part AI prompt, professional dashboard design.