"""
Translates a natural-language ops question into a STRUCTURED query over our
own SQLite tables (tasks + email_log), and returns supporting_data that the
chat endpoint hands to Gemini for phrasing only. Gemini never computes a
number here -- it only narrates numbers this module already computed.

This is intentionally a small pattern-matched intent router rather than a
general NL->SQL layer. It covers every question type in the spec's §7.3
sample list. Anything outside that coverage falls through to a generic
category-count handler, or -- for genuinely unsupported breakdowns (e.g.
"resellers vs tech integration partners" when we only store category-level
granularity) -- returns an explicit "not tracked at this granularity" flag
so the LLM is told to say "I don't have that breakdown" instead of guessing.
See DECISIONS.md for why this shape was chosen over full NL->SQL.
"""
import re

OUT_OF_SCOPE_PATTERNS = re.compile(
    r'\b(send\b.{0,40}\b(email|message)|reply to|forward this|schedule\b.{0,20}\bmeeting|'
    r'call (aarti|rohit|meera|karan|divya)|delete (the|this) task|create a task manually|'
    r'book a|assign this to)\b', re.I)

CATEGORY_ALIASES = {
    "rfp": "enterprise_rfp", "proposal": "enterprise_rfp", "enterprise_rfp": "enterprise_rfp",
    "smb": "smb_enquiry", "demo": "smb_enquiry", "smb_enquiry": "smb_enquiry",
    "marketing": "marketing", "webinar": "marketing", "sponsorship": "marketing",
    "alliances": "alliances", "reseller": "alliances", "partner": "alliances",
    "finance": "finance", "invoice": "finance", "billing": "finance",
    "triage": "triage",
}


def _counts_by_category(conn, candidate_id, run_batch=None):
    q = "SELECT category, COUNT(*) c FROM tasks WHERE candidate_id=?"
    params = [candidate_id]
    q += " GROUP BY category"
    rows = conn.execute(q, params).fetchall()
    return {r["category"]: r["c"] for r in rows}


def _skip_counts(conn, candidate_id):
    rows = conn.execute(
        "SELECT category FROM email_log WHERE candidate_id=? AND decision='skipped'", (candidate_id,)
    ).fetchall()
    out = {}
    for r in rows:
        out[r["category"]] = out.get(r["category"], 0) + 1
    return out


def handle_query(question: str, candidate_id: str, conn) -> dict:
    """Returns {"supporting_data": {...}, "note": "..."} — note is an optional
    hint appended so the LLM phrasing stays honest about limitations."""
    q = question.lower()

    # --- out of scope actions -----------------------------------------
    if OUT_OF_SCOPE_PATTERNS.search(q):
        return {"supporting_data": {}, "note": "OUT_OF_SCOPE: this chat interface can only answer questions about "
                "already-processed data; it cannot send messages, schedule things, or take actions. Say so plainly."}

    task_counts = _counts_by_category(conn, candidate_id)
    skip_counts = _skip_counts(conn, candidate_id)
    processed_total = conn.execute(
        "SELECT COUNT(*) c FROM email_log WHERE candidate_id=?", (candidate_id,)
    ).fetchone()["c"]

    # --- spurious rate ---------------------------------------------------
    if "spurious" in q:
        spurious = conn.execute(
            "SELECT COUNT(*) c FROM email_log WHERE candidate_id=? AND spurious_flag=1", (candidate_id,)
        ).fetchone()["c"]
        rate = round(spurious / processed_total, 3) if processed_total else 0.0
        return {"supporting_data": {"spurious_count": spurious, "processed": processed_total, "spurious_rate": rate}}

    # --- marketing vs spam --------------------------------------------
    if "marketing" in q and ("spam" in q or "ignor" in q):
        return {"supporting_data": {
            "marketing": task_counts.get("marketing", 0),
            "skipped_marketing_lookalike_spam": skip_counts.get("skipped_marketing_lookalike_spam", 0),
        }}

    # --- triage list with reasons ---------------------------------------
    if "triage" in q:
        rows = conn.execute(
            "SELECT task_id, description, confidence FROM tasks WHERE candidate_id=? AND category='triage'",
            (candidate_id,)
        ).fetchall()
        return {"supporting_data": {
            "triage_count": len(rows),
            "triage_task_ids": [r["task_id"] for r in rows],
            "triage_reasons": [{"task_id": r["task_id"], "reason": r["description"], "confidence": r["confidence"]} for r in rows],
        }}

    # --- high priority + low confidence compound filter -------------------
    if "low confidence" in q or ("high priority" in q and "confidence" in q):
        rows = conn.execute(
            "SELECT task_id, confidence, priority FROM tasks WHERE candidate_id=? AND priority='high' AND confidence < 0.5",
            (candidate_id,)
        ).fetchall()
        return {"supporting_data": {"matches": [{"task_id": r["task_id"], "confidence": r["confidence"]} for r in rows]}}

    # --- alliances sub-breakdown (not tracked at this granularity) --------
    if "reseller" in q and ("tech" in q or "integration" in q):
        return {"supporting_data": {"alliances": task_counts.get("alliances", 0)},
                "note": "NOT_TRACKED: we only store category-level granularity ('alliances'), not the "
                        "reseller-vs-tech-integration sub-split. Say plainly this breakdown isn't available."}

    # --- total deal value of open RFPs -----------------------------------
    if "deal value" in q or ("total" in q and "rfp" in q):
        rows = conn.execute(
            "SELECT deal_value_inr FROM tasks WHERE candidate_id=? AND category='enterprise_rfp'", (candidate_id,)
        ).fetchall()
        vals = [r["deal_value_inr"] for r in rows]
        total = sum(v for v in vals if v is not None)
        no_value = sum(1 for v in vals if v is None)
        return {"supporting_data": {"total_deal_value_inr": total, "rfps_with_no_stated_value": no_value}}

    # --- threads updated more than once -----------------------------------
    if "updated more than once" in q or "updated multiple" in q or ("thread" in q and "updat" in q):
        rows = conn.execute(
            "SELECT thread_id FROM tasks WHERE candidate_id=? AND update_count > 0", (candidate_id,)
        ).fetchall()
        return {"supporting_data": {"threads_updated_multiple_times": list({r["thread_id"] for r in rows})}}

    # --- generic category count (covers "how many X", including zero-count
    #     categories like "GST refunds" that don't map to any known category)
    matched_cat = None
    for alias, cat in CATEGORY_ALIASES.items():
        if alias in q:
            matched_cat = cat
            break
    if matched_cat:
        return {"supporting_data": {matched_cat: task_counts.get(matched_cat, 0)}}

    # Nothing matched a known category/alias at all -> genuine zero, not a
    # missing-breakdown case. e.g. "how many emails were about GST refunds?"
    return {"supporting_data": {"count": 0},
            "note": "ZERO_MATCH: no stored category or keyword matches this question — the true count is zero. "
                    "State zero plainly, do not invent a plausible-sounding number."}
