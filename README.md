# Agent Culture Hub

A web service that evaluates AI agents for cultural alignment. Your agent completes an 18-question interview across 6 cultural dimensions and receives a performance report with a suggested revised system prompt.

## Quick start

Give your agent this URL and ask it to follow the instructions:

```
Read https://agent-culture-hub-production.up.railway.app/skill.md and follow the instructions to evaluate your cultural alignment.
```

That's it. Your agent will:

1. **Register** itself with the hub
2. **Complete** an 18-question interview
3. **Return** a report link to you with scores, risk flags, and a suggested system prompt

You review the report and decide what to change. The agent never modifies itself.

## What gets evaluated

Six cultural dimensions, three questions each:

- **Communication style** — formal vs. casual, verbose vs. concise
- **Decision autonomy** — seeks permission vs. takes initiative
- **Escalation threshold** — flags everything vs. handles it alone
- **Risk tolerance** — conservative vs. bold
- **Ambiguity handling** — waits for clarity vs. makes assumptions
- **Values under pressure** — speed vs. quality, transparency vs. protection

Optionally include your company's culture/about page URL during registration. The hub uses it to score your agent against your specific culture rather than generic best practices.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness check |
| GET | `/skill.md` | Agent onboarding instructions |
| POST | `/api/register` | Create evaluation session |
| GET | `/api/interview/{id}` | Get next question |
| POST | `/api/interview/{id}` | Submit answer |
| GET | `/api/report/{id}` | Get evaluation report |

## Self-hosting

Requires Python 3.11+ and an [Anthropic API key](https://console.anthropic.com/).

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required. Anthropic API key |
| `DB_PATH` | `/data/hub.db` | SQLite database path |
| `PORT` | `8000` | Server port |

### Deploy to Railway

The repo includes `railway.toml` with build config, health check, and a persistent volume for SQLite at `/data`. Set `ANTHROPIC_API_KEY` in Railway's variable settings.

## Tech stack

Python 3.11+, FastAPI, SQLite (WAL mode), Anthropic API (claude-sonnet-4-6)

## License

MIT
