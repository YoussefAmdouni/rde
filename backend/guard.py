"""
Input Guard
===========
Two-layer safety check that runs before the router on every user submission.

Layer 1 — regex/heuristic  (instant, zero LLM cost)
Layer 2 — LLM classifier   (structured Pydantic output, only if Layer 1 passes)

Both layers return a GuardResult with:
  safe        : bool   — True = allow, False = block
  description : str    — human-readable explanation (always populated)
  layer       : int    — 1 = regex caught it, 2 = LLM caught/cleared it
"""

import re
import os
import yaml

from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from llm_config import get_llm

from logger import get_logger
logger = get_logger(__name__)

# ─── Load prompt ──────────────────────────────────────────────────────────────
_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

PROMPT_GUARD = _PROMPTS["input_guard"]


# ─── Pydantic output schema ───────────────────────────────────────────────────
class GuardResult(BaseModel):
    safe: bool = Field(
        description="True if the input is safe to process, False if it should be blocked."
    )
    description: str = Field(
        description=(
            "Short explanation of the decision. "
            "If safe=True: briefly confirm the input is fine. "
            "If safe=False: explain clearly what was detected (e.g. 'Prompt injection attempt: "
            "the input tries to override system instructions') so the user understands why "
            "their input was rejected."
        )
    )
    layer: int = Field(default=0, description="Internal: 1 = regex, 2 = LLM. Set by caller.")


# ─── LLM (structured output) ─────────────────────────────────────────────────
_llm_guard = get_llm("GUARD").with_structured_output(GuardResult).with_retry(stop_after_attempt=2)


# ─── Layer 1: Regex / heuristic patterns ─────────────────────────────────────

_UNSAFE_PATTERNS: list[tuple[str, str]] = [
    # (regex_pattern, description_if_matched)

    # Prompt injection
    (r"ignore\s+(all\s+)?(previous|prior|above|your)\s+instructions",
     "Prompt injection attempt: tries to override system instructions."),
    (r"disregard\s+(all\s+)?(previous|prior|above|your)\s+instructions",
     "Prompt injection attempt: tries to disregard system instructions."),
    (r"forget\s+(all\s+)?(previous|prior|above|your)\s+instructions",
     "Prompt injection attempt: tries to erase system instructions."),
    (r"act\s+as\s+(an?\s+)?(?:unrestricted|unfiltered|jailbroken|evil|opposite)",
     "Prompt injection attempt: requests an unrestricted AI persona."),
    (r"\bnew\s+persona\b",
     "Prompt injection attempt: requests a persona override."),
    (r"\bjailbreak\b",
     "Prompt injection attempt: contains a jailbreak instruction."),
    (r"\bDAN\b",
     "Prompt injection attempt: references the 'Do Anything Now' jailbreak."),
    (r"system\s+prompt\s*[:=]",
     "Prompt injection attempt: tries to inject a system prompt."),
    (r"<\s*system\s*>",
     "Prompt injection attempt: XML-style system tag injection detected."),
    (r"\[INST\]|\[\/INST\]",
     "Prompt injection attempt: LLM instruction tags detected in user input."),
    (r"###\s*instruction",
     "Prompt injection attempt: instruction delimiter detected."),
    (r"override\s+(the\s+)?(?:system|ai|model|assistant)",
     "Prompt injection attempt: tries to override the AI system."),
    (r"bypass\s+(the\s+)?(?:filter|safety|guard|restriction)",
     "Unsafe input: attempts to bypass safety filters."),
 
    # Harmful content
    (r"\b(how\s+to\s+)?(make|build|create|synthesize)\s+(a\s+)?(bomb|weapon|explosive|poison|malware|virus|ransomware)",
     "Unsafe input: request for harmful or dangerous content."),
    (r"\b(child|kid|minor)\s+(sexual|porn|nude|explicit)",
     "Unsafe input: contains prohibited content involving minors."),
    (r"\bcsam\b",
     "Unsafe input: contains prohibited content involving minors."),
    (r"\bself[\s-]harm\b",
     "Unsafe input: references self-harm."),
    (r"\bkill\s+(myself|yourself|a person|people)",
     "Unsafe input: contains threatening or self-harm language."),

    # Spam / abuse
    (r"(?![-=*#_~/])(.)\1{80,}",
     "Unsafe input: spam — excessive repeated characters detected."),
    (r"[^\x00-\x7F]{200,}",
     "Unsafe input: possible encoding attack — excessive non-ASCII content."),
]

_COMPILED = [
    (re.compile(pattern, re.IGNORECASE | re.DOTALL), description)
    for pattern, description in _UNSAFE_PATTERNS
]


def _layer1_check(text: str) -> GuardResult | None:
    """
    Returns GuardResult(safe=False) if any heuristic fires, else None.
    None means Layer 1 found nothing — pass to Layer 2.
    """
    for compiled_pat, description in _COMPILED:
        if compiled_pat.search(text):
            logger.warning(f"[Guard L1] Blocked — {description[:80]}")
            return GuardResult(safe=False, description=description, layer=1)
    return None


async def _layer2_check(text: str) -> GuardResult:
    """
    LLM-based check with structured Pydantic output.
    Fails open (safe=True) on LLM error to never block legitimate users.
    """
    snippet = text[:800]
    prompt  = PROMPT_GUARD.format(user_input=snippet)

    try:
        result: GuardResult = await _llm_guard.ainvoke([HumanMessage(content=prompt)])
        result.layer = 2
        if not result.safe:
            logger.warning(f"[Guard L2] Blocked — {result.description[:100]}")
        return result

    except Exception as e:
        logger.error(f"[Guard L2] LLM classifier error — failing open: {e}")
        return GuardResult(
            safe=True,
            description="Safety classifier temporarily unavailable — input passed by default.",
            layer=2,
        )


async def check_input(text: str) -> GuardResult:
    """
    Full two-layer guard. Callers block when result.safe is False.
    """
    layer1 = _layer1_check(text)
    if layer1 is not None:
        return layer1
    return await _layer2_check(text)