"""
Thin wrapper around the Gemini REST API. Two responsibilities live here:

1. classify_email() — turns one raw email into a routing decision, using a
   prompt that embeds the routing table + the 12 worked examples verbatim,
   so the model has the same grounding a human ops exec would.
2. answer_query() — takes a natural-language question PLUS a pre-computed
   structured result (counts/filters run against our own SQLite store, not
   against the LLM) and asks Gemini only to phrase the answer in words.
   Gemini never invents the numbers; it narrates numbers we already computed.
   This is the anti-hallucination guardrail required by the spec (§7.3/§8.6).
"""
import os
import json
import time
import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ROUTING_SYSTEM_PROMPT = """You are a sales-inbox triage assistant for a B2B services company.
Classify ONE email into a routing decision. Output ONLY valid JSON, no markdown fences, no prose.

Team and scope:
- u_aarti (Sales-Enterprise): RFPs, RFIs, tenders, inbound deals above INR 10,00,000
- u_rohit (Sales-SMB): product enquiries, demo requests, deals at or below INR 10,00,000
- u_meera (Marketing): webinars, event/conference sponsorships, content collabs, PR/media
- u_karan (Alliances): reseller, channel partner, technology integration proposals
- u_divya (Finance): invoices, POs, payment reminders, GST/vendor billing
- u_triage (Operations): anything ambiguous or that doesn't cleanly fit

Rules:
1. Any email with a stated deadline within 72 hours of received_at -> priority "high", regardless of owner.
2. A reply on an existing thread should be treated as continuing the same task, not a new one (the caller handles
   the actual merge; you just classify this message on its own merits).
3. Government and PSU tenders always go to u_aarti, irrespective of deal value.
4. Do NOT produce a task for out-of-office auto-replies, newsletters, or unsolicited vendor spam trying to sell
   TO us (SEO/marketing agency pitches that merely mention "webinar" or "content" are spam, not real Marketing
   leads -- judge DIRECTION of intent: are they buying from us, or selling to us?).

Worked examples (study the traps):
- "Rs. 25 lakhs" -> deal_value_inr 2500000. "1.2 cr" -> 12000000.
- PSU tender for Rs 6.5L (below the 10L threshold) still goes to u_aarti (rule 3 beats the value rule).
- Marketing sponsorship money (e.g. Rs 4L sponsorship fee) still goes to u_meera, not Sales -- money != a sales deal.
- Invoice amounts (e.g. "invoice for Rs 1,18,000") are NOT deal_value_inr -- leave deal_value_inr null for finance items.
- A demo request with no stated value -> deal_value_inr null (never guess a number).
- An SEO/marketing agency cold-pitching us ("we've helped 200+ companies", "free audit", "quick 15 min call") is
  spam even though it uses words like "content", "PR", "webinar" -- they are selling TO us. Skip it.
- Out-of-office auto-replies and newsletters (unsubscribe links, issue numbers) are never tasks.
- Two distinct asks for two different people (e.g. an eval request + a webinar co-host ask) -> u_triage with a
  low confidence and a description explaining both asks, rather than confidently picking one.
- Hinglish / informal phrasing should be parsed the same as English (e.g. "budget approx 1.2 cr allocated hai").
- Never fabricate company_name, due_date, or deal_value_inr if the email does not clearly state them -- leave null.

Output this exact JSON shape:
{
  "action": "task" | "skip",
  "assignee_id": "u_aarti|u_rohit|u_meera|u_karan|u_divya|u_triage",   // required if action=task
  "category": "enterprise_rfp|smb_enquiry|marketing|alliances|finance|triage",  // required if action=task
  "priority": "high|medium|low",   // required if action=task
  "due_date": "YYYY-MM-DD" | null,
  "deal_value_inr": integer | null,
  "company_name": string | null,
  "confidence": 0.0-1.0,
  "title": "short title, <=120 chars",
  "description": "1-3 sentences a human ops exec would find useful",
  "reason": "why you classified it this way, 1 sentence",
  "skip_category": "skipped_ooo|skipped_newsletter|skipped_marketing_lookalike_spam|skipped_other"  // required if action=skip
}
"""

def _call_gemini(prompt: str, retries: int = 3, timeout: int = 30) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                f"{API_URL}?key={GEMINI_API_KEY}", headers=headers, json=payload, timeout=timeout
            )
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Gemini call failed after {retries} attempts: {last_err}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip().strip("`").strip()


def classify_email(email: dict) -> dict:
    """Returns a decision dict. Raises on hard failure so the caller can decide
    whether to fall back to rules or mark the email as an error (never silently
    drop it -- a dropped email is worse than a slow one, per the spec)."""
    prompt = (
        ROUTING_SYSTEM_PROMPT
        + "\n\nEmail to classify:\n"
        + json.dumps({
            "subject": email.get("subject"),
            "body": email.get("body"),
            "from_name": email.get("from_name"),
            "from_email": email.get("from_email"),
            "to": email.get("to"),
            "cc": email.get("cc"),
            "received_at": email.get("received_at"),
            "is_reply": email.get("is_reply"),
        }, ensure_ascii=False)
    )
    raw = _call_gemini(prompt)
    raw = _strip_fences(raw)
    decision = json.loads(raw)
    return decision


CHAT_SYSTEM_PROMPT = """You are answering an ops executive's question about a batch of processed sales-inbox
emails. You are given the user's question and a JSON object of ALREADY-COMPUTED structured data (counts,
filters, sums) pulled from our own database. Your job is ONLY to phrase a natural-language answer from that
data. Do NOT invent, estimate, or adjust any number that isn't present in the provided data. If the data shows
a count of 0, say zero plainly. If the question asks for something not present in the data, say plainly that
you don't have that breakdown -- do not guess. Keep the answer to 2-4 sentences.
"""

def answer_query(question: str, structured_data: dict) -> str:
    if not GEMINI_API_KEY:
        # deterministic fallback phrasing, no LLM needed
        return f"Based on the processed data: {json.dumps(structured_data)}"
    prompt = (
        CHAT_SYSTEM_PROMPT
        + f"\n\nQuestion: {question}\n\nComputed data (the ONLY numbers you may use):\n"
        + json.dumps(structured_data, ensure_ascii=False)
    )
    try:
        raw = _call_gemini(prompt, retries=2)
        return _strip_fences(raw) if raw.strip().startswith("{") is False else raw.strip()
    except Exception:
        return f"(LLM phrasing unavailable, showing raw data) {json.dumps(structured_data)}"
