"""
LLM Configuration
=================
Single source of truth for every LLM used in the pipeline.
To swap a model, change it here — no need to touch individual modules.

Environment variable overrides follow the pattern:
  LLM_<NODE>_MODEL    — model name
  LLM_<NODE>_TEMP     — temperature (float)

Nodes:
  EXTRACT       — Step 1: extract topics from meeting notes        (agent.py)
  MATCH         — Step 2: confirm vector search match              (agent.py)
  UPDATE        — Step 3a: draft story update                      (agent.py)
  CREATE        — Step 3b: draft new story                         (agent.py)
  GUARD         — Input safety classifier                          (guard.py)
  ROUTER        — Intent classifier (meeting notes vs question)    (router_agent.py)
  WEB_SEARCH    — Web search answer synthesis                      (web_search_agent.py)
  SUMMARISER    — Conversation history compressor                  (memory_manager.py)
"""

import os
from dataclasses import dataclass
from langchain_google_genai import ChatGoogleGenerativeAI


# ─── Node config dataclass ────────────────────────────────────────────────────

@dataclass
class LLMNodeConfig:
    model:       str
    temperature: float
    description: str


# ─── Default configs per node ─────────────────────────────────────────────────

_DEFAULTS: dict[str, LLMNodeConfig] = {

    # Pipeline nodes — accuracy matters, use capable model
    "EXTRACT": LLMNodeConfig(
        model       = "gemini-3-flash-preview",
        temperature = 0.0,
        description = "Extracts topics from meeting notes (Step 1)",
    ),
    "MATCH": LLMNodeConfig(
        model       = "gemini-3-flash-preview",
        temperature = 0.0,
        description = "Confirms vector search match to existing story (Step 2)",
    ),
    "UPDATE": LLMNodeConfig(
        model       = "gemini-3.1-flash-lite-preview",
        temperature = 0.0,
        description = "Drafts an update to an existing story (Step 3a)",
    ),
    "CREATE": LLMNodeConfig(
        model       = "gemini-3.1-flash-lite-preview",
        temperature = 0.0,
        description = "Drafts a brand-new user story (Step 3b)",
    ),

    # Lightweight classification nodes — speed over raw capability
    "GUARD": LLMNodeConfig(
        model       = "gemini-3.1-flash-lite-preview",
        temperature = 0.0,
        description = "Input safety classifier (guard.py)",
    ),
    "ROUTER": LLMNodeConfig(
        model       = "gemini-3-flash-preview",
        temperature = 0.0,
        description = "Intent router: meeting notes vs general question (router_agent.py)",
    ),

    # Web search synthesis — needs reasoning over search results
    "WEB_SEARCH": LLMNodeConfig(
        model       = "gemini-2.5-flash",
        temperature = 0.2,
        description = "Synthesises web search results into an answer (web_search_agent.py)",
    ),

    # Summariser — lightweight, just compresses conversation history
    "SUMMARISER": LLMNodeConfig(
        model       = "gemma-3-27b-it",
        temperature = 0.0,
        description = "Compresses old conversation history into a summary (memory_manager.py)",
    ),
}


# ─── Config resolver — env overrides take precedence ─────────────────────────

def _resolve(node: str) -> LLMNodeConfig:
    """
    Return the config for a node, with env var overrides applied.
    LLM_<NODE>_MODEL and LLM_<NODE>_TEMP override the defaults.
    """
    base = _DEFAULTS[node]
    return LLMNodeConfig(
        model       = os.getenv(f"LLM_{node}_MODEL", base.model),
        temperature = float(os.getenv(f"LLM_{node}_TEMP", str(base.temperature))),
        description = base.description,
    )


# ─── Public factory — use this in every module ────────────────────────────────

def get_llm(node: str, **kwargs) -> ChatGoogleGenerativeAI:
    """
    Return a ChatGoogleGenerativeAI instance for the given node.

    Usage:
        from llm_config import get_llm
        llm = get_llm("EXTRACT")

    Extra kwargs are passed through to ChatGoogleGenerativeAI
    (e.g. streaming=True, callbacks=[...]).
    """
    if node not in _DEFAULTS:
        raise ValueError(
            f"Unknown LLM node '{node}'. "
            f"Valid nodes: {sorted(_DEFAULTS.keys())}"
        )
    cfg = _resolve(node)
    return ChatGoogleGenerativeAI(
        model       = cfg.model,
        temperature = cfg.temperature,
        **kwargs,
    )


def get_config(node: str) -> LLMNodeConfig:
    """Return the resolved config for a node (useful for logging)."""
    if node not in _DEFAULTS:
        raise ValueError(f"Unknown LLM node '{node}'.")
    return _resolve(node)


def log_all_configs(logger) -> None:
    """Log the active config for every node at startup — useful for debugging."""
    logger.info("─── Active LLM Configurations ───────────────────────────────")
    for node in sorted(_DEFAULTS.keys()):
        cfg = _resolve(node)
        logger.info(f"  {node:<12} model={cfg.model:<35} temp={cfg.temperature}  # {cfg.description}")
    logger.info("─────────────────────────────────────────────────────────────")