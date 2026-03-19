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
