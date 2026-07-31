# semantic_search.py
#
# This file adds SEMANTIC retrieval to Signalwatch, on top of the
# keyword-only matching you already have in app.py's score_post() and
# filter_and_rank().
#
# WHAT "SEMANTIC" MEANS HERE:
# Your current scoring counts exact word matches. If someone writes
# "I switched away from Nike" and your query is "Nike churn", that
# result scores 0 and gets silently dropped in filter_and_rank(),
# even though a human would immediately recognise it as relevant.
#
# Semantic search fixes this by turning both the query and every
# result title into a list of numbers, called an embedding, that
# captures MEANING, not just words. Two pieces of text that mean
# similar things end up with embeddings that are close together,
# even if they don't share a single word. We measure "closeness"
# with a simple formula called cosine similarity.
#
# WHY MISTRAL:
# You already have MISTRAL_API_KEY set up in Render as part of your
# AI fallback chain in app.py. Mistral also offers an embeddings
# endpoint on the same free account, no new signup, no new key.
# This reuses infrastructure you already have.
#
# ISOLATION:
# This file does not import from app.py and is not yet imported by
# app.py. It can be tested completely on its own, the same pattern
# already used for website_evolution.py in this project.

import os
import requests
import math

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")

# Mistral's embedding model. It turns a piece of text into a list of
# 1024 numbers. Two texts with similar meaning produce lists of
# numbers that sit close together, this is what an embedding is,
# a way of turning meaning into geometry.
EMBED_MODEL = "mistral-embed"


def get_embeddings(texts: list) -> list:
    """
    Sends a batch of text strings to Mistral and gets back one
    embedding, a list of 1024 numbers, per text, in the same order.

    Batching all texts in ONE request instead of one request per text
    is both faster and cheaper. Mistral's embeddings endpoint accepts
    a list directly.

    Returns an empty list on any failure. Callers must handle that
    the same way every other AI call in Signalwatch already handles
    a failed provider, by falling back to what you already have
    (keyword matching), never by crashing the search.
    """
    if not MISTRAL_API_KEY:
        print("semantic_search: skipped, no MISTRAL_API_KEY set")
        return []

    if not texts:
        return []

    try:
        res = requests.post(
            "https://api.mistral.ai/v1/embeddings",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": EMBED_MODEL,
                "input": texts
            },
            timeout=20
        )

        if res.status_code != 200:
            print(f"semantic_search: Mistral embeddings returned status {res.status_code}")
            return []

        data = res.json()
        # Mistral returns one object per input text, in the same order
        # we sent them, each with an "embedding" field
        embeddings = [item["embedding"] for item in data.get("data", [])]

        if len(embeddings) != len(texts):
            print(f"semantic_search: expected {len(texts)} embeddings, got {len(embeddings)}")
            return []

        return embeddings

    except Exception as e:
        print(f"semantic_search: embeddings call failed: {e}")
        return []


def cosine_similarity(vec_a: list, vec_b: list) -> float:
    """
    Measures how close two embeddings are, on a scale from -1 to 1.
    1 means the two texts mean almost exactly the same thing.
    0 means unrelated.
    Negative means opposite meaning, rare in practice for short titles.

    This is pure Python, no extra library needed, since our vectors
    are short lived, computed fresh per search, never stored, and the
    number of comparisons per search is small, a few dozen at most.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    magnitude_a = math.sqrt(sum(a * a for a in vec_a))
    magnitude_b = math.sqrt(sum(b * b for b in vec_b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)

def rescue_dropped_results(all_posts: list, ranked_titles: set, query: str,
                            max_rescue: int = 8, similarity_threshold: float = 0.72,
                            candidate_cap: int = 60) -> list:
    """
    THE ACTUAL FIX.

    filter_and_rank() in app.py drops any result with score == 0, a
    result that shares no exact word with the query, no matter how
    relevant it actually is. This function looks at exactly those
    dropped results and asks: is this one semantically close to the
    query, even without sharing a word?

    all_posts: every raw result before keyword filtering (source data)
    ranked_titles: the set of titles that keyword filtering ALREADY kept,
                   so we only spend embedding calls on what was dropped
    query: what the user searched for

    Returns a list of result dicts, in the same shape filter_and_rank()
    already produces (title, score, score_reason, source, url, created),
    so app.py can just add them onto the existing ranked list.

    Fails open: if Mistral is unavailable, this returns an empty list,
    Signalwatch behaves exactly as it does today, nothing breaks.
    """
    if not all_posts or not query:
        return []

    # Only look at posts keyword filtering DID NOT already keep —
    # no point spending an embedding call re-confirming what keyword
    # matching already found
    candidates = [
        p for p in all_posts
        if p.get("title") and p["title"] not in ranked_titles
    ]

    # Cap how many we check per search. Embedding 60 short titles in one
    # batched request is fast and cheap, embedding thousands would not be
    candidates = candidates[:candidate_cap]

    if not candidates:
        return []

    # One batched call: the query itself, plus every candidate title.
    # Batching keeps this to a single network round trip.
    texts_to_embed = [query] + [c["title"] for c in candidates]
    embeddings = get_embeddings(texts_to_embed)

    if not embeddings:
        # Mistral unavailable this round — fail open, keyword-only
        # results carry on exactly as they do today
        return []

    query_embedding = embeddings[0]
    candidate_embeddings = embeddings[1:]

    rescued = []
    for candidate, emb in zip(candidates, candidate_embeddings):
        similarity = cosine_similarity(query_embedding, emb)
        if similarity >= similarity_threshold:
            rescued.append({
                "title": candidate["title"],
                # Scaled roughly onto the same range as keyword scores
                # (which are small integers like 2, 5, 8) so it sits
                # sensibly in a results list sorted by score
                "score": round(similarity * 10, 1),
                "score_reason": f"Semantically related to \"{query}\" — no shared keyword, but same meaning",
                "source": candidate["source"],
                "url": candidate.get("url", ""),
                "created": candidate.get("created", 0)
            })

    rescued.sort(key=lambda x: x["score"], reverse=True)
    return rescued[:max_rescue]