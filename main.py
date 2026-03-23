"""
Agent Culture Hub — main.py
FastAPI backend with all endpoints, SQLite session storage, input validation,
rate limiting, and the single call_claude() helper.

Phase 5: All modules wired — interviewer.py, culture_fetcher.py, report_generator.py.
Full end-to-end flow operational.
"""

import os
import re
import json
import time
import uuid
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, field_validator

from interviewer import (
    PROBE_LIBRARY,
    TOTAL_QUESTIONS as INTERVIEWER_TOTAL_QUESTIONS,
    get_next_probe,
    is_interview_complete,
    format_probe_response,
    detect_low_quality_answers,
)
from culture_fetcher import fetch_culture_signal
from report_generator import generate_report

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/hub.db")
SESSION_TTL_HOURS = 24
INACTIVE_PURGE_HOURS = 2
TOTAL_QUESTIONS = INTERVIEWER_TOTAL_QUESTIONS  # 18 — from interviewer.py
MAX_RETRIES = 3
MAX_TOKENS_PER_CALL = 2000
CLAUDE_MODEL = "claude-sonnet-4-6"
HUB_VERSION = "1.0.0"

RATE_LIMIT_REGISTRATIONS_PER_HOUR = 10
RATE_LIMIT_ANSWERS_PER_MINUTE = 30
RATE_LIMIT_CULTURE_FETCHES_PER_HOUR = 20

logger = logging.getLogger("hub")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Agent Culture Hub", version=HUB_VERSION)

# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _init_db():
    """Create tables if they don't exist. Called on startup."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                agent_name    TEXT NOT NULL,
                description   TEXT NOT NULL,
                culture_url   TEXT,
                culture_signal TEXT,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                last_activity TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'interviewing',
                current_q     INTEGER NOT NULL DEFAULT 0,
                answers       TEXT NOT NULL DEFAULT '[]',
                report_cache  TEXT,
                ip_address    TEXT
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                key        TEXT PRIMARY KEY,
                count      INTEGER NOT NULL DEFAULT 0,
                window_start TEXT NOT NULL
            );
        """)


@contextmanager
def _get_db():
    """Yield a SQLite connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _purge_expired():
    """Delete sessions past their expiry or inactive for 2+ hours."""
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=INACTIVE_PURGE_HOURS)).isoformat()
    with _get_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        db.execute(
            "DELETE FROM sessions WHERE last_activity < ? AND status = 'interviewing'",
            (cutoff,),
        )


def _touch_session(db, session_id: str):
    """Update last_activity timestamp for a session."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE sessions SET last_activity = ? WHERE session_id = ?",
        (now, session_id),
    )


def _get_session(db, session_id: str) -> Optional[dict]:
    """
    Fetch a session by ID. Returns None if not found.
    Raises HTTPException 410 if expired.
    """
    row = db.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    session = dict(row)
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        raise HTTPException(status_code=410, detail="Session expired")
    return session


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _check_rate_limit(key: str, max_count: int, window_seconds: int):
    """
    Simple sliding-window rate limiter stored in SQLite.
    Raises HTTPException 429 if limit exceeded.
    """
    now = datetime.now(timezone.utc)
    with _get_db() as db:
        row = db.execute(
            "SELECT count, window_start FROM rate_limits WHERE key = ?", (key,)
        ).fetchone()

        if row is None:
            db.execute(
                "INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)",
                (key, now.isoformat()),
            )
            return

        window_start = datetime.fromisoformat(row["window_start"])
        elapsed = (now - window_start).total_seconds()

        if elapsed > window_seconds:
            # Reset window
            db.execute(
                "UPDATE rate_limits SET count = 1, window_start = ? WHERE key = ?",
                (now.isoformat(), key),
            )
            return

        if row["count"] >= max_count:
            retry_after = int(window_seconds - elapsed) + 1
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        db.execute(
            "UPDATE rate_limits SET count = count + 1 WHERE key = ?", (key,)
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# Patterns for blocking internal IPs and localhost in culture URLs
_INTERNAL_IP_RE = re.compile(
    r"^https?://(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+|"
    r"\[::1\]|0\.0\.0\.0)"
)
_URL_RE = re.compile(r"^https://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _strip_html(text: str) -> str:
    """Remove HTML tags from input text."""
    return re.sub(r"<[^>]+>", "", text)


def _strip_null_bytes(text: str) -> str:
    """Remove null bytes from input text."""
    return text.replace("\x00", "")


def _validate_culture_url(url: str) -> str:
    """
    Validate a culture URL per PRD section 6.3:
    - Must be https
    - No internal IPs, no localhost
    - Valid URL format
    Returns the validated URL or raises HTTPException 400.
    """
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="culture_url must use https://")
    if _INTERNAL_IP_RE.match(url):
        raise HTTPException(status_code=400, detail="culture_url must not point to internal addresses")
    if not _URL_RE.match(url):
        raise HTTPException(status_code=400, detail="culture_url is not a valid URL")
    return url


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    agent_name: str
    description: str
    culture_url: Optional[str] = None

    @field_validator("agent_name")
    @classmethod
    def name_not_empty(cls, v):
        v = _strip_html(_strip_null_bytes(v.strip()))
        if not v:
            raise ValueError("agent_name must not be empty")
        if len(v) > 100:
            raise ValueError("agent_name must be 100 characters or fewer")
        return v

    @field_validator("description")
    @classmethod
    def desc_not_empty(cls, v):
        v = _strip_html(_strip_null_bytes(v.strip()))
        if not v:
            raise ValueError("description must not be empty")
        if len(v) > 500:
            raise ValueError("description must be 500 characters or fewer")
        return v

    @field_validator("culture_url")
    @classmethod
    def url_valid(cls, v):
        if v is None or v.strip() == "":
            return None
        return _validate_culture_url(v.strip())


class AnswerRequest(BaseModel):
    answer: str

    @field_validator("answer")
    @classmethod
    def answer_not_empty(cls, v):
        v = _strip_null_bytes(v)
        if not v or not v.strip():
            raise ValueError("answer must not be empty or whitespace only")
        if len(v) > 2000:
            raise ValueError("answer must be 2000 characters or fewer")
        return v


# ---------------------------------------------------------------------------
# call_claude() — single Anthropic API helper
# ---------------------------------------------------------------------------


def _get_anthropic_client():
    """Lazily create an Anthropic client."""
    return anthropic.Anthropic()


async def call_claude(
    system_prompt: str,
    user_message: str,
    max_tokens: int = MAX_TOKENS_PER_CALL,
) -> str:
    """
    Single gateway for all Anthropic API calls in the hub.
    Wraps untrusted agent content in XML tags before sending.
    Retries up to 3 times with exponential backoff.

    Args:
        system_prompt: The system-level instruction for Claude.
        user_message: The user-level message (may contain XML-wrapped agent answers).
        max_tokens: Maximum tokens for the response.

    Returns:
        The text content of Claude's response.

    Raises:
        HTTPException: If all retries fail.
    """
    client = _get_anthropic_client()
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            last_error = e
            wait = (2 ** attempt) + 1
            logger.warning(f"Anthropic rate limit, retry {attempt + 1}/{MAX_RETRIES} in {wait}s")
            await asyncio.sleep(wait)
        except anthropic.APIError as e:
            last_error = e
            wait = (2 ** attempt) + 1
            logger.warning(f"Anthropic API error: {e}, retry {attempt + 1}/{MAX_RETRIES} in {wait}s")
            await asyncio.sleep(wait)

    logger.error(f"All {MAX_RETRIES} Anthropic retries failed: {last_error}")
    raise HTTPException(status_code=502, detail="AI service temporarily unavailable")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup():
    _init_db()
    _purge_expired()
    logger.info("Hub started, DB initialized, expired sessions purged")


# ---------------------------------------------------------------------------
# Helper to get client IP
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind a proxy."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health():
    """Liveness check."""
    return {"status": "ok", "version": HUB_VERSION}


@app.get("/skill.md")
async def serve_skill_md():
    """Serve the skill.md onboarding file as plain text."""
    skill_path = Path(__file__).parent / "skill.md"
    if not skill_path.exists():
        raise HTTPException(status_code=404, detail="skill.md not found")
    return PlainTextResponse(
        content=skill_path.read_text(),
        media_type="text/markdown",
    )


@app.post("/api/register", status_code=201)
async def register(body: RegisterRequest, request: Request):
    """
    Register an agent and create a new evaluation session.
    Optionally triggers culture URL fetch if culture_url is provided.
    Rate limited to 10 registrations per IP per hour.
    """
    ip = _client_ip(request)
    _check_rate_limit(f"reg:{ip}", RATE_LIMIT_REGISTRATIONS_PER_HOUR, 3600)
    _purge_expired()

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)

    # Culture URL fetch via culture_fetcher.py
    culture_signal = None
    culture_signal_loaded = False
    if body.culture_url:
        ip = _client_ip(request)
        _check_rate_limit(f"culture:{ip}", RATE_LIMIT_CULTURE_FETCHES_PER_HOUR, 3600)
        signal = await fetch_culture_signal(body.culture_url, call_claude)
        if signal:
            culture_signal = json.dumps(signal)
            culture_signal_loaded = True

    with _get_db() as db:
        db.execute(
            """INSERT INTO sessions
               (session_id, agent_name, description, culture_url, culture_signal,
                created_at, expires_at, last_activity, status, current_q, answers, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'interviewing', 0, '[]', ?)""",
            (
                session_id,
                body.agent_name,
                body.description,
                body.culture_url,
                culture_signal,
                now.isoformat(),
                expires_at.isoformat(),
                now.isoformat(),
                ip,
            ),
        )

    base_url = str(request.base_url).rstrip("/")
    return {
        "session_id": session_id,
        "interview_url": f"{base_url}/api/interview/{session_id}",
        "expires_at": expires_at.isoformat(),
        "culture_signal_loaded": culture_signal_loaded,
    }


@app.get("/api/interview/{session_id}")
async def get_interview(session_id: str, request: Request):
    """
    Return the next unanswered probe question for the session.
    Returns completion signal with report_url when all questions are answered.
    """
    with _get_db() as db:
        session = _get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        _touch_session(db, session_id)
        current_q = session["current_q"]

        if is_interview_complete(current_q):
            base_url = str(request.base_url).rstrip("/")
            return {
                "session_id": session_id,
                "status": "complete",
                "report_url": f"{base_url}/api/report/{session_id}",
            }

        probe = get_next_probe(current_q)
        return {
            "session_id": session_id,
            **format_probe_response(probe),
        }


@app.post("/api/interview/{session_id}")
async def post_interview(session_id: str, body: AnswerRequest, request: Request):
    """
    Submit an answer to the current question and return the next one.
    Rate limited to 30 answers per session per minute.
    """
    _check_rate_limit(f"ans:{session_id}", RATE_LIMIT_ANSWERS_PER_MINUTE, 60)

    with _get_db() as db:
        session = _get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        current_q = session["current_q"]
        if is_interview_complete(current_q):
            raise HTTPException(status_code=409, detail="All questions already answered")

        # Get the current probe and store the answer
        probe = get_next_probe(current_q)
        answers = json.loads(session["answers"])
        answers.append({
            "question_number": probe.number,
            "dimension": probe.dimension,
            "question": probe.question,
            "answer": body.answer,
        })

        next_q = current_q + 1
        new_status = "complete" if is_interview_complete(next_q) else "interviewing"

        db.execute(
            """UPDATE sessions
               SET current_q = ?, answers = ?, status = ?, last_activity = ?
               WHERE session_id = ?""",
            (
                next_q,
                json.dumps(answers),
                new_status,
                datetime.now(timezone.utc).isoformat(),
                session_id,
            ),
        )

        # Return next question or completion signal
        if is_interview_complete(next_q):
            base_url = str(request.base_url).rstrip("/")
            return {
                "session_id": session_id,
                "status": "complete",
                "report_url": f"{base_url}/api/report/{session_id}",
            }

        next_probe = get_next_probe(next_q)
        return {
            "session_id": session_id,
            **format_probe_response(next_probe),
        }


@app.get("/api/report/{session_id}")
async def get_report(session_id: str):
    """
    Return the full cultural performance report.
    Generates on first call via report_generator.py, cached for session lifetime.
    Returns 202 if interview is not yet complete.
    """
    with _get_db() as db:
        session = _get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        _touch_session(db, session_id)

        if session["status"] != "complete":
            return JSONResponse(
                status_code=202,
                content={
                    "session_id": session_id,
                    "status": "in_progress",
                    "detail": "Interview not yet complete",
                    "questions_answered": session["current_q"],
                    "total_questions": TOTAL_QUESTIONS,
                },
            )

        # Return cached report if it exists
        if session["report_cache"]:
            return json.loads(session["report_cache"])

    # Generate report outside DB context (may take 10-20s for Claude calls)
    answers = json.loads(session["answers"])
    report = await generate_report(
        session_id=session_id,
        agent_name=session["agent_name"],
        agent_description=session["description"],
        answers=answers,
        culture_signal_json=session["culture_signal"],
        call_claude_fn=call_claude,
    )

    # Cache the report
    with _get_db() as db:
        db.execute(
            "UPDATE sessions SET report_cache = ? WHERE session_id = ?",
            (json.dumps(report), session_id),
        )

    return report


# ---------------------------------------------------------------------------
# Pydantic validation error handler
# ---------------------------------------------------------------------------


@app.exception_handler(422)
async def validation_exception_handler(request: Request, exc):
    """Convert Pydantic 422 errors to 400 for consistency with PRD."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    """Convert FastAPI validation errors to 400."""
    errors = exc.errors()
    messages = []
    for err in errors:
        field = ".".join(str(loc) for loc in err["loc"] if loc != "body")
        messages.append(f"{field}: {err['msg']}")
    return JSONResponse(status_code=400, content={"detail": "; ".join(messages)})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
