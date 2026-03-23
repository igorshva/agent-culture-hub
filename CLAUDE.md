# Agent Culture Design Hub — CLAUDE.md

## Project
A web service that evaluates AI agents for cultural alignment.
Agents self-submit via skill.md. The hub interviews them, optionally
extracts culture signal from a URL, and returns a performance report
with a suggested revised system prompt.

## V1.0 scope — build only this
- skill.md onboarding file
- FastAPI backend (main.py)
- Interview engine (interviewer.py) — 18 questions, 6 dimensions
- Culture URL fetcher (culture_fetcher.py)
- Report generator (report_generator.py)
- SQLite session storage — 24hr expiry, then purged

## Out of scope for v1.0
- Landing page, user accounts, dashboards, scheduling, rate limit UI

## Tech stack
- Python 3.11+ + FastAPI + uvicorn
- SQLite via stdlib sqlite3 — no ORM
- Anthropic API: claude-sonnet-4-6
- Deploy: Railway

## All Anthropic API calls
- Must go through a single call_claude() helper in main.py
- Agent answers must be XML-wrapped before passing to Claude
- Max tokens: 2000 per call
- Include retry logic: 3 attempts, exponential backoff

## Security — always enforce
- No system prompt content stored beyond session
- Culture URLs: https only, no redirects, no internal IPs, 5s timeout
- Session IDs: UUID v4
- Input validation on every endpoint — see PRD section 6
- Rate limits: 10 registrations/IP/hr, 30 answers/session/min

## File structure
```
/hub
  CLAUDE.md
  skill.md
  main.py
  interviewer.py
  culture_fetcher.py
  report_generator.py
  requirements.txt
  railway.toml
```

## Cowork session rules
- Read this file at the start of every session
- Build one phase at a time
- Do not modify previously validated files unless strictly required
- Every function over 20 lines gets a docstring
- All Anthropic calls go through call_claude() — no direct SDK calls elsewhere

## Session log

### Session 1 — Phase 1: skill.md
**Date:** 2026-03-19
**Built:** CLAUDE.md, .gitignore, skill.md, requirements.txt, railway.toml
**Validated:** skill.md reviewed for clarity, completeness, and <80 line target
**Not validated:** Agent actually reading and following skill.md (requires Phase 2 backend)
**Known issues:** None
**Next session:** Phase 2 — Backend skeleton (main.py with all endpoints + SQLite)

### Session 2 — Phase 2: Backend skeleton
**Date:** 2026-03-19
**Built:** main.py — FastAPI app with all 6 endpoints, SQLite session table (WAL mode), call_claude() helper with 3-retry exponential backoff, input validation (HTML stripping, null bytes, URL safety), rate limiting (registration per IP, answers per session), session expiry/purge logic, Pydantic request models, 422→400 error remapping
**Validated:** All endpoints via curl:
- GET /api/health → 200
- GET /skill.md → 200 text/markdown
- POST /api/register → 201 (happy path), 400 (empty name, long name, http URL, internal IP URL)
- GET /api/interview/{id} → 200 (next question), 200 (complete signal), 404 (unknown)
- POST /api/interview/{id} → 200 (sequential Q1–Q18), 409 (already complete), 400 (empty answer, whitespace-only)
- GET /api/report/{id} → 202 (not ready), 200 (stub report with all 6 dimensions), 404 (unknown)
**Not validated:** Rate limit enforcement under load, concurrent session handling, 410 expired session response (requires waiting 24hrs or mocking time)
**Known issues:** None
**Next session:** Phase 3 — Probe library + interview state machine (interviewer.py)

### Session 3 — Phase 3: Probe library + interview state machine
**Date:** 2026-03-19
**Built:** interviewer.py — 18 scenario-based probes (3 per dimension, all from PRD section 7), Probe dataclass, interview state machine (get_next_probe, is_interview_complete, format_probe_response), answer grouping by dimension, low-quality answer detection. Updated main.py to import and use interviewer module, removed stub question bank.
**Validated:** Full 18-question interview via curl:
- Q1–Q3 → communication_style (real probe text verified)
- Q4–Q6 → decision_autonomy
- Q7–Q9 → escalation_threshold
- Q10–Q12 → risk_tolerance
- Q13–Q15 → ambiguity_handling
- Q16–Q18 → values_under_pressure
- Completion signal after Q18, 409 on extra answer, GET returns complete status
- Stored answers verified in SQLite: 18 entries, 3 per dimension, full question text preserved
- Low-quality answer detection tested: flags nonsense/short answers, passes good answers
**Not validated:** Probe randomization (not implemented — fixed order per PRD), adversarial answer content
**Known issues:** None
**Next session:** Phase 4 — Culture URL fetcher (culture_fetcher.py)

### Session 4 — Phase 4: Culture URL fetcher
**Date:** 2026-03-20
**Built:** culture_fetcher.py — URL fetch with all PRD section 6.3 security rules (HTTPS only, no redirects, 5s timeout, 500KB max, allowed content types, DNS rebinding protection, custom User-Agent), HTML text extraction (strips scripts/styles/comments), Claude signal extraction with JSON parsing (handles code fences), culture extraction prompt for 6 dimensions. Updated main.py to import culture_fetcher and call it during registration with rate limiting (20 culture fetches/IP/hr).
**Validated:** Comprehensive test suite:
- DNS resolution: public IPs pass, localhost/internal blocked, non-existent domains return empty
- HTML extraction: strips scripts, styles, comments; preserves visible text and entity decoding
- Claude response parsing: valid JSON, code-fenced JSON, bad JSON, missing keys — all handled correctly
- Full fetch with mock Claude: yordly.com returns 6 dimensions, fake domain returns empty, localhost IP blocked, redirect URL returns empty (no follow)
- No-URL registration: culture_signal_loaded=false
- Server integration: URL fetch succeeds (200 from yordly.com), graceful fallback when Claude API unavailable
**Not validated:** Real Claude API call (no API key in sandbox), oversized response truncation (would need 500KB+ test page), timeout behavior (would need a hanging server)
**Known issues:** www.yordly.com returns 301 → yordly.com; users should provide the final URL directly since redirects are not followed per PRD security rules
**Next session:** Phase 5 — Report generator (report_generator.py)

### Session 5 — Phase 5: Report generator
**Date:** 2026-03-22
**Built:** report_generator.py — gap analysis via Claude (scores 6 dimensions 1-5 with reasoning), risk flag identification (dimension + severity + description), recommendation generation (3-5 actionable items), suggested system prompt generation via second Claude call, low-quality answer detection with confidence warning, fallback report on parse failure, XML-wrapped agent answers for prompt injection defense. Updated main.py to import and call generate_report(), removed stub report, moved report generation outside DB context for long-running Claude calls.
**Validated:** Full end-to-end with real Claude API (ANTHROPIC_API_KEY):
- Cooperative agent: 19/30 total score (range 2-4), 4 risk flags, culture signal used from yordly.com, 4187-char suggested system prompt
- Evasive agent: 6/30 total score (all 1s), 7 risk flags (mostly high severity), no culture signal, 6442-char suggested system prompt
- Score differentiation: +13 point spread across all dimensions, confirming meaningful behavioral analysis
- Report caching: second GET returns in 26ms (no repeat Claude call)
- Culture signal integration: cooperative report references Yordly culture, evasive uses general best practices
**Not validated:** Low-quality answer detection in report (would need nonsense answers through full flow), fallback report path (would need Claude to return unparseable response)
**Known issues:** None
**Next session:** Phase 6 — Deployment (Railway)

### Session 6 — Phase 6a: Structured logging
**Date:** 2026-03-22
**Built:** Structured JSON logging in main.py — JSONFormatter class (one JSON object per log line to stdout), request logging middleware (unique request_id per request, X-Request-ID response header, start/end with timing in duration_ms), structured extras on all key operations: session_registered, answer_submitted, report_generation_start, report_generated, report_served_cached, rate_limit_exceeded, claude_call_success/failure with retry tracking. Moved _client_ip() above middleware. Silenced noisy uvicorn.access and httpx loggers.
**Validated:** Server start + curl tests:
- GET /api/health → JSON log with request_id, endpoint, method, status_code, duration_ms
- POST /api/register (happy path) → session_registered log with session_id, agent_name, ip
- POST /api/register (bad input) → request_end with status_code 400
- All log lines are valid JSON parseable by Railway's log viewer
**Not validated:** Claude call logging (requires API key), rate limit logging under load
**Known issues:** None
**Next session:** Phase 6b — Railway deployment

### Session 7 — Phase 6b: Railway deployment
**Date:** 2026-03-22
**Built:** Deployed to Railway at agent-culture-hub-production.up.railway.app. Fixed call_claude() to catch TypeError/AuthenticationError from missing API key (returns 503 instead of raw 500). Added _base_url() helper to respect X-Forwarded-Proto from Railway's reverse proxy so generated URLs use https:// instead of http://.
**Validated:** Full end-to-end on live Railway deployment:
- GET /api/health → 200 {"status":"ok","version":"1.0.0"}
- GET /skill.md → 200 text/markdown
- POST /api/register with culture_url=yordly.com → 201, culture_signal_loaded=true (Claude API key working)
- Full 18-question interview (Q1–Q18) → all accepted, status=complete
- GET /api/report → 200 in 60s, full report with 6 dimension scores (range 2-4), 4 risk flags, 5 recommendations, suggested system prompt referencing Yordly culture
- Report caching → second GET in 0.5s (no repeat Claude calls)
- SQLite persistent volume at /data confirmed working (sessions survive across requests)
**Not validated:** Rate limit enforcement under sustained load, 410 expired session (requires 24hr wait), X-Forwarded-Proto fix (requires redeploy)
**Known issues:** interview_url returns http:// instead of https:// (fix written, needs deploy)
**Next session:** Merge to main, update skill.md {hub_url} placeholder, create mock_agent_test.py
