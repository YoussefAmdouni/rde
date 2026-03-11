"""
Smart Backlog Assistant — FastAPI Backend
=========================================
Routes:
  POST /api/conversations          — create session
  GET  /api/conversations          — list sessions
  GET  /api/conversations/:id/messages
  DELETE /api/conversations/:id
  POST /api/sessions/:id/upload    — upload meeting notes (txt/pdf) + backlog JSON
  POST /api/sessions/:id/process/stream — run pipeline, stream progress
  POST /api/sessions/:id/review    — submit A/R/E decision for one proposed change
  GET  /api/sessions/:id/backlog   — download current/final backlog JSON
  GET  /api/sessions/:id/proposed  — get proposed changes list (for edit modal)
  Auth: /api/auth/...
"""

import os
import io
import json
import uuid
import asyncio
import re
import pypdf
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, AsyncIterator

from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from jose import jwt

from memory_manager import compress_history
from agent import (
    AgentState,
    load_vector_store,
    fetch_all_stories_from_pinecone,
    step_parse_extract,
    step_search_match,
    step_compare_decide,
    apply_review_decisions,
    upsert_stories_to_pinecone,
    format_proposed_change_for_chat,
    fetch_trace_telemetry,
    make_run_id,
    make_parent_run,
    _LANGSMITH_ENABLED,
)
from guard import check_input
from router_agent import classify_input
from web_search_agent import stream_web_search_answer
from auth import auth_router, require_active_user, SECRET_KEY
from database import (
    create_tables, check_db_connection, get_db, AsyncSessionLocal,
    DATABASE_URL, _IS_POSTGRES,
    User, Conversation, Message, BacklogSession,
)
from logger import get_logger

logger = get_logger(__name__)

# ─── Rate limiter ──────────────────────────────────────────────────────────────

def get_user_or_ip(request: Request) -> str:
    token = request.headers.get("Authorization", "")
    if token:
        try:
            payload = jwt.decode(token.replace("Bearer ", ""), SECRET_KEY, algorithms=["HS256"])
            return f"user:{payload.get('sub', get_remote_address(request))}"
        except Exception:
            pass
    return get_remote_address(request)

limiter = Limiter(key_func=get_user_or_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log which database we're using
    db_type = "PostgreSQL (Neon)" if _IS_POSTGRES else "SQLite (local)"
    logger.info(f"Database backend: {db_type}")

    # Create tables
    await create_tables()
    logger.info("Database tables ready")

    # Verify connectivity
    ok = await check_db_connection()
    if ok:
        logger.info(f"Database connection verified ✓")
    else:
        logger.error(f"Database connection FAILED — check DATABASE_URL")

    yield


app = FastAPI(title="Smart Backlog Assistant", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

DEV_MODE = os.getenv("DEV_MODE", "true").lower() == "true"
if DEV_MODE:
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
else:
    ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")]
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"], max_age=600,
    )

app.include_router(auth_router)

# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_conv_or_404(conv_id: str, user_id: str, db: AsyncSession) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id,
            Conversation.user_id == user_id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Session not found")
    return conv


async def _get_or_create_backlog_session(conv_id: str, db: AsyncSession) -> BacklogSession:
    result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = result.scalar_one_or_none()
    if not bs:
        bs = BacklogSession(conversation_id=conv_id)
        db.add(bs)
        await db.commit()
        await db.refresh(bs)
    return bs


async def _save_message(conv_id: str, role: str, content: str,
                         msg_type: str = "text", db: AsyncSession = None):
    async with AsyncSessionLocal() as s:
        s.add(Message(conversation_id=conv_id, role=role, content=content, msg_type=msg_type))
        conv = await s.get(Conversation, conv_id)
        if conv:
            if role == "user" and conv.title == "New Session":
                conv.title = content[:50] + ("…" if len(content) > 50 else "")
            conv.updated_at = datetime.now(timezone.utc)
        await s.commit()


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

    
def _extract_text_from_pdf(content: bytes) -> str:
    """Extract plain text from PDF bytes using pypdf."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as page_err:
                logger.warning(f"Skipping page due to extraction error: {page_err}")
                text = ""
            # Strip lone surrogates that cause utf-16-be encode errors in pypdf
            text = text.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
            # Normalize to clean ASCII-safe unicode
            text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
            parts.append(text)

        extracted = "\n".join(parts).strip()
        if not extracted:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not extract text from this PDF. "
                    "It may be scanned/image-based. "
                    "Please copy-paste the text directly into the text box instead."
                ),
            )
        return extracted

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"PDF extraction failed: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Failed to read PDF: {e}. Try copy-pasting the text instead.",
        )


# ─── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    return {
        "status":    "ok" if db_ok else "degraded",
        "database":  "postgres" if _IS_POSTGRES else "sqlite",
        "db_online": db_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── Conversations (sessions) ──────────────────────────────────────────────────
@app.get("/api/conversations")
@limiter.limit("60/minute")
async def list_conversations(
    request: Request,
    page: int = 1, page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    if page < 1:       page = 1
    if page_size > 50: page_size = 50
    offset = (page - 1) * page_size

    msg_count_subq = (
        select(Message.conversation_id, func.count(Message.id).label("cnt"))
        .group_by(Message.conversation_id).subquery()
    )
    result = await db.execute(
        select(Conversation, func.coalesce(msg_count_subq.c.cnt, 0).label("message_count"))
        .outerjoin(msg_count_subq, Conversation.id == msg_count_subq.c.conversation_id)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
        .offset(offset).limit(page_size)
    )
    rows = result.all()
    return {
        "conversations": [
            {"id": c.id, "title": c.title, "created_at": c.created_at,
             "updated_at": c.updated_at, "message_count": count}
            for c, count in rows
        ],
        "page": page, "page_size": page_size,
    }


class ConversationCreate(BaseModel):
    title: Optional[str] = "New Session"


@app.post("/api/conversations", status_code=201)
@limiter.limit("20/minute")
async def create_conversation(
    request: Request,
    body: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    conv = Conversation(
        id=str(uuid.uuid4()), user_id=current_user.id,
        title=body.title or "New Session",
        updated_at=datetime.now(timezone.utc),
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return {"id": conv.id, "title": conv.title, "created_at": conv.created_at,
            "updated_at": conv.updated_at, "message_count": 0}


@app.delete("/api/conversations/{conv_id}")
@limiter.limit("20/minute")
async def delete_conversation(
    request: Request, conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    conv = await _get_conv_or_404(conv_id, current_user.id, db)
    await db.delete(conv)
    await db.commit()
    return {"message": "Session deleted"}


@app.get("/api/conversations/{conv_id}/messages")
@limiter.limit("60/minute")
async def get_messages(
    request: Request, conv_id: str, limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .limit(min(limit, 200))
    )
    msgs = result.scalars().all()
    return {"messages": [
        {"id": m.id, "role": m.role, "content": m.content,
         "msg_type": m.msg_type, "created_at": m.created_at}
        for m in msgs
    ]}


# ─── Upload / classify input ──────────────────────────────────────────────────
@app.post("/api/sessions/{conv_id}/upload")
@limiter.limit("10/minute")
async def upload_meeting_notes(
    request: Request,
    conv_id: str,
    meeting_notes_file: Optional[UploadFile] = File(None),
    meeting_notes_text: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    bs = await _get_or_create_backlog_session(conv_id, db)

    # ── Parse raw text ──
    raw_text     = ""
    source_label = ""
    if meeting_notes_file and meeting_notes_file.filename:
        content  = await meeting_notes_file.read()
        fname    = meeting_notes_file.filename.lower()
        raw_text = (_extract_text_from_pdf(content) if fname.endswith(".pdf")
                    else content.decode("utf-8", errors="replace"))
        source_label = meeting_notes_file.filename
    elif meeting_notes_text:
        raw_text     = meeting_notes_text.strip()
        source_label = "pasted text"

    if not raw_text:
        raise HTTPException(status_code=400, detail="No input provided")

    # ── Guard ──
    guard_result = await check_input(raw_text)
    if not guard_result.safe:
        logger.warning(
            f"[{current_user.email}][{conv_id}] Guard blocked "
            f"(layer {guard_result.layer}): {guard_result.description[:100]}"
        )
        raise HTTPException(status_code=400, detail=f"⛔ {guard_result.description}")

    # ── Router ──
    route = await classify_input(raw_text)
    logger.info(f"[{current_user.email}][{conv_id}] Route: {route}")

    if route == "MEETING_NOTES":
        bs.pipeline_stage   = "uploaded"
        bs.proposed_changes = None
        bs.review_index     = "0"
        bs.final_backlog    = None
        bs.changelog        = None
        bs.updated_at       = datetime.now(timezone.utc)
        await db.commit()

        await _save_message(
            conv_id, "user",
            f"📄 **Meeting notes loaded** from *{source_label}* · {len(raw_text):,} chars",
            msg_type="file",
        )
        await _save_message(conv_id, "system", raw_text, msg_type="meeting_notes")

        logger.info(
            f"[{current_user.email}][{conv_id}] Meeting notes saved "
            f"({len(raw_text)} chars) from {source_label}"
        )
        return {"status": "ok", "route": "MEETING_NOTES", "chars": len(raw_text)}

    else:
        bs.pipeline_stage = "general_query"
        bs.updated_at     = datetime.now(timezone.utc)
        await db.commit()

        await _save_message(conv_id, "user", raw_text, msg_type="text")
        await _save_message(conv_id, "system", raw_text, msg_type="general_query")

        logger.info(f"[{current_user.email}][{conv_id}] General question routed to web-search agent")
        return {"status": "ok", "route": route}


# ─── Stream general question ──────────────────────────────────────────────────
@app.post("/api/sessions/{conv_id}/general/stream")
@limiter.limit("10/minute")
async def general_stream(
    request: Request,
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)

    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or bs.pipeline_stage != "general_query":
        raise HTTPException(status_code=400, detail="No general question pending for this session")

    msg_result = await db.execute(
        select(Message).where(
            Message.conversation_id == conv_id,
            Message.msg_type == "general_query",
        ).order_by(Message.created_at.desc()).limit(1)
    )
    query_msg = msg_result.scalar_one_or_none()
    if not query_msg:
        raise HTTPException(status_code=400, detail="Question not found in session")

    question = query_msg.content

    history_result = await db.execute(
        select(Message).where(
            Message.conversation_id == conv_id,
            Message.role.in_(["user", "assistant"]),
            Message.msg_type.notin_(["meeting_notes", "general_query", "pinecone_snapshot"]),
        ).order_by(Message.created_at.asc()).limit(60)   # fetch more, compressor will trim
    )
    raw_history = [
        {"role": m.role, "content": m.content}
        for m in history_result.scalars().all()
        if m.content != question
    ]
    history = await compress_history(raw_history) 

    async def event_gen() -> AsyncIterator[str]:
        answer_parts: list[str] = []
        search_count = 0

        async for chunk in stream_web_search_answer(question, conversation_history=history):
            yield chunk
            try:
                data = json.loads(chunk.removeprefix("data: ").strip())
                if data.get("type") == "answer":
                    answer_parts.append(data.get("message", ""))
                elif data.get("type") == "step_done":
                    search_count += 1
            except Exception:
                pass

        final_answer = "\n".join(answer_parts) if answer_parts else ""
        if final_answer:
            await _save_message(conv_id, "assistant", final_answer, msg_type="text")

        async with AsyncSessionLocal() as save_db:
            bs2 = await save_db.get(BacklogSession, bs.id)
            if bs2:
                bs2.pipeline_stage = "idle"
                bs2.updated_at     = datetime.now(timezone.utc)
                await save_db.commit()

        logger.info(
            f"[{current_user.email}][{conv_id}] Web-search answer delivered "
            f"({search_count} search(es))"
        )

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Stream pipeline processing ───────────────────────────────────────────────
@app.post("/api/sessions/{conv_id}/process/stream")
@limiter.limit("5/minute")
async def process_stream(
    request: Request,
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)

    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or bs.pipeline_stage not in ("uploaded",):
        raise HTTPException(status_code=400, detail="No meeting notes uploaded yet, or pipeline already ran")

    msg_result = await db.execute(
        select(Message).where(
            Message.conversation_id == conv_id,
            Message.msg_type == "meeting_notes",
        ).order_by(Message.created_at.desc()).limit(1)
    )
    notes_msg = msg_result.scalar_one_or_none()
    if not notes_msg:
        raise HTTPException(status_code=400, detail="No meeting notes found in session")

    meeting_notes = notes_msg.content

    async def event_gen() -> AsyncIterator[str]:
        run_id = make_run_id()
        state  = AgentState(meeting_notes=meeting_notes, run_id=run_id)

        async with AsyncSessionLocal() as s0:
            bs0 = await s0.get(BacklogSession, bs.id)
            if bs0:
                bs0.langsmith_run_id = run_id
                bs0.pipeline_stage   = "processing"
                bs0.updated_at       = datetime.now(timezone.utc)
                await s0.commit()

        if _LANGSMITH_ENABLED:
            yield _sse({"type": "info",
                        "message": f"🔭 LangSmith tracing active — run `{run_id[:8]}…`"})

        try:
            with make_parent_run(run_id) as parent_run_id:
                # ── Step 1 — extract topics ──
                yield _sse({"type": "step", "step": 1, "message": "🔍 Extracting topics from meeting notes…"})
                state = await step_parse_extract(state, parent_run_id)
                topics_summary = ", ".join(f"`{t.topic_id}`" for t in state.extracted_topics)
                yield _sse({"type": "step_done", "step": 1,
                            "message": f"✅ Extracted **{len(state.extracted_topics)} topic(s)**: {topics_summary}"})

                # ── Step 2 — vector search against Pinecone ──
                yield _sse({"type": "step", "step": 2, "message": "🔎 Searching Pinecone backlog for matching stories…"})
                pinecone_stories = []
                try:
                    vector_store     = load_vector_store()
                    state            = await step_search_match(state, vector_store, parent_run_id)
                    pinecone_stories = await asyncio.to_thread(fetch_all_stories_from_pinecone)
                    yield _sse({"type": "step_done", "step": 2,
                                "message": f"✅ Backlog search complete — **{len(pinecone_stories)} stories** found in Pinecone."})
                except Exception as e:
                    yield _sse({"type": "warning",
                                "message": f"⚠️ Pinecone unavailable ({e}). All topics will generate new stories."})
                    class _EmptyDecision:
                        matched_story_id = None
                        confidence       = 0.0
                        reasoning        = "Pinecone unavailable."
                        needs_update     = False
                    state._match_results = [(t, _EmptyDecision(), []) for t in state.extracted_topics]

                # ── Step 3 — compare & decide ──
                yield _sse({"type": "step", "step": 3, "message": "🤔 Drafting proposed changes…"})
                state = await step_compare_decide(state, pinecone_stories, parent_run_id)
                yield _sse({"type": "step_done", "step": 3,
                            "message": f"✅ **{len(state.proposed_changes)} proposed change(s)** ready for your review."})

            # ── Persist to DB ──
            proposed_json = [c.model_dump() for c in state.proposed_changes]

            async with AsyncSessionLocal() as save_db:
                bs2 = await save_db.get(BacklogSession, bs.id)
                if bs2:
                    bs2.proposed_changes = proposed_json
                    bs2.pipeline_stage   = "review"
                    bs2.review_index     = "0"
                    bs2.updated_at       = datetime.now(timezone.utc)
                    await save_db.commit()

            await _save_message(conv_id, "system",
                json.dumps(pinecone_stories), msg_type="pinecone_snapshot")

            # ── Emit first review card ──
            total = len(proposed_json)
            if total == 0:
                final_msg = "✅ Pipeline complete — no backlog changes needed. Everything is already up to date."
                yield _sse({"type": "done", "message": final_msg})
                await _save_message(conv_id, "assistant", final_msg, msg_type="result")
                async with AsyncSessionLocal() as s2:
                    bs2 = await s2.get(BacklogSession, bs.id)
                    if bs2:
                        bs2.pipeline_stage = "done"
                        await s2.commit()
                return

            first_card = format_proposed_change_for_chat(0, proposed_json[0]).replace("{total}", str(total))
            yield _sse({"type": "review_card", "index": 0, "total": total,
                        "content": first_card, "change_data": proposed_json[0]})
            await _save_message(conv_id, "assistant", first_card, msg_type="review")

        except asyncio.CancelledError:
            logger.info(f"[{conv_id}] Client disconnected during processing")
        except Exception as e:
            logger.error(f"[{conv_id}] Pipeline error: {e}", exc_info=True)
            yield _sse({"type": "error", "message": f"Pipeline error: {str(e)}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Review decision endpoint ─────────────────────────────────────────────────

class ReviewDecisionBody(BaseModel):
    decision:    str
    edited_data: Optional[dict] = None


@app.post("/api/sessions/{conv_id}/review")
@limiter.limit("30/minute")
async def submit_review(
    request: Request,
    conv_id: str,
    body: ReviewDecisionBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)

    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or bs.pipeline_stage not in ("review",):
        raise HTTPException(status_code=400, detail="Not in review stage")

    decision = body.decision.upper()
    if decision not in ("APPROVE", "REJECT", "EDIT"):
        raise HTTPException(status_code=400, detail="Decision must be APPROVE, REJECT, or EDIT")

    current_idx = int(bs.review_index or "0")
    proposed    = bs.proposed_changes or []
    total       = len(proposed)

    if current_idx >= total:
        raise HTTPException(status_code=400, detail="All changes already reviewed")

    decisions_msg_result = await db.execute(
        select(Message).where(
            Message.conversation_id == conv_id,
            Message.msg_type == "review_decisions",
        ).order_by(Message.created_at.desc()).limit(1)
    )
    decisions_msg = decisions_msg_result.scalar_one_or_none()
    decisions = json.loads(decisions_msg.content) if decisions_msg else []

    decisions.append({
        "change_index": current_idx,
        "decision":     decision,
        "edited_data":  body.edited_data,
    })

    change   = proposed[current_idx]
    label    = {"APPROVE": "✅ Approved", "REJECT": "❌ Rejected", "EDIT": "✏️ Edited"}[decision]
    user_msg = f"{label}: **{change['topic_title']}** ({change['change_type']})"
    await _save_message(conv_id, "user", user_msg, msg_type="review_decision")

    next_idx = current_idx + 1

    if next_idx < total:
        async with AsyncSessionLocal() as s2:
            bs2 = await s2.get(BacklogSession, bs.id)
            bs2.review_index = str(next_idx)
            bs2.updated_at   = datetime.now(timezone.utc)
            dm = decisions_msg
            if dm:
                dm2 = await s2.get(Message, dm.id)
                if dm2:
                    dm2.content = json.dumps(decisions)
            else:
                s2.add(Message(
                    conversation_id=conv_id, role="system",
                    content=json.dumps(decisions), msg_type="review_decisions",
                ))
            await s2.commit()

        card = format_proposed_change_for_chat(next_idx, proposed[next_idx]).replace(
            "{total}", str(total)
        )
        await _save_message(conv_id, "assistant", card, msg_type="review")
        return {
            "status":      "next",
            "next_index":  next_idx,
            "total":       total,
            "review_card": card,
            "change_data": proposed[next_idx],
        }

    else:
        snapshot_result = await db.execute(
            select(Message).where(
                Message.conversation_id == conv_id,
                Message.msg_type == "pinecone_snapshot",
            ).order_by(Message.created_at.desc()).limit(1)
        )
        snapshot_msg     = snapshot_result.scalar_one_or_none()
        pinecone_stories = json.loads(snapshot_msg.content) if snapshot_msg else []

        final_backlog, changelog = apply_review_decisions(pinecone_stories, proposed, decisions)
        approved = sum(1 for d in decisions if d["decision"] != "REJECT")
        rejected = sum(1 for d in decisions if d["decision"] == "REJECT")

        stories_to_upsert = []
        for dec in decisions:
            if dec["decision"] == "REJECT":
                continue
            change = proposed[dec["change_index"]]
            edits  = dec.get("edited_data") or {}

            if change["change_type"] == "UPDATE" and change.get("story_update"):
                u        = {**change["story_update"], **edits}
                story_id = u["story_id"]
                merged   = next((s for s in final_backlog if s["id"] == story_id), None)
                if merged:
                    stories_to_upsert.append(merged)

            elif change["change_type"] == "CREATE" and change.get("new_story"):
                n        = {**change["new_story"], **edits}
                story_id = n["suggested_id"]
                merged   = next((s for s in final_backlog if s["id"] == story_id), None)
                if merged:
                    stories_to_upsert.append(merged)

        pinecone_synced = 0
        pinecone_errors: list[str] = []
        if stories_to_upsert:
            pinecone_synced, pinecone_errors = await asyncio.to_thread(
                upsert_stories_to_pinecone, stories_to_upsert
            )
            logger.info(
                f"[{conv_id}] Pinecone sync: {pinecone_synced} upserted, "
                f"{len(pinecone_errors)} errors"
            )

        async with AsyncSessionLocal() as s2:
            bs2 = await s2.get(BacklogSession, bs.id)
            bs2.final_backlog  = final_backlog
            bs2.changelog      = changelog
            bs2.pipeline_stage = "done"
            bs2.review_index   = str(next_idx)
            bs2.updated_at     = datetime.now(timezone.utc)
            dm = decisions_msg
            if dm:
                dm2 = await s2.get(Message, dm.id)
                if dm2:
                    dm2.content = json.dumps(decisions)
            else:
                s2.add(Message(
                    conversation_id=conv_id, role="system",
                    content=json.dumps(decisions), msg_type="review_decisions",
                ))
            await s2.commit()

        telemetry = None
        run_id    = bs.langsmith_run_id
        if run_id and _LANGSMITH_ENABLED:
            await asyncio.sleep(2)
            telemetry = await fetch_trace_telemetry(run_id)
            if telemetry:
                logger.info(
                    f"[{conv_id}] Telemetry: "
                    f"{telemetry['total_tokens']} tokens, "
                    f"{telemetry['latency_ms']}ms, "
                    f"{len(telemetry['llm_calls'])} LLM calls"
                )

        cl_lines = []
        for entry in changelog:
            action = entry["action"]
            if action == "UPDATED":
                cl_lines.append(f"- ✏️ **Updated** `{entry['story_id']}`: {entry['detail']}")
            elif action == "CREATED":
                cl_lines.append(f"- ➕ **Created** `{entry['story_id']}`: {entry['detail']}")
            elif action == "REJECTED":
                cl_lines.append(f"- ❌ **Rejected**: {entry['topic']}")
            elif action == "NO_CHANGE":
                cl_lines.append(f"- ✓ **No change**: {entry['topic']}")

        pinecone_line = ""
        if stories_to_upsert:
            if pinecone_errors:
                pinecone_line = (
                    f"\n\n⚠️ **Pinecone sync partially failed** — "
                    f"{pinecone_synced}/{len(stories_to_upsert)} stories synced. "
                    f"Errors: {'; '.join(pinecone_errors[:2])}"
                )
            else:
                pinecone_line = (
                    f"\n\n🔄 **Pinecone updated** — "
                    f"{pinecone_synced} stor{'y' if pinecone_synced == 1 else 'ies'} "
                    f"synced to index `{os.getenv('PINECONE_INDEX_NAME', 'backlog-index')}`. "
                    f"Future sessions will find these stories in vector search."
                )

        summary = (
            f"## ✅ Backlog Update Complete\n\n"
            f"**{approved}** change(s) applied · **{rejected}** rejected · "
            f"**{len(final_backlog)}** total stories\n\n"
            f"### Changelog\n" + "\n".join(cl_lines) +
            pinecone_line +
            f"\n\n---\nClick **Download Backlog** to export `backlog_updated.json`."
        )
        await _save_message(conv_id, "assistant", summary, msg_type="result")

        return {
            "status":        "done",
            "approved":      approved,
            "rejected":      rejected,
            "total_stories": len(final_backlog),
            "summary":       summary,
            "telemetry":     telemetry,
        }


# ─── Get proposed changes ──────────────────────────────────────────────────────
@app.get("/api/sessions/{conv_id}/proposed")
@limiter.limit("30/minute")
async def get_proposed_changes(
    request: Request,
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or not bs.proposed_changes:
        raise HTTPException(status_code=404, detail="No proposed changes found")
    return {"changes": bs.proposed_changes, "review_index": int(bs.review_index or "0")}


# ─── Download final backlog ────────────────────────────────────────────────────
@app.get("/api/sessions/{conv_id}/backlog")
@limiter.limit("20/minute")
async def download_backlog(
    request: Request, conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or not bs.final_backlog:
        raise HTTPException(status_code=404, detail="No final backlog available yet — complete review first")

    return JSONResponse(
        content=bs.final_backlog,
        headers={"Content-Disposition": "attachment; filename=backlog_updated.json"},
    )


# ─── Download changelog ────────────────────────────────────────────────────────
@app.get("/api/sessions/{conv_id}/changelog")
@limiter.limit("20/minute")
async def download_changelog(
    request: Request, conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or not bs.changelog:
        raise HTTPException(status_code=404, detail="No changelog available yet")

    return JSONResponse(
        content=bs.changelog,
        headers={"Content-Disposition": "attachment; filename=changelog.json"},
    )


# ─── LangSmith telemetry ──────────────────────────────────────────────────────
@app.get("/api/sessions/{conv_id}/telemetry")
@limiter.limit("20/minute")
async def get_telemetry(
    request: Request, conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)

    if not _LANGSMITH_ENABLED:
        raise HTTPException(status_code=404, detail="LangSmith tracing is not enabled")

    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs or not bs.langsmith_run_id:
        raise HTTPException(status_code=404, detail="No LangSmith run recorded for this session")

    telemetry = await fetch_trace_telemetry(bs.langsmith_run_id)
    if not telemetry:
        raise HTTPException(status_code=503, detail="LangSmith data not yet available — try again in a few seconds")

    return telemetry


# ─── Session status ───────────────────────────────────────────────────────────
@app.get("/api/sessions/{conv_id}/status")
async def session_status(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_user),
):
    await _get_conv_or_404(conv_id, current_user.id, db)
    bs_result = await db.execute(
        select(BacklogSession).where(BacklogSession.conversation_id == conv_id)
    )
    bs = bs_result.scalar_one_or_none()
    if not bs:
        return {"stage": "idle", "review_index": 0, "total_changes": 0}
    return {
        "stage":         bs.pipeline_stage,
        "review_index":  int(bs.review_index or "0"),
        "total_changes": len(bs.proposed_changes or []),
    }


# ─── Serve static frontend ─────────────────────────────────────────────────────
frontend_path = Path(__file__).parent.parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
    logger.info(f"Serving frontend from {frontend_path}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)