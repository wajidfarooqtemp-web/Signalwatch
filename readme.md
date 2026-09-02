# Signalwatch

Brand intelligence platform. Signalwatch pulls public mentions of a
brand across many sources, ranks them, and produces a short briefing
that says what is happening and what to do about it. It is built to
save the hour someone would otherwise spend reading mentions by hand.

## What it does

A person searches a brand or topic. Signalwatch queries a wide range of
live data sources at once, ranks the results using boolean query logic
and semantic relevance, and produces a briefing with one recommended
action, rather than a feed of links to sort through.

While the person reads that briefing, a background agent keeps working
on a specific angle the first pass did not fully cover. A separate
agent identifies named competitors and checks what they are doing
right now.

There is also a lead generation feature. It scores mentions by buying
intent and drafts outreach ready to send.

A separate feature, Website Evolution, samples historical web archive
data to show how a domain's own site has changed over time. It is
intentionally isolated from the rest of the product and shares no code
with the search pipeline.

## Search

Full boolean query support: AND, OR, NOT, parentheses for grouping, and
quoted phrases for exact matches. Ranking combines keyword relevance
with semantic similarity, so results that mean the same thing as the
query but share no exact word with it are not missed.

## Reliability

Every AI generated section of the product, the briefing, the agent
findings, the competitor summary, is validated against a fixed schema
before it reaches the user. If a response cannot be parsed, it is
repaired once automatically, and if that also fails, the section is
left empty rather than shown with something invented in its place.
There is no beta fallback path that quietly shows made up content. It
either works or it says nothing.

## Payments

Subscription and one time payments, verified entirely on the server
using each provider's own webhook signature. Access is never granted
based on anything the browser reports.

## Reliability under load

Rate limiting and AI provider cooldowns are stored in Postgres, not in
memory, so this state stays correct and consistent even if the app
ever runs as more than one process. This was a deliberate fix, not the
original design, made once the risk of running multiple processes with
private, disconnected counters was identified.

## Project layout

app.py is the main backend: search, ranking, the agent system, and API
endpoints.

payments.py handles all payment and subscription logic, kept separate
from the rest of the backend on purpose.

analytics.py tracks usage with a pseudonymous token only. No raw IP
addresses are stored, and records are deleted automatically after a
fixed retention period.

rate_limits_db.py stores IP rate limits and AI provider cooldowns in
Postgres, shared across every server process, rather than in private,
process local memory.

mcp_server.py and mcp_keys_db.py expose core features as tools an AI
assistant can call directly, with per client API keys stored as
hashes, never in plain text.

website_evolution.py is the standalone domain history feature.

semantic_search.py adds embedding based relevance, recovering results
that keyword only matching would otherwise miss.

insight_parser.py validates every AI response against a schema before
it is used, with an automatic repair step if the first response does
not parse.

boolean_search.py parses and evaluates AND, OR, NOT, and phrase queries
properly, rather than treating operators as plain text.

index.html is the frontend.

## Stack

Python backend, deployed on Render. Static frontend, deployed on
Vercel. Postgres for storage. Google OAuth for sign in.

## Status

Live product with customers. Built by one person.

Learning by building... understanding the underlying systems, making the decisions, and using LLMs strategically as a co-pilot throughout the process.