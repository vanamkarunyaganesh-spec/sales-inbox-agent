# Sales Inbox Agent

A local portfolio application based on the architecture of the existing [Sales Inbox Agent repository](https://github.com/vanamkarunyaganesh-spec/sales-inbox-agent). This is a standalone implementation, not a byte-for-byte copy or a drop-in replacement for the original challenge API.

## What it does

- Ingests batches of email JSON and converts actionable emails into tasks.
- Routes work to sales, marketing, alliances, finance or operations using optional Gemini JSON classification or offline rules.
- Applies deadline and government tender rules, skips common non-actionable messages, deduplicates email IDs and updates existing threads.
- Stores tasks and routing logs in SQLite. Supports completing and reopening tasks.
- Answers supported natural-language queries using parameterized SQL and grounded counts. This is a limited intent router, not an unrestricted LLM-to-SQL system.
- Includes a responsive dashboard, REST API documentation, tests and synthetic classification evaluation.

## Quick start on Windows (PowerShell)

1. Extract the ZIP. Open the `Sales_Inbox_Agent` folder in VS Code.
2. Open Terminal > New Terminal. Run these commands (Python 3.11 or 3.12 recommended):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

3. Open http://127.0.0.1:8000 in your browser.
4. Click **Load sample emails**, then **Route emails**. The demo runs without an API key.
5. Try asking `How many open high priority tasks?` or `Show finance tasks`. Complete a task, then refresh the page: SQLite retains it.
6. Stop the server with Ctrl+C. Delete `inbox.db` only if you intentionally want to reset all local data.

Interactive API docs: http://127.0.0.1:8000/docs

## Optional Gemini integration

Get your own Gemini API key and copy `.env.example` to `.env` in the project root. Set `GEMINI_API_KEY` and a model available to your account in `GEMINI_MODEL`. Restart the server. Check the dashboard mode and routing logs in SQLite (the `emails` table records `gemini`, `rules` or `rules_fallback`). Never upload your `.env` to GitHub. The API sends supplied email contents to Gemini when enabled; use synthetic emails for testing and do not submit confidential email without permission. API requests may incur charges depending on your account.

## Run tests and evaluation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evaluate.py
```

The evaluation is a tiny hand-labeled synthetic fixture, not a real inbox benchmark or an LLM evaluation. See `EVAL_RESULTS.txt` for actual offline results. Six local API tests passed during packaging, including a mocked Gemini success and failure. Live Gemini calls and Docker execution were not tested. There is no Gmail integration, authentication, multi-tenant access control or production deployment; keep the server local.

## Architecture

```
Email JSON -> FastAPI /ingest -> Gemini or rules -> Business rules
                                  |                  |
                                  +------------------+-> SQLite
Dashboard <- REST API <- task queries and SQL filters <--+
```

## Optional Docker

```bash
docker build -t sales-inbox-agent .
docker run --rm -p 127.0.0.1:8000:8000 -v sales-inbox-data:/data -e DB_PATH=/data/inbox.db sales-inbox-agent
```

The Dockerfile is provided but not executed during packaging.
