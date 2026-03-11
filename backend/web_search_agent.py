"""
Web Search Agent
================
Answers GENERAL_QUESTION intents using a plain LLM + tool loop 
same pattern as the existing agent.py in the project.

The LLM is bound to the Tavily search tool and loops until it stops
making tool calls (= final answer reached) or hits max iterations.
"""

import os
import json
import asyncio
import yaml
from datetime import datetime
from typing import AsyncIterator

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import HumanMessage
from memory_manager import compress_history

from logger import get_logger
logger = get_logger(__name__)

# ─── Load prompt ──────────────────────────────────────────────────────────────
_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

PROMPT_WEB_SEARCH_AGENT = _PROMPTS["web_search_agent"]

# ─── Config ───────────────────────────────────────────────────────────────────
_TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY", "")
_MAX_SEARCH_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS_PER_SEARCH", "4"))


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _extract_text(content) -> str:
    """Normalise LLM response content to a plain string."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content
        ).strip()
    return str(content).strip()


async def stream_web_search_answer(
    question: str,
    conversation_history: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Run the web-search tool loop and stream SSE events.
    No AgentExecutor — just LLM.bind_tools() + a while loop,
    identical to the run_tool_loop pattern in agent.py.
    """
    if not _TAVILY_API_KEY:
        yield _sse({"type": "error",
                    "message": "⚠️ TAVILY_API_KEY is not set. Add it to your .env to enable web search."})
        return

    current_date = datetime.now().strftime("%A, %B %d, %Y")

    # Build memory string from conversation history
    history = conversation_history or []
    if history:
        compressed = await compress_history(history)
        memory = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in compressed)
    else:
        memory = "No prior conversation."

    tavily_tool = TavilySearchResults(
        max_results=_MAX_SEARCH_RESULTS,
        tavily_api_key=_TAVILY_API_KEY,
    )

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0.2,
    ).bind_tools([tavily_tool]).with_retry(stop_after_attempt=3)

    system_prompt = PROMPT_WEB_SEARCH_AGENT.format(
        current_date=current_date,
        user_question=question,
        memory=memory,
    )
    messages = [HumanMessage(content=system_prompt)]

    search_count = 0
    yield _sse({"type": "step", "step": 1, "message": "🌐 Searching the web…"})

    try:
        while True:
            logger.info(f"[WebSearchAgent] Iteration {search_count + 1}")
            response = await llm.ainvoke(messages)

            # No tool calls → final answer
            if not getattr(response, "tool_calls", None):
                answer = _extract_text(response.content)
                if not answer:
                    answer = "I searched the web but couldn't find a confident answer. Please try rephrasing."
                logger.info(f"[WebSearchAgent] Done — {search_count} search(es) for: {question[:80]}")
                yield _sse({"type": "answer", "message": answer})
                return

            # Execute each tool call and stream progress
            messages.append(response)
            for tool_call in response.tool_calls:
                search_count += 1
                query        = tool_call["args"].get("query", str(tool_call["args"]))
                tool_call_id = tool_call.get("id")

                yield _sse({
                    "type":    "step_done",
                    "step":    search_count,
                    "message": f"🔍 Search {search_count}: *{query}*",
                })

                result = await asyncio.to_thread(tavily_tool.invoke, tool_call["args"])

                tool_msg = {
                    "role":         "tool",
                    "name":         tool_call["name"],
                    "content":      str(result),
                    "tool_call_id": tool_call_id,
                }
                messages.append(tool_msg)


    except Exception as e:
        logger.error(f"[WebSearchAgent] Error: {e}")
        yield _sse({"type": "error", "message": f"❌ Web search failed: {e}"})