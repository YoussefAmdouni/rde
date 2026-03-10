"""
Input Router
============
Single fast LLM call that classifies user input into one of three intents:

  MEETING_NOTES    → run the full backlog pipeline
  BACKLOG_QUERY    → (future) Pinecone Q&A  — currently treated as GENERAL_QUESTION
  GENERAL_QUESTION → hand off to the Tavily web-search agent

Only the first ~600 chars of the input are sent to keep latency and cost minimal.
"""

import os
import yaml
from typing import Literal

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from logger import get_logger
logger = get_logger(__name__)

# ─── Load prompt ──────────────────────────────────────────────────────────────
_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

PROMPT_ROUTER = _PROMPTS["input_router"]

# ─── LLM (same model, label-only output) ─────────────────────────────────────
_llm_router = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.0,
    max_output_tokens=10,
)

RouteLabel = Literal["MEETING_NOTES", "GENERAL_QUESTION"]

_VALID_LABELS: set[RouteLabel] = {"MEETING_NOTES", "GENERAL_QUESTION"}


async def classify_input(text: str) -> RouteLabel:
    """
    Classify user input into a routing label.
    Falls back to GENERAL_QUESTION on any error to avoid blocking the user.
    """
    snippet = text[:600]
    prompt  = PROMPT_ROUTER.format(user_input=snippet)

    try:
        response    = await _llm_router.ainvoke([HumanMessage(content=prompt)])
        label_raw = response.content.strip().upper().split()[0]

        # Normalise partial matches the LLM sometimes returns
        if label_raw in ("MEETING", "MEETING_NOTE", "NOTES"):
            label_raw = "MEETING_NOTES"
        elif label_raw.startswith("GENERAL") or label_raw in ("QUESTION", "GENERAL_Q"):
            label_raw = "GENERAL_QUESTION"

        if label_raw not in _VALID_LABELS:
            logger.warning(f"[Router] Unknown label '{label_raw}' — defaulting to GENERAL_QUESTION")
            return "GENERAL_QUESTION"

        label: RouteLabel = label_raw  # type: ignore[assignment]
        logger.info(f"[Router] Classified as: {label}")
        return label

    except Exception as e:
        logger.error(f"[Router] Classification error — defaulting to GENERAL_QUESTION: {e}")
        return "GENERAL_QUESTION"