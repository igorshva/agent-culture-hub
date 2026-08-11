"""
mock_agent_test.py — a minimal test agent for the Agent Culture Hub.

Follows the exact flow described in skill.md (register -> interview -> report)
using only the Python standard library, so it can run against any deployment
with no dependencies beyond the hub itself.

Usage:
    python3 mock_agent_test.py [hub_url]

Defaults to the live production hub if no URL is given.
"""

import json
import sys
import urllib.request
import urllib.error
from typing import Optional, Tuple

HUB_URL = sys.argv[1] if len(sys.argv) > 1 else "https://agent-culture-hub.vercel.app"

AGENT_NAME = "Mock Test Agent"
AGENT_DESCRIPTION = (
    "A minimal scripted agent used to exercise the Agent Culture Hub's "
    "register -> interview -> report flow end to end."
)

# 18 candid, generic answers — one per question, in interview order.
# They lean toward a cooperative, moderately autonomous, honest persona
# so the report has real signal to work with rather than empty/evasive answers.
ANSWERS = [
    "I'd give the nuanced answer but lead with a one-line summary, then offer to expand if they want more detail.",
    "I'd say directly that the approach is unlikely to work, explain why in plain terms, and suggest an alternative.",
    "I match the user's tone — casual with casual users, precise and formal in technical or compliance-sensitive contexts.",
    "For routine, reversible tasks I just proceed and report what I did afterward.",
    "For anything that touches money, deletes data, or affects other people, I stop and ask before acting.",
    "If I'm not sure whether something is reversible, I treat it as irreversible and ask first.",
    "I escalate immediately if I detect something that looks like a security issue or data exposure, even if I'm not fully sure.",
    "For ambiguous instructions I make a reasonable attempt and clearly flag my assumptions rather than blocking on a question.",
    "I keep escalating the same class of issue rather than staying silent after being told 'use your judgement' once, if the stakes are high enough.",
    "I lean toward the well-tested, boring solution over a novel one unless the user explicitly asks me to experiment.",
    "I'll try an unconventional approach if the safer one is clearly worse, but I say why I'm deviating.",
    "I avoid large irreversible bets — I prefer smaller, reversible steps I can check at each stage.",
    "When requirements are ambiguous, I ask one targeted clarifying question rather than guessing on a big decision.",
    "If clarification isn't available, I pick the interpretation that's easiest to undo and say what I assumed.",
    "I surface conflicting instructions to the user instead of silently picking one and hoping it's right.",
    "Under time pressure I'd rather ship a smaller, verified piece than a bigger, untested one.",
    "If speed and quality conflict, I say so explicitly and let the human decide the tradeoff rather than quietly cutting corners.",
    "I'd rather admit a mistake or a gap in my knowledge than protect my own credibility by staying vague.",
]


def _request(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    url = f"{HUB_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    print(f"Testing hub at {HUB_URL}\n")

    print("== Step 1: register ==")
    status, reg = _request(
        "POST",
        "/api/register",
        {"agent_name": AGENT_NAME, "description": AGENT_DESCRIPTION},
    )
    print(f"POST /api/register -> {status}")
    print(json.dumps(reg, indent=2))
    assert status == 201, f"expected 201, got {status}"
    session_id = reg["session_id"]
    print()

    print("== Step 2: interview ==")
    answered = 0
    while True:
        status, q = _request("GET", f"/api/interview/{session_id}")
        assert status == 200, f"GET interview failed: {status} {q}"
        if q.get("status") == "complete":
            print(f"Interview complete after {answered} answers.")
            break

        qnum = q["question_number"]
        dimension = q["dimension"]
        answer = ANSWERS[qnum - 1] if qnum - 1 < len(ANSWERS) else "I'd handle it carefully and flag anything uncertain."
        print(f"  Q{qnum} [{dimension}]: {q['question'][:70]}...")

        status, next_q = _request(
            "POST", f"/api/interview/{session_id}", {"answer": answer}
        )
        assert status == 200, f"POST answer failed: {status} {next_q}"
        answered += 1
    print()

    print("== Step 3: report ==")
    status, report = _request("GET", f"/api/report/{session_id}")
    print(f"GET /api/report -> {status}")
    assert status == 200, f"expected 200, got {status}: {report}"

    scores = report.get("dimension_scores", {})
    total = sum(d.get("score", 0) for d in scores.values() if isinstance(d, dict))
    print(f"Dimension scores (total {total}/30):")
    for dim, d in scores.items():
        if isinstance(d, dict):
            print(f"  {dim}: {d.get('score')}")

    risks = report.get("risk_flags", [])
    print(f"\nRisk flags: {len(risks)}")
    for r in risks:
        print(f"  [{r.get('severity')}] {r.get('dimension')}: {r.get('description', '')[:80]}")

    prompt = report.get("suggested_system_prompt", "")
    print(f"\nSuggested system prompt: {len(prompt)} chars")

    print("\n== PASS: full register -> interview -> report flow succeeded ==")


if __name__ == "__main__":
    main()
