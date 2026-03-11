"""
Input Router
============
Classifies user input into one of two intents:

  MEETING_NOTES    → run the full backlog pipeline
  GENERAL_QUESTION → hand off to the Tavily web-search agent

Classification order:
  1. Keyword pre-check (regex, zero LLM cost) — catches obvious meeting notes
  2. LLM classifier with Pydantic structured output — for ambiguous freeform text
"""
 
import os
import re
import yaml
from typing import Literal

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from llm_config import get_llm

from logger import get_logger
logger = get_logger(__name__)

# ─── Load prompt ──────────────────────────────────────────────────────────────
_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

PROMPT_ROUTER = _PROMPTS["input_router"]

# ─── Pydantic output schema ───────────────────────────────────────────────────
RouteLabel = Literal["MEETING_NOTES", "GENERAL_QUESTION"]

class RouteResult(BaseModel):
    route: RouteLabel = Field(
        description=(
            "MEETING_NOTES if the input is meeting notes, a transcript, agenda, "
            "or any text describing what was discussed or decided in a meeting. "
            "GENERAL_QUESTION for everything else — greetings, questions, advice, etc."
        )
    )
    reasoning: str = Field(
        description="One sentence explaining why this route was chosen."
    )

# ─── LLM with structured output ──────────────────────────────────────────────
_llm_router = get_llm("ROUTER").with_structured_output(RouteResult).with_retry(stop_after_attempt=3)


# ─── Keyword pre-check ────────────────────────────────────────────────────────
# Matches strong structural signals that appear in meeting notes, transcripts,
# agendas, and minutes — but almost never in conversational questions.
# If any signal is found in the first 500 chars we skip the LLM entirely.
_MEETING_SIGNALS = re.compile(
    r"\b("
    r"attendees?"
    r"|agenda"
    r"|action\s+items?"
    r"|meeting\s+(notes?|minutes?|title|summary)"
    r"|scrum|standup|stand-up"
    r"|sprint\s+(review|planning|retrospective|refinement|goal)"
    r"|retrospective"
    r"|transcri(pt|ption)"
    r"|host[:\s]"
    r"|joined\s+at"
    r"|recorded\s+by"
    r"|note[- ]?taker"
    r"|next\s+meeting"
    r"|decisions?\s+made"
    r"|discussion\s+points?"
    r"|follow[- ]?ups?"
    r"|\d{1,2}:\d{2}\s*(am|pm|AM|PM)"
    r")",
    re.IGNORECASE,
)


def _keyword_precheck(text: str) -> RouteLabel | None:
    """
    Returns MEETING_NOTES if strong structural signals are found in the first
    500 characters, otherwise None (fall through to LLM classifier).
    """
    if _MEETING_SIGNALS.search(text[:500]):
        logger.info("[Router] Keyword pre-check matched — MEETING_NOTES (skipped LLM)")
        return "MEETING_NOTES"
    return None


async def classify_input(text: str) -> RouteLabel:
    """
    Classify user input into a routing label.

    Order:
      1. Keyword pre-check — instant, zero cost, handles clear meeting notes
      2. LLM classifier    — Pydantic structured output, only for ambiguous text

    Falls back to GENERAL_QUESTION on any LLM error to avoid blocking the user.
    """
    # ── Layer 1: keyword pre-check ──
    precheck = _keyword_precheck(text)
    if precheck is not None:
        return precheck

    # ── Layer 2: LLM classifier with structured output ──
    snippet = text[:600]
    prompt  = PROMPT_ROUTER.format(user_input=snippet)

    try:
        result: RouteResult = await _llm_router.ainvoke([HumanMessage(content=prompt)])
        logger.info(f"[Router] LLM classified as: {result.route} — {result.reasoning}")
        return result.route

    except Exception as e:
        logger.error(f"[Router] Classification error — defaulting to GENERAL_QUESTION: {e}")
        return "GENERAL_QUESTION"