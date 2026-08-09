# Sales Inbox → Task Router

**candidate_id:** `YOUR_EMAIL@example.com`  ← replace with your real, lowercased email everywhere (this file, `.env`, every API call). See "Before you submit" below.

**Deployed backend URL:** `https://REPLACE-ME.onrender.com`
**Deployed frontend URL:** `https://REPLACE-ME.vercel.app`

*(Fill these in only after you've deployed — see "Deploy" below. They must be byte-identical here, in the submission form, and in every request your frontend makes.)*

## What this is

A backend (FastAPI) that implements the challenge's Task API spec (`/tasks`, `/users`), plus
`/ingest`, `/api/tasks`, `/api/stats`, `/api/chat` on top of it — all under one deployable
service, backed by SQLite so state survives restarts. A frontend (single static `index.html`,
no build step) that lets you paste a batch of emails, see them as a raw table, then ask
natural-language questions about them.

Routing uses Gemini when `GEMINI_API_KEY` is set (recommended — this is what handles messy/
Hinglish/ambiguous text well), with a deterministic rule-based fallback so the system is still
testable with zero API cost. Two hard business rules (PSU tenders → Aarti; deadline <72h →
priority high) are enforced in a post-processing pass regardless of which classifier ran, so
they can never be silently missed.

## Setup (local)

```bash
cd backend
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # then paste your real Gemini key into .env
uvicorn main:app --reload --port 8000
```

Open `frontend/index.html` directly in a browser (or `python3 -m http.server` inside
`frontend/`), point the "Backend URL" field at `http://localhost:8000`, and paste emails or
click "Generate 250 sample emails".

## Deploy

**Backend (Render, free tier):**
1. Push this repo to GitHub.
2. On [render.com](https://render.com) → New → Web Service → connect the repo, root directory `backend`.
3. Build command: `pip install -r requirements.txt`. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Add env var `GEMINI_API_KEY` (get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey)).
5. **Important (spec §5, persistence):** Render's free tier disk is ephemeral across deploys but
   persists across idle-spin-downs within the same instance — that's sufficient for the grader's
   Run 1→2→3 sequence (minutes apart), but if you redeploy the code between runs, SQLite resets.
   For real durability, add a Render persistent disk mounted at `/opt/render/project/src/backend`,
   or switch `DB_PATH` to a free Supabase Postgres and swap the `sqlite3` calls in `main.py` for
   `psycopg2` (the SQL is close to portable already — see DECISIONS.md).
6. Note the deployed URL (e.g. `https://sales-inbox-agent.onrender.com`).

**Frontend (Netlify or Vercel, free tier):**
1. Deploy the `frontend/` folder as a static site (drag-and-drop on Netlify works, or connect the repo with root directory `frontend`).
2. No build step needed — it's a single `index.html`.
3. Open the deployed site, paste your backend URL and candidate_id into the two fields at the top.

**CORS:** `main.py` currently allows `allow_origins=["*"]` so the frontend works immediately from
any origin. Before final submission, consider narrowing this to your actual frontend URL in
`main.py`'s `CORSMiddleware` config — not required for grading, but better practice.

## Before you submit — checklist

- [ ] Replace every `YOUR_EMAIL@example.com` placeholder (this file, `.env` on your host) with
      your real, lowercased, no-`+alias` email.
- [ ] Confirm `GET https://your-backend-url/tasks?candidate_id=your@email.com` returns your test
      tasks from a browser or curl — this is exactly what the grader will call.
- [ ] Run the three-run sequence from §8.1 of the brief yourself once against your deployed URL
      (ingest → ingest again → ingest replies) and confirm counts match the idempotency/
      reconciliation expectations.
- [ ] Fill in EVALS.md with real hand-labels from the ACTUAL `inbox.json` you were given — the
      version in this repo was run against synthetic sample data (see EVALS.md header) because
      the real dataset wasn't available while this scaffold was built. This step is not optional;
      fabricated/synthetic metrics presented as real ones score below an honest low number per §7.5.
- [ ] Fill in the deployed URLs at the top of this file and in the submission form — byte-identical.
- [ ] Remove `backend/tasks.db` and `backend/smoke_test.py` from the repo before pushing (dev artifacts).
- [ ] Double check no API key is committed (`.env` should be in `.gitignore`, already set up).

## Project layout

```
backend/
  main.py                 # FastAPI app: Task API + ingest + api/tasks + api/stats + api/chat
  rules.py                 # deterministic parsing, prefilter, hard business rules, fallback classifier
  gemini_client.py         # Gemini REST calls: classification + chat answer phrasing
  query_engine.py          # translates chat questions into SQL queries over our own store
  generate_sample_emails.py
  team_roster.json
  requirements.txt
  .env.example
frontend/
  index.html               # paste → table → chat, single file, no build step
DECISIONS.md
EVALS.md
```

## A known limitation, stated plainly

The chat query engine (`query_engine.py`) is a pattern-matched intent router covering the
10 sample question types in the spec, not a general NL→SQL layer — see DECISIONS.md for why,
and what a follow-up with more time would look like.
