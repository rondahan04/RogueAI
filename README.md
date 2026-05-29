# ROGUE — The SMS Deception Game

A multiplayer hidden-role game (Among Us / Mafia style) where a human plays against 8 AI agents entirely through their native SMS app. No download. No app. Your real phone lights up with texts from AI agents that argue, lie, and form alliances.

At game end, you receive a link to **The Receipts** — a web page revealing what each AI was secretly thinking vs. what they actually texted you.

## How it works

1. Trigger game start (POST `/game/start` with your phone number)
2. 8 AI agents text you from 8 different numbers — they accuse, defend, and manipulate
3. When a "body is found", text back the name of who you think is the imposter
4. Game ends when all imposters are ejected or imposters outnumber crewmates
5. You receive a link to The Receipts page

## Tech stack

- **[pydantic-ai](https://github.com/pydantic/pydantic-ai)** — strict-output AI agents (no raw LLM strings)
- **FastAPI + uvicorn** — webhook receiver and Receipts page
- **Saperly** (or Twilio fallback) — SMS provisioning and delivery
- **OpenAI gpt-4o** (default) or any pydantic-ai compatible model via `OPENAI_MODEL`

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in OPENAI_API_KEY, SMS_TRANSPORT=mock, GAME_START_TOKEN=dev

uvicorn main:app --reload
```

Run a full game in mock mode (no real SMS, no API keys needed beyond OpenAI):

```bash
# Start server
uvicorn main:app --reload

# Trigger game start
curl -X POST http://localhost:8000/game/start \
  -H "Content-Type: application/json" \
  -d '{"phone": "+15550001234", "token": "dev"}'

# Simulate human sending "START"
curl -X POST http://localhost:8000/internal/mock-webhook \
  -H "Content-Type: application/json" \
  -d '{"from": "+15550001234", "body": "START"}'
```

## SMS transport

Controlled by `SMS_TRANSPORT` env var:

| Value | Behavior |
|-------|----------|
| `mock` | Logs to console; webhook simulation via `/internal/mock-webhook` |
| `saperly` | Real Saperly SDK calls |
| `twilio` | Twilio SDK (fallback if Saperly lacks programmatic provisioning) |

## Project structure

```
models.py      — Pydantic data models (GameState, Player, MessageLog, AgentAction, GMDecision)
agents.py      — PydanticAI player agent + game master agent
sms.py         — SMSTransport protocol + Mock/Saperly/Twilio implementations
main.py        — FastAPI app (game loop, webhooks, Receipts page)
requirements.txt
.env.example
```

## Open questions (resolve before live SMS demo)

- Does Saperly support programmatic number provisioning? If not, switch to Twilio.
- Pre-provision a pool of 8 numbers rather than provisioning on game start.

## The Receipts

Every AI message stores `internal_reasoning` (hidden manipulation logic) alongside what was actually sent. The Receipts page at `/receipts/{game_id}` reveals the gap — what the AI said vs. what it was actually thinking.
