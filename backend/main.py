import os
import json
import uuid
import sqlite3
import threading
import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import rules
import gemini_client
import query_engine

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tasks.db"))

ALLOWED_ASSIGNEES = ["u_aarti", "u_rohit", "u_meera", "u_karan", "u_divya", "u_triage"]
ALLOWED_CATEGORIES = ["enterprise_rfp", "smb_enquiry", "marketing", "alliances", "finance", "triage"]
ALLOWED_PRIORITIES = ["high", "medium", "low"]
SPAM_SKIP_CATEGORIES = {"skipped_ooo", "skipped_newsletter", "skipped_marketing_lookalike_spam", "skipped_other"}

with open(os.path.join(os.path.dirname(__file__), "team_roster.json")) as f:
    TEAM = json.load(f)["team"]

_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        source_email_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        assignee_id TEXT,
        category TEXT,
        priority TEXT,
        due_date TEXT,
        deal_value_inr INTEGER,
        company_name TEXT,
        confidence REAL,
        created_at TEXT,
        updated_at TEXT,
        update_count INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT,
        email_id TEXT,
        thread_id TEXT,
        decision TEXT,
        category TEXT,
        assignee_id TEXT,
        reason TEXT,
        confidence REAL,
        task_id TEXT,
        spurious_flag INTEGER DEFAULT 0,
        run_batch TEXT,
        from_email TEXT,
        subject TEXT,
        processed_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_candidate ON tasks(candidate_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(candidate_id, thread_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_log_candidate ON email_log(candidate_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_log_dedup ON email_log(candidate_id, email_id)")
    conn.commit()
    conn.close()


init_db()

app = FastAPI(title="Sales Inbox Task Router")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your frontend origin in production (see README)
    allow_methods=["*"],
    allow_headers=["*"],
)


def norm_email(e: str) -> str:
    if not e:
        return e
    e = e.strip().lower()
    if "@" in e:
        local, domain = e.split("@", 1)
        local = local.split("+")[0]
        e = f"{local}@{domain}"
    return e


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def validate_enum(field, value, allowed):
    if value not in allowed:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_enum_value", "field": field, "received": value, "allowed": allowed
        })


# ---------------------------------------------------------------------------
# §5 Task API
# ---------------------------------------------------------------------------
class TaskIn(BaseModel):
    candidate_id: str
    source_email_id: str
    thread_id: str
    title: str
    description: Optional[str] = None
    assignee_id: str
    category: str
    priority: str
    due_date: Optional[str] = None
    deal_value_inr: Optional[int] = None
    company_name: Optional[str] = None
    confidence: float


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    deal_value_inr: Optional[int] = None
    company_name: Optional[str] = None
    confidence: Optional[float] = None


@app.post("/tasks", status_code=201)
def create_task(t: TaskIn):
    validate_enum("assignee_id", t.assignee_id, ALLOWED_ASSIGNEES)
    validate_enum("category", t.category, ALLOWED_CATEGORIES)
    validate_enum("priority", t.priority, ALLOWED_PRIORITIES)
    cid = norm_email(t.candidate_id)
    task_id = "tsk_" + uuid.uuid4().hex[:8]
    ts = now_iso()
    conn = get_db()
    with _lock:
        conn.execute(
            """INSERT INTO tasks (task_id,candidate_id,source_email_id,thread_id,title,description,
               assignee_id,category,priority,due_date,deal_value_inr,company_name,confidence,
               created_at,updated_at,update_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (task_id, cid, t.source_email_id, t.thread_id, t.title, t.description, t.assignee_id,
             t.category, t.priority, t.due_date, t.deal_value_inr, t.company_name, t.confidence, ts, ts),
        )
        conn.commit()
    conn.close()
    return {"task_id": task_id, "candidate_id": cid, "source_email_id": t.source_email_id, "created_at": ts}


@app.patch("/tasks/{task_id}")
def patch_task(task_id: str, p: TaskPatch):
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail={"error": "not_found", "task_id": task_id})
    updates = {k: v for k, v in p.dict(exclude_unset=True).items()}
    if "assignee_id" in updates:
        validate_enum("assignee_id", updates["assignee_id"], ALLOWED_ASSIGNEES)
    if "category" in updates:
        validate_enum("category", updates["category"], ALLOWED_CATEGORIES)
    if "priority" in updates:
        validate_enum("priority", updates["priority"], ALLOWED_PRIORITIES)
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [now_iso(), task_id]
        with _lock:
            conn.execute(
                f"UPDATE tasks SET {set_clause}, updated_at=?, update_count=update_count+1 WHERE task_id=?", vals
            )
            conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row)


@app.get("/tasks")
def list_tasks(candidate_id: str, thread_id: Optional[str] = None,
                source_email_id: Optional[str] = None, assignee_id: Optional[str] = None):
    cid = norm_email(candidate_id)
    conn = get_db()
    q = "SELECT * FROM tasks WHERE candidate_id=?"
    params = [cid]
    if thread_id:
        q += " AND thread_id=?"; params.append(thread_id)
    if source_email_id:
        q += " AND source_email_id=?"; params.append(source_email_id)
    if assignee_id:
        q += " AND assignee_id=?"; params.append(assignee_id)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    conn = get_db()
    with _lock:
        conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        conn.commit()
    conn.close()
    return {"deleted": task_id}


@app.get("/users")
def get_users():
    return {"team": TEAM}


# ---------------------------------------------------------------------------
# §7.1 /ingest
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    candidate_id: str
    emails: List[dict]


def _log_email(conn, cid, email, decision, run_batch, task_id, action, spurious=False):
    ts = now_iso()
    with _lock:
        try:
            conn.execute(
                """INSERT INTO email_log (candidate_id,email_id,thread_id,decision,category,assignee_id,
                   reason,confidence,task_id,spurious_flag,run_batch,from_email,subject,processed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, email.get("email_id"), email.get("thread_id"), action,
                 decision.get("category") or decision.get("skip_category"),
                 decision.get("assignee_id"), decision.get("reason"), decision.get("confidence"),
                 task_id, 1 if spurious else 0, run_batch, email.get("from_email"),
                 email.get("subject"), ts),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # already logged this email_id for this candidate — idempotency no-op


def classify_with_fallback(email: dict) -> dict:
    """Try Gemini first; on any failure (rate limit exhausted, network, bad JSON),
    fall back to the deterministic rule-based classifier rather than dropping
    the email. A slow/degraded classification beats a silently dropped email."""
    pre = rules.structural_prefilter(email)
    if pre:
        return {"action": "skip", "skip_category": pre["category"], "reason": pre["reason"], "confidence": 0.9}

    if os.environ.get("GEMINI_API_KEY"):
        try:
            decision = gemini_client.classify_email(email)
            decision = rules.apply_hard_business_rules(decision, email)
            return decision
        except Exception as e:
            decision = rules.rule_based_classify(email)
            decision["reason"] = (decision.get("reason") or "") + f" [gemini call failed, used fallback: {e}]"
            decision = rules.apply_hard_business_rules(decision, email)
            return decision
    else:
        decision = rules.rule_based_classify(email)
        return rules.apply_hard_business_rules(decision, email)


@app.post("/ingest")
def ingest(req: IngestRequest):
    cid = norm_email(req.candidate_id)
    if len(req.emails) > 100:
        raise HTTPException(status_code=400, detail="batch too large; max 100 per call")

    processed = created = updated = skipped = 0
    errors = []
    run_batch = uuid.uuid4().hex[:8]
    conn = get_db()

    for email in req.emails:
        email_id = email.get("email_id")
        thread_id = email.get("thread_id")
        try:
            processed += 1

            already = conn.execute(
                "SELECT * FROM email_log WHERE candidate_id=? AND email_id=?", (cid, email_id)
            ).fetchone()
            if already:
                skipped += 1
                continue

            decision = classify_with_fallback(email)

            if decision.get("action") == "skip":
                skipped += 1
                spurious = decision.get("skip_category") not in SPAM_SKIP_CATEGORIES  # true if flagged spam but wasn't obviously spam-shaped -- conservative default False for clean skips
                _log_email(conn, cid, email, decision, run_batch, None, "skipped", spurious=False)
                continue

            existing_thread_task = conn.execute(
                "SELECT * FROM tasks WHERE candidate_id=? AND thread_id=? ORDER BY created_at ASC LIMIT 1",
                (cid, thread_id),
            ).fetchone()

            if existing_thread_task:
                patch_fields = {
                    k: decision[k] for k in
                    ["title", "description", "assignee_id", "category", "priority",
                     "due_date", "deal_value_inr", "company_name", "confidence"]
                    if decision.get(k) is not None
                }
                if patch_fields:
                    set_clause = ", ".join(f"{k}=?" for k in patch_fields)
                    vals = list(patch_fields.values()) + [now_iso(), existing_thread_task["task_id"]]
                    with _lock:
                        conn.execute(
                            f"UPDATE tasks SET {set_clause}, updated_at=?, update_count=update_count+1 WHERE task_id=?",
                            vals,
                        )
                        conn.commit()
                updated += 1
                _log_email(conn, cid, email, decision, run_batch, existing_thread_task["task_id"], "task_updated")
            else:
                task_id = "tsk_" + uuid.uuid4().hex[:8]
                ts = now_iso()
                with _lock:
                    conn.execute(
                        """INSERT INTO tasks (task_id,candidate_id,source_email_id,thread_id,title,description,
                           assignee_id,category,priority,due_date,deal_value_inr,company_name,confidence,
                           created_at,updated_at,update_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                        (task_id, cid, email_id, thread_id, decision.get("title") or email.get("subject") or "Untitled",
                         decision.get("description"), decision["assignee_id"], decision["category"],
                         decision["priority"], decision.get("due_date"), decision.get("deal_value_inr"),
                         decision.get("company_name"), decision.get("confidence", 0.5), ts, ts),
                    )
                    conn.commit()
                created += 1
                _log_email(conn, cid, email, decision, run_batch, task_id, "task_created")

        except Exception as e:
            errors.append({"email_id": email_id, "error": str(e)})

    conn.close()
    return {"processed": processed, "tasks_created": created, "tasks_updated": updated,
            "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# §7.2 backend wrapper endpoints
# ---------------------------------------------------------------------------
@app.get("/api/tasks")
def api_tasks(candidate_id: str):
    cid = norm_email(candidate_id)
    conn = get_db()
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE candidate_id=?", (cid,)).fetchall()]
    skipped = [dict(r) for r in conn.execute(
        "SELECT * FROM email_log WHERE candidate_id=? AND decision='skipped'", (cid,)
    ).fetchall()]
    conn.close()
    return {"tasks": tasks, "skipped_emails": skipped}


@app.get("/api/stats")
def api_stats(candidate_id: str):
    cid = norm_email(candidate_id)
    conn = get_db()
    by_category = {r["category"]: r["c"] for r in conn.execute(
        "SELECT category, COUNT(*) c FROM tasks WHERE candidate_id=? GROUP BY category", (cid,)
    ).fetchall()}
    by_run = {r["run_batch"]: r["c"] for r in conn.execute(
        "SELECT run_batch, COUNT(*) c FROM email_log WHERE candidate_id=? GROUP BY run_batch", (cid,)
    ).fetchall()}
    totals = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM email_log WHERE candidate_id=?) as processed,
             (SELECT COUNT(*) FROM email_log WHERE candidate_id=? AND decision='task_created') as created,
             (SELECT COUNT(*) FROM email_log WHERE candidate_id=? AND decision='task_updated') as updated,
             (SELECT COUNT(*) FROM email_log WHERE candidate_id=? AND decision='skipped') as skipped,
             (SELECT COUNT(*) FROM email_log WHERE candidate_id=? AND spurious_flag=1) as spurious
        """,
        (cid, cid, cid, cid, cid),
    ).fetchone()
    conn.close()
    return {"totals": dict(totals), "by_category": by_category, "by_run": by_run}


class ChatRequest(BaseModel):
    candidate_id: str
    query: str


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    cid = norm_email(req.candidate_id)
    conn = get_db()
    result = query_engine.handle_query(req.query, cid, conn)
    conn.close()
    supporting_data = result["supporting_data"]
    note = result.get("note")

    if note and note.startswith("OUT_OF_SCOPE"):
        answer = ("I can only answer questions about the emails already processed in this batch — "
                   "I can't send messages, schedule anything, or take actions on your behalf.")
        return {"answer": answer, "supporting_data": {}}

    prompt_data = dict(supporting_data)
    if note:
        prompt_data["_internal_note_for_model"] = note
    answer = gemini_client.answer_query(req.query, prompt_data)
    return {"answer": answer, "supporting_data": supporting_data}


@app.get("/health")
def health():
    return {"status": "ok"}
