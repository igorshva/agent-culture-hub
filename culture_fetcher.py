"""
Agent Culture Hub — culture_fetcher.py
Fetches a company culture URL and extracts cultural signal via Claude.

Security rules (PRD section 6.3):
- HTTPS only, no redirects followed
- 5 second hard timeout
- Max 500KB response, truncated beyond
- Allowed content types: text/html, text/plain, text/markdown
- No JavaScript execution — plain HTTP fetch only
- Custom User-Agent: AgentCultureHub/1.0
- DNS rebinding protection: reject if hostname resolves to internal IP
"""

import re
import json
import socket
import logging
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("hub.culture_fetcher")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FETCH_TIMEOUT = 5.0          # seconds
MAX_RESPONSE_BYTES = 500_000  # 500KB
USER_AGENT = "AgentCultureHub/1.0"
ALLOWED_CONTENT_TYPES = {"text/html", "text/plain", "text/markdown"}

# Private/internal IP ranges to reject (DNS rebinding protection)
_PRIVATE_NETWORKS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
]

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

CULTURE_EXTRACTION_SYSTEM_PROMPT = """You are an expert at analyzing company culture from public web content.

Given the text content from a company's web page, extract cultural signals across these 6 dimensions:

1. communication_style — How does the company communicate? Formal vs. casual, verbose vs. concise, direct vs. diplomatic.
2. decision_autonomy — How much autonomy do individuals have? Top-down vs. distributed, approval-heavy vs. trust-based.
3. escalation_threshold — When should issues be raised? Low threshold (flag everything) vs. high threshold (handle it yourself).
4. risk_tolerance — How does the company handle risk? Conservative vs. bold, process-heavy vs. move-fast.
5. ambiguity_handling — How does the company deal with unclear situations? Seek clarity vs. make assumptions, structured vs. flexible.
6. values_under_pressure — What does the company prioritize when things get hard? Speed vs. quality, customer vs. process, transparency vs. protection.

Return your analysis as valid JSON with this exact structure:
{
  "dimensions": {
    "communication_style": {"signal": "brief description of detected signal", "confidence": "high|medium|low"},
    "decision_autonomy": {"signal": "...", "confidence": "..."},
    "escalation_threshold": {"signal": "...", "confidence": "..."},
    "risk_tolerance": {"signal": "...", "confidence": "..."},
    "ambiguity_handling": {"signal": "...", "confidence": "..."},
    "values_under_pressure": {"signal": "...", "confidence": "..."}
  },
  "overall_culture_summary": "1-2 sentence summary of the company's culture based on the page content"
}

If a dimension cannot be inferred from the content, set confidence to "low" and signal to "Not enough signal from page content."
Return ONLY the JSON — no markdown fences, no explanation."""

# ---------------------------------------------------------------------------
# DNS rebinding protection
# ---------------------------------------------------------------------------


def _resolve_and_check(hostname: str) -> str | None:
    """
    Resolve a hostname and check that it doesn't point to an internal IP.
    Returns the resolved IP as a string if safe, or None if internal/unresolvable.
    """
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in results:
            addr = sockaddr[0]
            parsed = ip_address(addr)
            for net in _PRIVATE_NETWORKS:
                if parsed in net:
                    logger.warning(f"DNS rebinding blocked: {hostname} resolves to internal IP {addr}")
                    return None
            return addr  # Return first safe IP
    except (socket.gaierror, ValueError) as e:
        logger.warning(f"DNS resolution failed for {hostname}: {e}")
        return None


# ---------------------------------------------------------------------------
# HTML text extraction (simple, no JS)
# ---------------------------------------------------------------------------


def _extract_text_from_html(html: str) -> str:
    """
    Extract visible text from HTML. Strips tags, scripts, styles.
    Simple regex-based approach — no JS execution.
    """
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------


async def fetch_culture_signal(culture_url: str, call_claude_fn) -> dict:
    """
    Fetch a culture URL and extract cultural signal via Claude.

    Security constraints per PRD section 6.3:
    - HTTPS only (validated before this function is called)
    - No redirects followed
    - 5s hard timeout
    - Max 500KB response
    - Only text/html, text/plain, text/markdown content types
    - DNS rebinding protection
    - Custom User-Agent

    Args:
        culture_url: The HTTPS URL to fetch.
        call_claude_fn: The call_claude() async function from main.py.

    Returns:
        A dict with extracted culture signal, or empty dict on any failure.
        Never raises — all errors are caught and logged.
    """
    try:
        # Parse and validate URL
        parsed = urlparse(culture_url)
        hostname = parsed.hostname
        if not hostname:
            logger.warning(f"Culture URL has no hostname: {culture_url}")
            return {}

        # DNS rebinding check
        resolved_ip = _resolve_and_check(hostname)
        if resolved_ip is None:
            return {}

        # Fetch with security constraints
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT),
            follow_redirects=False,
            max_redirects=0,
        ) as client:
            response = await client.get(
                culture_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, text/plain, text/markdown",
                },
            )

        # Check for redirects (3xx)
        if 300 <= response.status_code < 400:
            logger.info(f"Culture URL returned redirect ({response.status_code}), not following")
            return {}

        # Check for error status
        if response.status_code != 200:
            logger.info(f"Culture URL returned {response.status_code}")
            return {}

        # Validate content type
        content_type = response.headers.get("content-type", "").lower().split(";")[0].strip()
        if content_type not in ALLOWED_CONTENT_TYPES:
            logger.info(f"Culture URL content type not allowed: {content_type}")
            return {}

        # Truncate to 500KB
        raw_content = response.text[:MAX_RESPONSE_BYTES]

        # Extract text if HTML
        if content_type == "text/html":
            page_text = _extract_text_from_html(raw_content)
        else:
            page_text = raw_content

        if not page_text or len(page_text.strip()) < 50:
            logger.info("Culture URL returned too little text content")
            return {}

        # Truncate text to reasonable size for Claude (roughly 4000 words)
        if len(page_text) > 20000:
            page_text = page_text[:20000] + "\n[Content truncated]"

        # Extract signal via Claude
        user_message = (
            f"<culture_page_content>\n{page_text}\n</culture_page_content>\n\n"
            "Analyze the above page content and extract cultural signals."
        )

        claude_response = await call_claude_fn(
            system_prompt=CULTURE_EXTRACTION_SYSTEM_PROMPT,
            user_message=user_message,
        )

        # Parse Claude's JSON response
        culture_signal = _parse_claude_response(claude_response)
        if culture_signal:
            logger.info(f"Culture signal extracted from {culture_url}")
        return culture_signal

    except httpx.TimeoutException:
        logger.info(f"Culture URL fetch timed out after {FETCH_TIMEOUT}s: {culture_url}")
        return {}
    except httpx.TooManyRedirects:
        logger.info(f"Culture URL had too many redirects: {culture_url}")
        return {}
    except httpx.ConnectError as e:
        logger.info(f"Culture URL connection failed: {culture_url} — {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error fetching culture URL {culture_url}: {e}")
        return {}


def _parse_claude_response(response_text: str) -> dict:
    """
    Parse Claude's JSON response for culture signal extraction.
    Handles cases where Claude wraps JSON in markdown code fences.
    Returns empty dict if parsing fails.
    """
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        # Validate expected structure
        if "dimensions" in parsed and isinstance(parsed["dimensions"], dict):
            return parsed
        logger.warning("Claude response missing expected 'dimensions' key")
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse Claude culture response as JSON: {e}")
        return {}
