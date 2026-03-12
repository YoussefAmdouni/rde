# Smart Backlog Assistant — Web UI

A web interface for the Smart Backlog Agent. Upload meeting notes (TXT/PDF) or paste them directly in chat, run the AI pipeline, and review/approve each proposed change interactively. All changes are synced back to Pinecone so future sessions benefit from an up-to-date vector index.

## Architecture

```
backlog-ui/
├── backend/
│   ├── main.py              ← FastAPI app (all routes)
│   ├── agent.py             ← Backlog pipeline (steps 1–3 + review + Pinecone sync)
│   ├── auth.py              ← JWT auth (register/login/refresh/logout)
│   ├── database.py          ← SQLAlchemy models + SQLite / PostgreSQL setup
│   ├── guard.py             ← Two-layer input safety classifier (regex + LLM)
│   ├── router_agent.py      ← Intent classifier (meeting notes vs. general question)
│   ├── web_search_agent.py  ← Web-search answer agent (Tavily + LLM)
│   ├── memory_manager.py    ← Token-budget-aware conversation history compressor
│   ├── llm_config.py        ← Central LLM configuration (all nodes, env overrides)
│   ├── logger.py            ← JSON structured logger (rotating file + console)         
│   ├── prompts.ymal         ← All LLM prompts
|   └── .env.example         ← Template for .env
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── sample_data/
│   ├── backlog.json             ← Sample backlog to get started
|   ├── .env.example             ← Template for .env
│   └── ingest_backlog.py        ← One-time script: embeds & pushes backlog to Pinecone
├── requirements.txt
├── readme.md
└── .gitignore
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   GOOGLE_API_KEY       — Gemini API key
#   PINECONE_API_KEY     — Pinecone API key
#   PINECONE_INDEX_NAME  — Name of your Pinecone index (must exist at 768 dimensions)
#   SECRET_KEY           — Random secret for JWT signing
#   TAVILY_API_KEY       — (optional) enables web-search for general questions
#   LANGSMITH_API_KEY    — (optional) enables LangSmith tracing
```

### 3. (One-time) Create your Pinecone index

Create a Pinecone index with **768 dimensions** before ingesting. The free tier supports one index.

### 4. (One-time) Ingest your backlog into Pinecone

Use the provided sample data or your own backlog JSON:

```bash
# Override the index name (defaults to PINECONE_INDEX_NAME in .env)
python sample_data/ingest_backlog.py --backlog sample_data/backlog.json --index my-index
```

This script embeds every story using `gemini-embedding-001` (768 dimensions, `RETRIEVAL_DOCUMENT` task type) and upserts them into Pinecone. Story IDs are used as vector IDs, so re-running is idempotent.

### 5. Start the server

```bash
cd backend
python main.py
# or:
uvicorn main:app --reload --port 8000
```

### 6. Open the UI

Open `index.html` 

## Usage Flow

1. **Register / Sign in** via the auth modal.
2. **New Session** — click "+ New Session" in the sidebar.
3. **Submit input** — either:
   - Attach meeting notes (`.txt` or `.pdf`), or
   - Paste text directly in the composer.
   - Non-meeting input (questions, general queries) is automatically routed to the web-search agent.
4. **Run Backlog Agent** — meeting notes trigger the 3-step AI pipeline, streamed in real time:
   - **Step 1** — Extract topics from the meeting notes.
   - **Step 2** — Search Pinecone for matching existing stories.
   - **Step 3** — Draft UPDATE or CREATE proposals for each topic.
5. **Review** — for each proposed change (UPDATE / CREATE / NO_CHANGE), click:
   - ✅ **Approve** — apply as-is
   - ❌ **Reject** — skip this change
   - ✏️ **Edit** — modify fields (title, priority, category, story, acceptance criteria) before applying
6. **Pinecone sync** — approved changes are automatically upserted back to Pinecone so the index stays current for future sessions.
7. **Download** — once all changes are reviewed, click **Download Backlog** to export `backlog_updated.json`.

## Sample Data

The `sample_data/` folder contains:

| File | Purpose |
|------|---------|
| `backlog.json` | A sample product backlog you can use to test the full pipeline |
| `ingest_backlog.py` | One-time ingestion script — run once at project start, or whenever you want to seed/reset the index |
| `sample_data` | `txt` and `pdf` meeting notes for testing|

The backlog JSON format expected by the ingestion script and the agent:

```json
[
  {
    "id": "US-001",
    "title": "As a user I want ...",
    "story": "As a user I want ... so that ...",
    "priority": "High",
    "category": "Feature",
    "acceptanceCriteria": [
      "Given ... When ... Then ..."
    ]
  }
]
```

## LLM Configuration

All models are defined in `backend/app/llm_config.py`. You can override any model or temperature via environment variables without touching code:

```env
LLM_EXTRACT_MODEL=gemini-2.0-flash
LLM_UPDATE_TEMP=0.1
```

| Node | Default model | Purpose |
|------|--------------|---------|
| `EXTRACT` | `gemini-3-flash-preview` | Step 1 — topic extraction |
| `MATCH` | `gemini-3-flash-preview` | Step 2 — vector match confirmation |
| `UPDATE` | `gemini-3.1-flash-lite-preview` | Step 3a — story update drafting |
| `CREATE` | `gemini-3.1-flash-lite-preview` | Step 3b — new story drafting |
| `GUARD` | `gemini-3.1-flash-lite-preview` | Input safety classifier |
| `ROUTER` | `gemini-3-flash-preview` | Intent router |
| `WEB_SEARCH` | `gemini-2.5-flash` | Web-search answer synthesis |
| `SUMMARISER` | `gemma-3-27b-it` | Conversation history compressor |

## Input Safety

Every user submission passes through a two-layer guard (`guard.py`) before any processing:

- **Layer 1** — Regex/heuristic patterns (zero LLM cost): catches prompt injection, jailbreak attempts, harmful requests, and spam.
- **Layer 2** — LLM classifier with structured Pydantic output: handles ambiguous cases. Fails open (allows) on error to avoid blocking legitimate users.

## LangSmith Tracing (optional)

Set `LANGSMITH_API_KEY` in `.env` to enable distributed tracing. Each pipeline run gets a unique root `run_id`; all LLM calls appear as children in one unified trace.

## Database

The app uses **PostgreSQL via Neon**. Set the `DATABASE_URL` in your `.env` to your Neon connection string:
 
```env
DATABASE_URL=postgresql://user:password@host/dbname
```
 
SQLAlchemy uses `asyncpg` as the async driver. The URL is automatically normalised from `postgres://` or `postgresql://` to `postgresql+asyncpg://` at startup. Tables are created automatically on first run via `CREATE TABLE IF NOT EXISTS`.
 
## Notes
 
- Conversation history is stored in your Neon PostgreSQL database.
- If Pinecone is unavailable during a run, all topics fall through to "CREATE new story".
- `DEV_MODE=true` (default) enables CORS for all origins. Set to `false` in production and configure `ALLOWED_ORIGINS`.
- Logs are written to `agent_logs/agent.log`.
- The memory manager (`memory_manager.py`) compresses conversation history when it exceeds the token budget, keeping the last N turns verbatim and summarising older context.