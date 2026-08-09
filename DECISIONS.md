# Decisions

## 1. Gemini rate limits and retries

`gemini_client._call_gemini` retries up to 3 times with exponential backoff (2s, 4s, 8s) and
specifically checks for HTTP 429 before backing off. If all retries are exhausted, `main.py`'s
`classify_with_fallback` catches the exception and falls back to the deterministic rule-based
classifier (`rules.rule_based_classify`) rather than dropping the email — a degraded
classification beats a missing one, per the spec's explicit preference (§8.5: "a dropped email
is worse than a slow one"). The fallback's output still goes through
`rules.apply_hard_business_rules`, so PSU-routing and the 72h-deadline rule hold even in
degraded mode. **What I'd do with two more weeks:** move to a job queue (e.g. Celery/RQ) so a
transient Gemini outage doesn't block the synchronous `/ingest` response inside the 15-minute
batch timeout, and add a circuit breaker so a fully-down Gemini doesn't retry-storm on every
email in a 100-email batch.

## 2. Idempotency

Two layers: (a) `email_log` has a `UNIQUE(candidate_id, email_id)` index — a second ingest of
the same `email_id` is detected before any classification work happens and is counted as
`skipped`, not reprocessed. (b) Even without that check, `POST /tasks` from a duplicate
`source_email_id` would still be caught because `/ingest` checks `email_log` first, not
`tasks` directly — this keeps idempotency logic in one place rather than scattered across both
tables. **Tradeoff:** this means idempotency is enforced by the `/ingest` orchestration layer,
not by the raw `POST /tasks` endpoint itself — hitting `/tasks` directly five times with the
same `source_email_id` *will* create five tasks, exactly as the spec warns (§5.6) and expects
to be tested (§8). I read that as intentional: the spec explicitly says dedup is the
candidate's responsibility to add, and grades `/ingest`'s idempotency, not raw `/tasks` POSTs.

## 3. Data model for instant, no-re-Gemini chat answers

`email_log` stores one row per processed email — decision, category, assignee, confidence,
reason, and whether it was skipped — independent of the `tasks` table, which only holds live
task state. This is what makes `/api/chat` fast and Gemini-free for the actual numbers:
`query_engine.py` runs plain SQL aggregates (`GROUP BY category`, `COUNT(*) WHERE spurious_flag`,
etc.) against these two tables and only sends the *result* to Gemini for phrasing. Gemini never
sees raw emails again at chat time. **Tradeoff:** this requires deciding up front which
questions are askable — see decision 5.

## 4. Keeping the chat interface from hallucinating

The query path is: question → `query_engine.handle_query()` (pattern-matches intent, runs SQL,
returns `supporting_data`) → `gemini_client.answer_query()` (phrases `supporting_data` in
words, explicitly instructed never to introduce a number not present in it) → response includes
both `answer` and the raw `supporting_data` so the grader can cross-check. Three specific traps
from the spec are handled structurally, not by hoping the LLM behaves: a zero-count category
returns `{"count": 0}` from SQL (real zero, not an LLM guess); an out-of-scope action request
(`OUT_OF_SCOPE_PATTERNS` regex) short-circuits before Gemini is even called and returns a fixed
refusal string; and a genuinely untracked breakdown (e.g. reseller vs. tech-integration within
"alliances") is flagged with a `NOT_TRACKED` note that instructs the model to say so rather than
invent a split we don't store. **What I'd do with two more weeks:** replace the keyword-intent
router with a small function-calling layer (Gemini picks from a fixed set of query functions
with typed arguments) so compound/novel questions don't fall through to the generic handler —
see the honest gap below.

## 5. One thing I knowingly shipped anyway

`query_engine.py`'s question routing is keyword/pattern matching, not true NL→SQL. It covers
every sample question in the brief, but a rephrased question outside those patterns (e.g. "which
company has the biggest deal we haven't closed yet") falls through to the generic category
counter or the zero-match path, which may answer a different question than the one actually
asked. I chose this over building real function-calling because it's auditable in five
minutes and can't silently invent a SQL query that's subtly wrong — a wrong-but-honest "I don't
have that breakdown" is safer than a fluent-sounding wrong number. The real fix (Gemini as a
function-calling layer over a small fixed set of typed query tools) is a half-day of work I
didn't have time for here.
