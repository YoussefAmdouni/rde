"""
Backlog Agent Web Adapter
=========================
Wraps the original backlog_agent pipeline steps for async web usage.
Replaces the CLI human-review step with a queue-based interaction model,
so the FastAPI SSE endpoint can pause and resume.

LangSmith tracing is enabled automatically when LANGSMITH_API_KEY is set.
Each pipeline run gets a unique run_id that can be used to fetch telemetry.
"""

import warnings
warnings.filterwarnings("ignore")

import os
import json
import uuid
import asyncio
from typing import Optional, AsyncIterator
from datetime import datetime

import yaml
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_core.messages import HumanMessage
from langchain_pinecone import PineconeVectorStore

from dotenv import load_dotenv
load_dotenv(".env")

from logger import get_logger
logger = get_logger(__name__)

# ─── LangSmith — enable tracing if API key is present ────────────────────────
_LANGSMITH_ENABLED = bool(os.getenv("LANGSMITH_API_KEY"))

if _LANGSMITH_ENABLED:
    # These env vars are read by LangChain automatically at import time.
    # Setting them here ensures they're in place before any LLM client is created.
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT",
                          os.getenv("LANGSMITH_PROJECT", "backlog-assistant"))
    logger.info(
        f"LangSmith tracing enabled — project: "
        f"{os.environ['LANGCHAIN_PROJECT']}"
    )
else:
    # Explicitly disable so LangChain doesn't try to connect
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logger.info("LangSmith tracing disabled (LANGSMITH_API_KEY not set)")


# ─── Load prompts ─────────────────────────────────────────────────────────────
_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
    _prompts = yaml.safe_load(f)

PROMPT_EXTRACT       = _prompts["extract_topics"]
PROMPT_CONFIRM_MATCH = _prompts["confirm_match"]
PROMPT_UPDATE_STORY  = _prompts["update_story"]
PROMPT_CREATE_STORY  = _prompts["create_story"]

# ─── Constants ────────────────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))
TOP_K                = int(os.getenv("TOP_K", "5"))
MAX_MEMORY_TURNS     = int(os.getenv("MAX_MEMORY_TURNS", "10"))
PINECONE_INDEX_NAME  = os.getenv("PINECONE_INDEX_NAME", "backlog-index")

# ─── Pydantic models (re-exported) ────────────────────────────────────────────

class ExtractedTopic(BaseModel):
    topic_id:           str
    title:              str
    description:        str
    is_new_requirement: bool
    priority_hint:      Optional[str] = None
    category_hint:      Optional[str] = None

class ExtractionResult(BaseModel):
    topics: list[ExtractedTopic]

class MatchDecision(BaseModel):
    matched_story_id: Optional[str] = None
    confidence:       float
    reasoning:        str
    needs_update:     bool

class StoryUpdate(BaseModel):
    story_id:                    str
    updated_title:               str
    updated_story:               str
    updated_priority:            str
    updated_category:            str
    updated_acceptance_criteria: list[str]
    changelog_entry:             str

class NewStory(BaseModel):
    suggested_id:        str
    title:               str
    story:               str
    priority:            str
    category:            str
    acceptance_criteria: list[str]
    source_topic_id:     str

class ProposedChange(BaseModel):
    change_type:  str
    topic_id:     str
    topic_title:  str
    story_update: Optional[StoryUpdate] = None
    new_story:    Optional[NewStory]    = None
    reason:       str

# ─── LLM clients ──────────────────────────────────────────────────────────────
_base_llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite-preview", temperature=0.0)

llm_extract = _base_llm.with_structured_output(ExtractionResult).with_retry(stop_after_attempt=3)
llm_match   = _base_llm.with_structured_output(MatchDecision).with_retry(stop_after_attempt=3)
llm_update  = _base_llm.with_structured_output(StoryUpdate).with_retry(stop_after_attempt=3)
llm_new     = _base_llm.with_structured_output(NewStory).with_retry(stop_after_attempt=3)

# ─── LangSmith run context ────────────────────────────────────────────────────

def make_run_id() -> str:
    """Generate a UUID string to use as the LangSmith root run ID."""
    return str(uuid.uuid4())


def get_langsmith_run_url(run_id: str) -> str | None:
    """Return the direct LangSmith UI URL for a run, or None if not enabled."""
    if not _LANGSMITH_ENABLED:
        return None
    return f"https://smith.langchain.com/public/{run_id}/r"


def make_parent_run(run_id: str, pipeline_name: str = "backlog-pipeline"):
    """
    Returns a context manager that opens a LangSmith root RunTree so that
    every LLM call made inside the `with` block is recorded as a child of
    this parent run — giving you one unified trace per pipeline execution.

    When LangSmith is disabled it returns a no-op context manager.

    Usage (in the SSE event generator):
        with make_parent_run(state.run_id) as parent_run_id:
            state = await step_parse_extract(state, parent_run_id)
            ...
    """
    from contextlib import contextmanager

    if not _LANGSMITH_ENABLED:
        @contextmanager
        def _noop():
            yield None
        return _noop()

    from langsmith.run_trees import RunTree

    rt = RunTree(
        name         = pipeline_name,
        run_type     = "chain",
        id           = uuid.UUID(run_id),
        inputs       = {"pipeline": pipeline_name},
        tags         = ["backlog-assistant"],
        project_name = os.environ.get("LANGCHAIN_PROJECT", "backlog-assistant"),
    )

    @contextmanager
    def _ctx():
        rt.post()
        try:
            yield str(rt.id)
        except Exception as exc:
            rt.end(error=str(exc))
            rt.patch()
            raise
        else:
            rt.end(outputs={"status": "completed"})
            rt.patch()

    return _ctx()


def _lc_config(run_id: str, step_name: str, parent_run_id: str | None = None) -> dict:
    """
    Build a LangChain RunnableConfig for one LLM call.
    - Names and tags the call so it appears clearly in LangSmith.
    - When parent_run_id is provided the call is nested under the root trace.
    """
    cfg: dict = {
        "run_name": step_name,
        "tags":     ["backlog-assistant", step_name],
        "metadata": {"pipeline_run_id": run_id},
    }
    if _LANGSMITH_ENABLED and parent_run_id:
        cfg["run_id"]        = str(uuid.uuid4())   # unique ID for this call
        cfg["parent_run_id"] = parent_run_id
    return cfg


async def fetch_trace_telemetry(run_id: str) -> dict | None:
    """
    Query the LangSmith REST API for aggregated telemetry of a root run.

    Returns a dict with:
        total_tokens, prompt_tokens, completion_tokens,
        latency_ms, llm_calls (list of per-call breakdowns),
        langsmith_url
    or None if LangSmith is disabled or the call fails.

    LangSmith API reference:
        GET /api/v1/runs/{run_id}
        GET /api/v1/runs?parent_run=<run_id>&run_type=llm&limit=50
    """
    if not _LANGSMITH_ENABLED:
        return None

    import httpx

    api_key = os.environ["LANGSMITH_API_KEY"]
    base    = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. Fetch the root run for overall latency
            root_resp = await client.get(
                f"{base}/api/v1/runs/{run_id}", headers=headers
            )
            if root_resp.status_code == 404:
                # LangSmith may take a few seconds to finalize — not an error
                logger.info(f"LangSmith run {run_id} not found yet (may still be ingesting)")
                return None
            root_resp.raise_for_status()
            root = root_resp.json()

            # 2. Fetch all child LLM runs
            child_resp = await client.get(
                f"{base}/api/v1/runs",
                params={
                    "parent_run": run_id,
                    "run_type":   "llm",
                    "limit":      50,
                    "select":     "name,start_time,end_time,total_tokens,prompt_tokens,completion_tokens,inputs,outputs,status",
                },
                headers=headers,
            )
            child_resp.raise_for_status()
            children = child_resp.json()

            # children may be a list or {"runs": [...]}
            if isinstance(children, dict):
                children = children.get("runs", [])

            # Aggregate totals
            total_prompt     = 0
            total_completion = 0
            llm_calls        = []

            for run in children:
                usage = run.get("extra", {}).get("usage_metadata") or {}
                # LangChain stores token counts in different places depending on version
                pt = (
                    usage.get("input_tokens")
                    or usage.get("prompt_tokens")
                    or run.get("prompt_tokens", 0)
                    or 0
                )
                ct = (
                    usage.get("output_tokens")
                    or usage.get("completion_tokens")
                    or run.get("completion_tokens", 0)
                    or 0
                )
                total_prompt     += pt
                total_completion += ct

                # Latency
                start = run.get("start_time")
                end   = run.get("end_time")
                call_ms = None
                if start and end:
                    from datetime import timezone
                    import dateutil.parser
                    try:
                        s = dateutil.parser.parse(start)
                        e = dateutil.parser.parse(end)
                        call_ms = int((e - s).total_seconds() * 1000)
                    except Exception:
                        pass

                llm_calls.append({
                    "name":             run.get("name", "llm"),
                    "status":           run.get("status", "unknown"),
                    "prompt_tokens":    pt,
                    "completion_tokens": ct,
                    "total_tokens":     pt + ct,
                    "latency_ms":       call_ms,
                })

            # Overall latency from root run
            root_start = root.get("start_time")
            root_end   = root.get("end_time")
            total_ms   = None
            if root_start and root_end:
                try:
                    import dateutil.parser
                    s = dateutil.parser.parse(root_start)
                    e = dateutil.parser.parse(root_end)
                    total_ms = int((e - s).total_seconds() * 1000)
                except Exception:
                    pass

            return {
                "run_id":             run_id,
                "status":             root.get("status", "unknown"),
                "total_tokens":       total_prompt + total_completion,
                "prompt_tokens":      total_prompt,
                "completion_tokens":  total_completion,
                "latency_ms":         total_ms,
                "llm_calls":          llm_calls,
                "langsmith_url":      get_langsmith_run_url(run_id),
            }

    except Exception as e:
        logger.warning(f"LangSmith telemetry fetch failed for run {run_id}: {e}")
        return None


def load_vector_store() -> PineconeVectorStore:
    return PineconeVectorStore(
        index_name       = PINECONE_INDEX_NAME,
        embedding        = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=768
        ),
        pinecone_api_key = os.environ["PINECONE_API_KEY"],
    )


def fetch_all_stories_from_pinecone() -> list[dict]:
    """
    Fetches every story stored in the Pinecone index by using the
    Pinecone client directly (list + fetch).  Returns a list of story dicts
    identical to the original backlog JSON format.
    Falls back gracefully to an empty list if the index is empty or unreachable.
    """
    try:
        from pinecone import Pinecone
        pc    = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        index = pc.Index(PINECONE_INDEX_NAME)

        stories = []
        # list() returns an iterator of id batches
        for id_batch in index.list():
            if not id_batch:
                continue
            fetch_resp = index.fetch(ids=id_batch)
            for vec_id, vec in fetch_resp.vectors.items():
                meta = vec.metadata or {}
                story_json = meta.get("story_json")
                if story_json:
                    try:
                        stories.append(json.loads(story_json))
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse story_json for vector {vec_id}")

        logger.info(f"Fetched {len(stories)} stories from Pinecone index '{PINECONE_INDEX_NAME}'")
        return stories

    except Exception as e:
        logger.error(f"fetch_all_stories_from_pinecone failed: {e}", exc_info=True)
        return []


# ─── Agent state ──────────────────────────────────────────────────────────────

class AgentState:
    def __init__(self, meeting_notes: str, run_id: str | None = None):
        self.meeting_notes     = meeting_notes
        self.run_id            = run_id or make_run_id()
        self.extracted_topics: list[ExtractedTopic] = []
        self.proposed_changes: list[ProposedChange] = []
        self._match_results    = []
        self.session_log: list[str]   = []
        self.memory: list[dict]       = []

    def add_memory(self, role: str, content: str):
        self.memory.append({"role": role, "content": content})
        if len(self.memory) > MAX_MEMORY_TURNS * 2:
            self.memory = self.memory[-(MAX_MEMORY_TURNS * 2):]

    def format_memory(self) -> str:
        if not self.memory:
            return "No prior context."
        lines = ["Recent processing context:"]
        for m in self.memory:
            lines.append(f"  {m['role'].upper()}: {m['content']}")
        return "\n".join(lines)

    def log(self, entry: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.session_log.append(f"[{ts}] {entry}")
        logger.info(entry)

# ─── Pipeline steps ───────────────────────────────────────────────────────────

async def step_parse_extract(state: AgentState, parent_run_id: str | None = None) -> AgentState:
    state.log("STEP 1 — Parsing and extracting topics from meeting notes")
    prompt = PROMPT_EXTRACT.format(
        meeting_notes=state.meeting_notes,
        memory=state.format_memory(),
    )
    result: ExtractionResult = await llm_extract.ainvoke(
        [HumanMessage(content=prompt)],
        config=_lc_config(state.run_id, "step1-extract-topics", parent_run_id),
    )
    state.extracted_topics = result.topics
    for t in result.topics:
        state.add_memory("extracted_topic", f"{t.topic_id}: {t.description[:120]}")
    state.log(f"  -> Extracted {len(result.topics)} topics: {[t.topic_id for t in result.topics]}")
    return state


async def _search_and_match_topic(topic, vector_store, state, parent_run_id: str | None = None):
    """
    Only called for topics where is_new_requirement=False.
    Runs vector search then asks the LLM to select the correct story to update.
    """
    query_text = f"{topic.title}. {topic.description}"
    results    = vector_store.similarity_search_with_score(query_text, k=TOP_K)
    candidates_above = [(doc, score) for doc, score in results if score >= SIMILARITY_THRESHOLD]
    state.log(f"  Topic '{topic.topic_id}': {len(candidates_above)}/{TOP_K} candidates above threshold")

    if not candidates_above:
        # No strong match found — treat as a new story despite the flag
        state.log(f"  Topic '{topic.topic_id}': no candidates above threshold → will CREATE")
        decision = MatchDecision(
            matched_story_id=None, confidence=0.0,
            reasoning="No backlog story matched the similarity threshold — will create new story.",
            needs_update=False,
        )
        return topic, decision, []

    raw_candidates  = []
    candidate_lines = []
    for rank, (doc, score) in enumerate(candidates_above, 1):
        story = json.loads(doc.metadata["story_json"])
        raw_candidates.append(story)
        candidate_lines.append(
            f"  [{rank}] {story['id']} (score={score:.3f})\n"
            f"       Title: {story['title']}\n"
            f"       Story: {story['story'][:200]}"
        )

    prompt = PROMPT_CONFIRM_MATCH.format(
        topic_id=topic.topic_id,
        topic_title=topic.title,
        topic_description=topic.description,
        candidates="\n".join(candidate_lines),
        memory=state.format_memory(),
    )
    decision: MatchDecision = await llm_match.ainvoke(
        [HumanMessage(content=prompt)],
        config=_lc_config(state.run_id, f"step2-match-{topic.topic_id}", parent_run_id),
    )
    state.add_memory(
        "match_decision",
        f"{topic.topic_id} -> {decision.matched_story_id or 'NO MATCH'} "
        f"(conf={decision.confidence:.2f}): {decision.reasoning}"
    )
    return topic, decision, raw_candidates


async def step_search_match(state: AgentState, vector_store,
                            parent_run_id: str | None = None) -> AgentState:
    state.log("STEP 2 — Searching backlog for UPDATE topics (skipping new requirements)")

    update_topics = [t for t in state.extracted_topics if not t.is_new_requirement]
    new_topics    = [t for t in state.extracted_topics if t.is_new_requirement]

    state.log(f"  -> {len(update_topics)} UPDATE topic(s), {len(new_topics)} NEW topic(s)")

    # Short-circuit new requirements — no vector search needed
    no_search_results = []
    for topic in new_topics:
        state.log(f"  Skipping search for new requirement: '{topic.topic_id}'")
        decision = MatchDecision(
            matched_story_id=None, confidence=1.0,
            reasoning="Flagged as a new requirement — skipping backlog search, will create story directly.",
            needs_update=False,
        )
        no_search_results.append((topic, decision, []))

    # Run vector search only for UPDATE topics (in parallel)
    search_results = []
    if update_topics:
        tasks = [_search_and_match_topic(t, vector_store, state, parent_run_id)
                 for t in update_topics]
        search_results = list(await asyncio.gather(*tasks))

    # Preserve original topic order
    result_map = {r[0].topic_id: r for r in no_search_results + search_results}
    state._match_results = [result_map[t.topic_id] for t in state.extracted_topics]

    state.log(f"  -> Step 2 complete: {len(state._match_results)} topics processed")
    return state


def _get_story_by_id(story_id: str, backlog: list[dict]) -> Optional[dict]:
    for s in backlog:
        if s["id"] == story_id:
            return s
    return None

def _max_story_id(backlog: list[dict]) -> str:
    nums = []
    for s in backlog:
        try:
            nums.append(int(s["id"].replace("US-", "")))
        except ValueError:
            pass
    return f"US-{max(nums):03d}" if nums else "US-000"


async def step_compare_decide(state: AgentState, pinecone_stories: list[dict],
                              parent_run_id: str | None = None) -> AgentState:
    """Step 3: For each topic, draft an UPDATE or CREATE proposal."""
    state.log("STEP 3 — Comparing and deciding on each match result")
    state._pinecone_stories = pinecone_stories
    proposed = []
    max_id   = _max_story_id(pinecone_stories)

    for topic, decision, candidates in state._match_results:
        if decision.matched_story_id and decision.needs_update:
            original = _get_story_by_id(decision.matched_story_id, pinecone_stories)
            if original is None:
                state.log(f"  WARNING: {decision.matched_story_id} not found in Pinecone snapshot — skipping")
                continue
            prompt = PROMPT_UPDATE_STORY.format(
                original_story_json=json.dumps(original, indent=2),
                topic_title=topic.title,
                topic_description=topic.description,
                priority_hint=topic.priority_hint or "not specified",
                memory=state.format_memory(),
            )
            updated: StoryUpdate = await llm_update.ainvoke(
                [HumanMessage(content=prompt)],
                config=_lc_config(state.run_id, f"step3-update-{decision.matched_story_id}", parent_run_id),
            )
            proposed.append(ProposedChange(
                change_type="UPDATE", topic_id=topic.topic_id, topic_title=topic.title,
                story_update=updated, reason=decision.reasoning,
            ))
            state.add_memory("decision", f"Proposed UPDATE for {decision.matched_story_id}: {updated.changelog_entry}")

        elif decision.matched_story_id and not decision.needs_update:
            proposed.append(ProposedChange(
                change_type="NO_CHANGE", topic_id=topic.topic_id, topic_title=topic.title,
                reason=f"{decision.matched_story_id} reviewed — no changes needed. {decision.reasoning}",
            ))

        else:
            prompt = PROMPT_CREATE_STORY.format(
                topic_title=topic.title,
                topic_description=topic.description,
                priority_hint=topic.priority_hint or "Medium",
                category_hint=topic.category_hint or "Feature",
                max_existing_id=max_id,
                memory=state.format_memory(),
            )
            new_story: NewStory = await llm_new.ainvoke(
                [HumanMessage(content=prompt)],
                config=_lc_config(state.run_id, f"step3-create-{topic.topic_id}", parent_run_id),
            )
            proposed.append(ProposedChange(
                change_type="CREATE", topic_id=topic.topic_id, topic_title=topic.title,
                new_story=new_story,
                reason=f"No matching backlog item found. {decision.reasoning}",
            ))
            state.add_memory("decision", f"Proposed CREATE {new_story.suggested_id}: {new_story.title}")

    state.proposed_changes = proposed
    state.log(f"  -> {len(proposed)} proposed changes ready for review")
    return state


def apply_review_decisions(
    pinecone_stories: list[dict],
    proposed_changes: list[dict],
    decisions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Apply review decisions to the full backlog fetched from Pinecone.

    Returns:
        final_backlog — all stories with approved changes merged in
        changelog     — list of human-readable change summaries
    """
    backlog_map = {s["id"]: dict(s) for s in pinecone_stories}
    changelog   = []

    for dec in decisions:
        idx = dec["change_index"]
        if idx >= len(proposed_changes):
            continue
        change = proposed_changes[idx]

        if dec["decision"] == "REJECT":
            changelog.append({
                "action":    "REJECTED",
                "topic":     change["topic_title"],
                "change_type": change["change_type"],
                "detail":    "Manually rejected during review.",
            })
            continue

        edits = dec.get("edited_data") or {}

        if change["change_type"] == "UPDATE" and change.get("story_update"):
            u = {**change["story_update"], **edits}
            backlog_map[u["story_id"]] = {
                "id":                 u["story_id"],
                "title":              u["updated_title"],
                "story":              u["updated_story"],
                "priority":           u["updated_priority"],
                "category":           u["updated_category"],
                "acceptanceCriteria": u["updated_acceptance_criteria"],
                "_changelog":         u["changelog_entry"],
                "_updated_at":        datetime.now().isoformat(),
            }
            changelog.append({
                "action":    "UPDATED",
                "story_id":  u["story_id"],
                "topic":     change["topic_title"],
                "detail":    u["changelog_entry"],
            })

        elif change["change_type"] == "CREATE" and change.get("new_story"):
            n = {**change["new_story"], **edits}
            backlog_map[n["suggested_id"]] = {
                "id":                 n["suggested_id"],
                "title":              n["title"],
                "story":              n["story"],
                "priority":           n["priority"],
                "category":           n["category"],
                "acceptanceCriteria": n["acceptance_criteria"],
                "_source_topic":      n["source_topic_id"],
                "_created_at":        datetime.now().isoformat(),
            }
            changelog.append({
                "action":    "CREATED",
                "story_id":  n["suggested_id"],
                "topic":     change["topic_title"],
                "detail":    n["title"],
            })

        elif change["change_type"] == "NO_CHANGE":
            changelog.append({
                "action":    "NO_CHANGE",
                "topic":     change["topic_title"],
                "detail":    change["reason"],
            })

    return list(backlog_map.values()), changelog


def upsert_stories_to_pinecone(stories: list[dict]) -> tuple[int, list[str]]:
    """
    Embeds and upserts a list of story dicts into the Pinecone index.
    Uses RETRIEVAL_DOCUMENT task type (same as ingest_backlog.py).
    Story ID is used as the vector ID → idempotent: re-running overwrites old vector.

    Returns:
        (upserted_count, error_messages)
    """
    if not stories:
        return 0, []

    errors = []
    try:
        from langchain_core.documents import Document

        embeddings = GoogleGenerativeAIEmbeddings(
            model                 = "models/gemini-embedding-001",
            task_type             = "RETRIEVAL_DOCUMENT",
            output_dimensionality = 768,
        )
        vector_store = PineconeVectorStore(
            index_name       = PINECONE_INDEX_NAME,
            embedding        = embeddings,
            pinecone_api_key = os.environ["PINECONE_API_KEY"],
        )

        docs = []
        ids  = []
        for item in stories:
            try:
                ac_text    = " ".join(item.get("acceptanceCriteria", []))
                embed_text = f"{item['title']}. {item['story']} {ac_text}".strip()
                doc = Document(
                    page_content = embed_text,
                    metadata     = {
                        "story_id":   item["id"],
                        "title":      item["title"],
                        "priority":   item.get("priority", "Medium"),
                        "category":   item.get("category", "Feature"),
                        "story_json": json.dumps(item),
                    }
                )
                docs.append(doc)
                ids.append(item["id"])
            except Exception as e:
                err = f"Skipped story {item.get('id', '?')}: {e}"
                logger.warning(err)
                errors.append(err)

        if docs:
            vector_store.add_documents(docs, ids=ids)
            logger.info(f"Upserted {len(docs)} stories to Pinecone index '{PINECONE_INDEX_NAME}'")

        return len(docs), errors

    except Exception as e:
        msg = f"Pinecone upsert failed: {e}"
        logger.error(msg, exc_info=True)
        return 0, [msg]


def format_proposed_change_for_chat(idx: int, change: dict) -> str:
    """Render a proposed change as a readable chat message (Markdown)."""
    ct = change["change_type"]
    lines = [f"### Change {idx + 1} of {{total}} — **{ct}**"]
    lines.append(f"**Topic:** {change['topic_title']}")
    lines.append(f"**Reason:** {change['reason']}\n")

    if ct == "UPDATE" and change.get("story_update"):
        u = change["story_update"]
        lines.append(f"**Story ID:** `{u['story_id']}`")
        lines.append(f"**Title:** {u['updated_title']}")
        lines.append(f"**Priority:** {u['updated_priority']}  |  **Category:** {u['updated_category']}")
        lines.append(f"\n**Story:**\n> {u['updated_story']}\n")
        lines.append("**Acceptance Criteria:**")
        for ac in u["updated_acceptance_criteria"]:
            lines.append(f"- {ac}")
        lines.append(f"\n**Changelog:** _{u['changelog_entry']}_")

    elif ct == "CREATE" and change.get("new_story"):
        n = change["new_story"]
        lines.append(f"**Suggested ID:** `{n['suggested_id']}`")
        lines.append(f"**Title:** {n['title']}")
        lines.append(f"**Priority:** {n['priority']}  |  **Category:** {n['category']}")
        lines.append(f"\n**Story:**\n> {n['story']}\n")
        lines.append("**Acceptance Criteria:**")
        for ac in n["acceptance_criteria"]:
            lines.append(f"- {ac}")

    elif ct == "NO_CHANGE":
        lines.append("_No changes needed — story is already up to date._")

    lines.append("\n---")
    lines.append("**Your decision:**  ✅ Approve  ❌ Reject  ✏️ Edit")
    return "\n".join(lines)