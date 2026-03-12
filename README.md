# Smart Backlog Assistant — Web UI

A web interface for the Smart Backlog Agent. Upload meeting notes (TXT/PDF) or paste them directly in chat, upload your current backlog JSON, run the 5-step AI pipeline, and review/approve each proposed change interactively.

## Architecture

```
backlog-ui/
├── backend/
│   ├── app/
│   │   ├── main.py          ← FastAPI app (all routes)
│   │   ├── agent.py         ← Backlog pipeline adapter (steps 1-3 + review logic)
│   │   ├── auth.py          ← JWT auth (register/login/refresh/logout)
│   │   ├── database.py      ← SQLAlchemy models + SQLite setup
│   │   ├── logger.py        ← JSON structured logger
│   │   ├── prompts.yaml     ← All LLM prompts
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
└── .env.example
```

## Setup

### 1. Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — fill in GOOGLE_API_KEY, PINECONE_API_KEY, SECRET_KEY
```

### 3. (One-time) Ingest your backlog into Pinecone

Run your existing `ingest_backlog.py` script:

```bash
python ingest_backlog.py --backlog path/to/backlog.json
```

### 4. Start the server

```bash
cd backend/app
python main.py
# or: uvicorn main:app --reload --port 8000
```

### 5. Open the UI

Navigate to `http://localhost:8000` in your browser.

## Usage Flow

1. **Register / Sign in** via the auth modal.
2. **New Session** — click "+ New Session" in the sidebar.
3. **Upload files** — drag or click to upload:
   - Meeting notes: `.txt` or `.pdf`
   - Or paste meeting notes directly in the text area.
4. **Run Backlog Agent** — click the button. Watch the 3-step pipeline stream in real time.
5. **Review** — for each proposed change (UPDATE / CREATE / NO_CHANGE), click:
   - ✅ **Approve** — apply as-is
   - ❌ **Reject** — skip this change
   - ✏️ **Edit** — enter JSON overrides (e.g. `{"updated_priority": "High"}`)
6. **Download** — once all changes are reviewed, download `backlog_updated.json`.

## Notes

- All conversation history is stored in `backlog_assistant.db` (SQLite).
- If Pinecone is unavailable, all topics fall through to "CREATE new story".
- The `.env` `DEV_MODE=true` enables CORS for all origins (dev only).
- Logs are written to `agent_logs/agent.log` (JSON, rotating).
