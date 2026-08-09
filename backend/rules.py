"""
Deterministic parsing + rule-based classification.

This is the SAFETY NET, not the primary brain. The primary classification comes
from Gemini (see gemini_client.py), which is better at handling messy/Hinglish
text and genuine ambiguity. This module does three jobs:

  1. Pre-filter obvious non-tasks (auto-reply / newsletter / spam) cheaply,
     without spending an LLM call, using strong structural signals.
  2. Parse currency/date mentions deterministically as a cross-check on
     whatever the LLM extracts (LLMs are unreliable at "25 lakhs" -> 2500000
     arithmetic; regex is not).
  3. Enforce the two hard business rules that must NEVER be left to a model's
     judgment: (a) PSU/government tenders always -> u_aarti regardless of
     value, (b) any stated deadline within 72h of received_at -> priority high.
     These are applied as a post-processing pass over whatever the LLM (or
     the rule-based fallback) proposed, so they can't be "argued around" by
     a model call.
"""
import re
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Currency parsing: "Rs. 25 lakhs", "₹6,50,000", "1.2 cr", "Rs 1,18,000"
# ---------------------------------------------------------------------------
LAKH = 100_000
CRORE = 10_000_000

_CURRENCY_PATTERNS = [
    (re.compile(r'(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore|crores)\b', re.I), CRORE),
    (re.compile(r'(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(?:l|lac|lacs|lakh|lakhs)\b', re.I), LAKH),
    (re.compile(r'(?:rs\.?|inr|₹)\s*([\d,]{4,})(?:\.\d+)?\b'), 1),
]

def parse_money_mentions(text: str):
    """Return a list of (raw_match, value_in_inr) found in text, largest-context first."""
    found = []
    for pattern, multiplier in _CURRENCY_PATTERNS:
        for m in pattern.finditer(text):
            raw = m.group(0)
            num = m.group(1).replace(',', '')
            try:
                val = float(num) * multiplier
                found.append((raw.strip(), int(round(val))))
            except ValueError:
                continue
    return found

INVOICE_CONTEXT = re.compile(r'\b(invoice|inv[-\s]?\d|purchase order|\bpo[-\s]?\d|po number|gst)\b', re.I)

def best_deal_value(text: str):
    """Heuristic: prefer amounts NOT immediately adjacent to invoice/PO language,
    since invoice amounts are explicitly NOT deal_value_inr per the spec."""
    mentions = parse_money_mentions(text)
    if not mentions:
        return None
    non_invoice = []
    for raw, val in mentions:
        idx = text.lower().find(raw.lower())
        window = text[max(0, idx - 40): idx + len(raw) + 40].lower()
        if not INVOICE_CONTEXT.search(window):
            non_invoice.append(val)
    pool = non_invoice if non_invoice else []
    if not pool:
        return None
    return max(pool)

# ---------------------------------------------------------------------------
# Deadline parsing (best-effort, English-dominant). Gemini handles the rest
# (e.g. "20th ko hai", "tomorrow EOD") — this is a cross-check only.
# ---------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}

_DATE_PATTERNS = [
    re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})\b'),
    re.compile(r'\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b'),
    re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b'),
]

def is_within_72h(due_date_str, received_at_str):
    if not due_date_str or not received_at_str:
        return False
    try:
        due = datetime.fromisoformat(due_date_str)
        received = datetime.fromisoformat(received_at_str.replace('Z', '+00:00'))
        if due.tzinfo is None:
            due = due.replace(tzinfo=received.tzinfo or timezone.utc)
        delta = (due - received).total_seconds()
        return 0 <= delta <= 72 * 3600 + 3600 * 23  # inclusive-ish of "due end of that day"
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Hard-coded structural signals (cheap pre-filter, applied before any LLM call)
# ---------------------------------------------------------------------------
OOO_PATTERNS = re.compile(
    r'\b(out of office|on leave|automatic reply|auto[-\s]?reply|away from (my )?desk|'
    r'limited access to email|currently unavailable)\b', re.I)

NEWSLETTER_PATTERNS = re.compile(
    r'\b(unsubscribe|newsletter|weekly digest|this (week|edition)\'?s (roundup|issue)|'
    r'view (this )?email in (your )?browser|issue #\d+)\b', re.I)

GOV_PSU_PATTERNS = re.compile(
    r'\b(tender notice|government of|govt\. of|ministry of|psu|public sector undertaking|'
    r'bhel|ongc|nhai|indian railways|gem portal|gem/|nic\.in|gov\.in|\.gov\.in|municipal corporation|'
    r'e-procurement)\b', re.I)

SPAM_SELLING_TO_US_PATTERNS = re.compile(
    r"\b(we('| ha)?ve helped \d+\+? (companies|clients)|free audit|quick \d+\s?min call|"
    r"noticed your website|boost your (seo|rankings|traffic)|grow your (traffic|leads)|"
    r"unlock \d+x|limited time offer|guaranteed results)\b", re.I)


def structural_prefilter(email: dict):
    """Returns 'skip' with a reason if a cheap, high-confidence structural signal
    fires; otherwise returns None (meaning: let the classifier decide)."""
    subject = (email.get("subject") or "")
    body = (email.get("body") or "")
    text = f"{subject}\n{body}"

    if OOO_PATTERNS.search(text):
        return {"skip": True, "reason": "auto-reply / out-of-office pattern matched", "category": "skipped_ooo"}
    if NEWSLETTER_PATTERNS.search(text):
        return {"skip": True, "reason": "newsletter structural pattern matched (unsubscribe link, issue number, etc.)", "category": "skipped_newsletter"}
    return None


def apply_hard_business_rules(decision: dict, email: dict):
    """Post-processing pass: applied AFTER the LLM (or fallback) proposes a
    routing decision, so these two rules can never be silently overridden."""
    text = f"{email.get('subject','')}\n{email.get('body','')}"

    # Rule 3: government/PSU tenders always -> Aarti, regardless of value.
    if decision.get("action") == "task" and GOV_PSU_PATTERNS.search(text):
        decision["assignee_id"] = "u_aarti"
        decision["category"] = "enterprise_rfp"
        decision.setdefault("reason", "")
        decision["reason"] = (decision.get("reason") or "") + " [rule: PSU/government tender forced to u_aarti]"

    # Rule 1: stated deadline within 72h -> priority high, regardless of owner.
    if decision.get("action") == "task" and decision.get("due_date"):
        if is_within_72h(decision["due_date"], email.get("received_at")):
            decision["priority"] = "high"
            decision["reason"] = (decision.get("reason") or "") + " [rule: deadline <72h forces priority=high]"

    return decision


# ---------------------------------------------------------------------------
# Pure rule-based fallback classifier — used ONLY when GEMINI_API_KEY is not
# set, so the system is still demoable/testable without a live key. This is
# deliberately conservative: when unsure, it routes to u_triage with low
# confidence rather than guessing.
# ---------------------------------------------------------------------------
KEYWORDS = {
    "enterprise_rfp": ["rfp", "rfi", "request for proposal", "tender", "proposal for", "bid submission"],
    "smb_enquiry": ["demo", "quick question", "pricing", "trial", "product enquiry", "interested in your product"],
    "marketing": ["webinar", "sponsorship", "sponsor", "conference", "co-host", "speaking slot", "keynote", "content collaboration", "press", "media coverage"],
    "alliances": ["reseller", "channel partner", "partnership", "integrate with", "technology partner", "implementation partner"],
    "finance": ["invoice", "purchase order", " po-", "payment", "overdue", "gst", "billing", "vendor payment"],
}

CATEGORY_TO_ASSIGNEE = {
    "enterprise_rfp": "u_aarti",
    "smb_enquiry": "u_rohit",
    "marketing": "u_meera",
    "alliances": "u_karan",
    "finance": "u_divya",
    "triage": "u_triage",
}

def rule_based_classify(email: dict):
    subject = (email.get("subject") or "").lower()
    body = (email.get("body") or "").lower()
    text = f"{subject}\n{body}"

    pre = structural_prefilter(email)
    if pre:
        return {"action": "skip", "reason": pre["reason"], "skip_category": pre["category"], "confidence": 0.9}

    if SPAM_SELLING_TO_US_PATTERNS.search(text):
        return {"action": "skip", "reason": "vendor is selling TO us (unsolicited outreach), not a genuine inbound lead",
                "skip_category": "skipped_marketing_lookalike_spam", "confidence": 0.75}

    scores = {cat: sum(1 for kw in kws if kw in text) for cat, kws in KEYWORDS.items()}
    best_cat = max(scores, key=scores.get)
    best_score = scores[best_cat]

    if best_score == 0:
        return {
            "action": "task", "assignee_id": "u_triage", "category": "triage", "priority": "medium",
            "due_date": None, "deal_value_inr": None, "company_name": None, "confidence": 0.3,
            "title": (email.get("subject") or "Needs review")[:120],
            "description": "No routing keywords matched confidently; needs human triage.",
            "reason": "rule-based fallback: no strong keyword signal",
        }

    deal_value = best_deal_value(f"{email.get('subject','')}\n{email.get('body','')}")
    if best_cat == "enterprise_rfp" and deal_value is not None and deal_value <= 1_000_000 and not GOV_PSU_PATTERNS.search(text):
        assignee = "u_rohit"
        best_cat = "smb_enquiry"
    else:
        assignee = CATEGORY_TO_ASSIGNEE[best_cat]

    date_match = None
    for pattern in _DATE_PATTERNS:
        m = pattern.search(email.get("body") or "")
        if m:
            date_match = m.group(0)
            break

    return {
        "action": "task",
        "assignee_id": assignee,
        "category": best_cat,
        "priority": "medium",
        "due_date": None,  # fallback deliberately does not guess exact ISO dates from free text
        "deal_value_inr": deal_value,
        "company_name": None,
        "confidence": min(0.65, 0.35 + 0.1 * best_score),
        "title": (email.get("subject") or "Untitled")[:120],
        "description": f"Rule-based routing (no LLM key configured). Matched keywords for '{best_cat}'.",
        "reason": f"rule-based fallback: keyword match on '{best_cat}'",
    }
