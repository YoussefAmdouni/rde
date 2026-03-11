"""
Memory Manager
==============
Token-budget-aware conversation history compressor.

Strategy:
  1. Estimate token count of the full history (4 chars ≈ 1 token).
  2. If under budget  → return history as-is.
  3. If over budget   → keep the last RECENT_TURNS turns verbatim,
                        summarize everything older with a light LLM (gemma-3-27b-it),
                        and prepend the summary as a single synthetic "assistant" turn.

This keeps the context window lean without losing important earlier context.
"""

import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from dotenv import load_dotenv
load_dotenv(".env")

from logger import get_logger
logger = get_logger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TOKEN_BUDGET    = int(os.getenv("HISTORY_TOKEN_BUDGET", "8000"))  
RECENT_TURNS    = int(os.getenv("HISTORY_RECENT_TURNS", "8"))     
CHARS_PER_TOKEN = 4                                               

# Light summariser — gemma-3-27b-it via Google AI
_llm_summariser = ChatGoogleGenerativeAI(
    model="gemma-3-27b-it",
    temperature=0.0,
).with_retry(stop_after_attempt=2)


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: total chars / 4."""
    return sum(len(m.get("content", "")) for m in messages) // CHARS_PER_TOKEN


async def _summarise(messages: list[dict]) -> str:
    """
    Ask the light LLM to summarise a list of conversation turns into
    a compact paragraph the main LLM can use as prior context.
    """
    if not messages:
        return ""

    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
    )
    prompt = (
        "You are a conversation summariser. "
        "Summarise the following conversation history into a concise paragraph "
        "that captures the key topics discussed, decisions made, and any important "
        "facts the assistant should remember. Be brief but complete.\n\n"
        f"Conversation to summarise:\n{convo_text}\n\n"
        "Summary:"
    )
    try:
        response = await _llm_summariser.ainvoke([HumanMessage(content=prompt)])
        summary = response.content.strip() if isinstance(response.content, str) else str(response.content).strip()
        logger.info(
            f"[MemoryManager] Summarised {len(messages)} messages → "
            f"{len(summary)} chars ({_estimate_tokens([{'content': summary}])} tokens)"
        )
        return summary
    except Exception as e:
        # Fail gracefully — return a simple concatenation if summariser is unavailable
        logger.warning(f"[MemoryManager] Summariser failed, falling back to truncation: {e}")
        return " | ".join(
            f"{m['role']}: {m['content'][:120]}"
            for m in messages[-4:]
        )


async def compress_history(
    messages: list[dict],
    token_budget: int = TOKEN_BUDGET,
    recent_turns: int = RECENT_TURNS,
) -> list[dict]:
    """
    Return a token-budget-aware version of the conversation history.

    Args:
        messages:     Full list of {"role": ..., "content": ...} dicts.
        token_budget: Max tokens allowed for the history block.
        recent_turns: Number of recent turns (user+assistant pairs) to always keep verbatim.

    Returns:
        Compressed list — either unchanged (if under budget) or
        [{"role": "assistant", "content": "<summary>"}] + last N turns.
    """
    if not messages:
        return []

    estimated = _estimate_tokens(messages)

    if estimated <= token_budget:
        logger.debug(f"[MemoryManager] History within budget ({estimated}/{token_budget} tokens) — no compression")
        return messages

    logger.info(
        f"[MemoryManager] History over budget ({estimated}/{token_budget} tokens) — "
        f"compressing, keeping last {recent_turns} turn pairs verbatim"
    )

    # Split: keep last `recent_turns` user+assistant pairs verbatim
    # Each "turn pair" = 2 messages (user + assistant)
    keep_count = recent_turns * 2
    if len(messages) <= keep_count:
        # Not enough messages to split — just return as-is
        return messages

    older   = messages[:-keep_count]
    recent  = messages[-keep_count:]

    summary = await _summarise(older)

    compressed = []
    if summary:
        compressed.append({
            "role":    "assistant",
            "content": f"[Earlier conversation summary]: {summary}",
        })
    compressed.extend(recent)

    new_estimate = _estimate_tokens(compressed)
    logger.info(
        f"[MemoryManager] Compressed {len(messages)} → {len(compressed)} messages "
        f"({estimated} → {new_estimate} tokens)"
    )
    return compressed