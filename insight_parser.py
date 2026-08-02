# insight_parser.py
#
# Replaces the regex-based JSON extraction and repair logic currently
# in app.py's extract_briefing_and_questions() and sanitise_briefing_output()
# with LangChain's schema-validated output parsing.
#
# WHAT WAS WRONG WITH THE OLD APPROACH:
# extract_briefing_and_questions() strips code fences by regex, hunts
# for the first "{" in the text, tries json.loads(), and if that fails,
# falls back to a SECOND regex trying to pull "briefing": "..." out by
# hand. sanitise_briefing_output() then checks the result against a
# hardcoded list of phrases like "^the user wants" to catch leaked
# model reasoning. This is a lot of hand-built defence against models
# not reliably returning valid JSON, and it only gets more fragile as
# more edge cases are found.
#
# WHAT THIS FILE DOES INSTEAD:
# Defines the EXACT shape a briefing must have as a Pydantic schema.
# LangChain's PydanticOutputParser turns that schema into formatting
# instructions for the prompt, and validates the model's response
# against it. If parsing fails, OutputFixingParser automatically makes
# ONE extra call, showing a model the broken output and the schema,
# and asking it to fix it, then validates again. This replaces manual
# regex repair with a documented, tested library mechanism built
# exactly for "free-tier models don't always return valid JSON."
#
# ISOLATION:
# Does not import from app.py. Not yet imported by app.py. Can be
# tested completely on its own, same pattern as website_evolution.py
# and semantic_search.py already in this project. Fails open — if
# anything goes wrong, parse_insight() returns None, and the caller
# in app.py is expected to fall back to the existing regex-based
# extract_briefing_and_questions() exactly as it does today.

import os
from typing import List, Optional
from pydantic import BaseModel, Field

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.exceptions import OutputParserException
from langchain.output_parsers import OutputFixingParser
from langchain_openai import ChatOpenAI

GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")


# ── SCHEMA ──────────────────────────────────────────────────────────────
# This is the exact shape generate_insight() in app.py already asks
# the model for in its prompt. Writing it as a Pydantic model means
# LangChain can both generate matching format instructions AND
# validate the response against them, catching a wrong shape (a
# missing field, questions as strings instead of objects, etc)
# automatically, instead of that failure surfacing later as a blank
# section on the website.

class InsightQuestion(BaseModel):
    question: str = Field(description="A strategic question the brand team should ask, based on a real pattern in the data")
    reason: str = Field(description="Why this question matters commercially, in plain English, no technical terms")


class InsightBriefing(BaseModel):
    briefing: str = Field(description="2 to 3 sentences, plain English, describing the most important pattern found in the data")
    action: str = Field(description="One sentence starting with a verb — the single most important thing to do in the next 48 hours")
    questions: List[InsightQuestion] = Field(description="Exactly 3 strategic questions the brand team should ask, each grounded in the data")


_parser = PydanticOutputParser(pydantic_object=InsightBriefing)


def get_format_instructions() -> str:
    """
    Returns text describing the required JSON shape, generated
    directly from the schema above rather than typed out by hand in
    every prompt. Append this to your existing prompt in app.py so
    the model sees exactly what shape is expected — this is the
    "format instructions" half of LangChain's output parsing pattern.
    """
    return _parser.get_format_instructions()


def _get_fixing_llm():
    """
    The model used ONLY to repair a broken response, not to generate
    the original briefing (ai_call() in app.py already did that).
    Tries Groq first since it's fast and already in your fallback
    chain, falls back to Mistral. Both expose OpenAI-compatible
    endpoints, which is why langchain_openai.ChatOpenAI works against
    either just by pointing base_url at a different provider.
    Returns None if neither key is configured — caller must handle that.
    """
    if GROQ_API_KEY:
        return ChatOpenAI(
            model="llama-3.1-8b-instant",
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
            max_tokens=700,
            timeout=20
        )
    if MISTRAL_API_KEY:
        return ChatOpenAI(
            model="mistral-small-latest",
            api_key=MISTRAL_API_KEY,
            base_url="https://api.mistral.ai/v1",
            max_tokens=700,
            timeout=20
        )
    return None


def parse_insight(raw_text: str) -> Optional[dict]:
    """
    Takes the raw text ai_call() already got back from whichever
    provider answered (OpenRouter, Groq, Cerebras, or Mistral — this
    function does not care which), and turns it into a validated dict
    with exactly the keys generate_insight() needs: briefing, action,
    questions (a list of {question, reason} dicts).

    Tries strict parsing first (fast, no extra network call). If that
    fails, because the model wrapped the JSON in prose, used the wrong
    field names, or produced invalid JSON, tries ONE repair pass via
    OutputFixingParser, which shows a small model the broken text and
    the schema, and asks it to fix it.

    Returns None if both attempts fail, or if no fixing model is
    configured. The caller (generate_insight() in app.py) is expected
    to fall back to the existing regex-based extraction in that case —
    this function is additive, not a replacement that can break search
    if something about it goes wrong.
    """
    if not raw_text:
        return None

    # First attempt — strict parsing, no extra network call.
    # This is the cheap, fast path, and will succeed whenever the
    # model actually followed the format instructions correctly.
    try:
        result = _parser.parse(raw_text)
        return _to_dict(result)
    except OutputParserException:
        pass
    except Exception as e:
        print(f"insight_parser: strict parse failed with unexpected error: {e}")

    # Second attempt — ask a model to repair the broken output.
    # This is what replaces your old regex fallback in
    # extract_briefing_and_questions().
    fixing_llm = _get_fixing_llm()
    if not fixing_llm:
        print("insight_parser: no fixing model available (GROQ_API_KEY or MISTRAL_API_KEY needed), skipping repair")
        return None

    try:
        fixing_parser = OutputFixingParser.from_llm(parser=_parser, llm=fixing_llm)
        result = fixing_parser.parse(raw_text)
        print("insight_parser: repaired malformed output via fixing model")
        return _to_dict(result)
    except Exception as e:
        print(f"insight_parser: repair attempt also failed: {e}")
        return None


def _to_dict(parsed: InsightBriefing) -> dict:
    """
    Converts the validated Pydantic object into the plain dict shape
    generate_insight() in app.py already works with everywhere else,
    so wiring this in later is a small, surgical change, not a
    rewrite of anything downstream.
    """
    return {
        "briefing": parsed.briefing,
        "action": parsed.action,
        "questions": [
            {"question": q.question, "reason": q.reason}
            for q in parsed.questions
        ]
    }