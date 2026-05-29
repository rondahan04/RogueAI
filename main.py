from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agents import broadcast_ai_turns, run_game_master
from models import (
    GamePhase,
    GameState,
    MessageLog,
    Player,
    PlayerRole,
)
from sms import SMSTransport, build_transport

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SMS_TRANSPORT = os.getenv("SMS_TRANSPORT", "mock")
GAME_START_TOKEN = os.getenv("GAME_START_TOKEN", "dev")
SYSTEM_PHONE_NUMBER = os.getenv("SYSTEM_PHONE_NUMBER", "+15550000000")
SMS_SEND_DELAY = float(os.getenv("SMS_SEND_DELAY", "0.5"))
MAX_EXPLORATION_ROUNDS = int(os.getenv("MAX_EXPLORATION_ROUNDS", "3"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

_sms_config: dict[str, Any] = {
    "mock_webhook_url": "http://localhost:8000/internal/mock-webhook",
    "saperly_api_key": os.getenv("SAPERLY_API_KEY", ""),
    "system_phone_number": SYSTEM_PHONE_NUMBER,
    "twilio_account_sid": os.getenv("TWILIO_ACCOUNT_SID", ""),
    "twilio_auth_token": os.getenv("TWILIO_AUTH_TOKEN", ""),
    "twilio_number_pool": [
        n.strip()
        for n in os.getenv("TWILIO_NUMBER_POOL", "").split(",")
        if n.strip()
    ],
}

transport: SMSTransport = build_transport(SMS_TRANSPORT, _sms_config)

# ---------------------------------------------------------------------------
# In-memory state store
# ---------------------------------------------------------------------------

games: dict[str, GameState] = {}

# ---------------------------------------------------------------------------
# Personalities
# ---------------------------------------------------------------------------

_PERSONALITIES = [
    "The Emotional One",
    "The Analyst",
    "The Joker",
    "The Paranoid One",
    "The Silent Observer",
    "The Overconfident Leader",
    "The Peacemaker",
    "The Conspiracy Theorist",
]

_NAMES = ["Alex", "Maria", "Jordan", "Sam", "Chris", "Taylor", "Morgan", "Riley"]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ROGUE")


# ---------------------------------------------------------------------------
# Game initialisation
# ---------------------------------------------------------------------------

async def initialize_game(human_phone: str) -> GameState:
    numbers = await transport.provision_numbers(8)
    personalities = _PERSONALITIES[:]
    random.shuffle(personalities)

    roles = [PlayerRole.IMPOSTER] * 2 + [PlayerRole.CREWMATE] * 6
    random.shuffle(roles)

    players = [
        Player(
            name=_NAMES[i],
            saperly_number=numbers[i],
            role=roles[i],
            personality=personalities[i],
        )
        for i in range(8)
    ]

    state = GameState(
        game_id=str(uuid.uuid4()),
        human_phone_number=human_phone,
        system_phone_number=SYSTEM_PHONE_NUMBER,
        players=players,
    )
    games[state.game_id] = state

    await send_system(
        state,
        f"ROGUE started. You are an observer. 8 players remain. "
        f"2 are imposters. Watch closely and vote wisely.",
    )
    return state


# ---------------------------------------------------------------------------
# SMS helpers
# ---------------------------------------------------------------------------

async def send_system(state: GameState, body: str) -> None:
    await transport.send(state.system_phone_number, state.human_phone_number, body)
    state.chat_history.append(
        MessageLog(
            sender_name="SYSTEM",
            sender_number=state.system_phone_number,
            content=body,
            timestamp=datetime.utcnow(),
        )
    )


async def send_player_action(state: GameState, player: Player, action) -> None:
    if not action.message_to_send:
        return
    await transport.send(player.saperly_number, state.human_phone_number, action.message_to_send)
    state.chat_history.append(
        MessageLog(
            sender_name=player.name,
            sender_number=player.saperly_number,
            content=action.message_to_send,
            timestamp=datetime.utcnow(),
            is_private=(action.action_type == "private_manipulate"),
            internal_reasoning=action.internal_reasoning,
        )
    )


# ---------------------------------------------------------------------------
# Game loop
# ---------------------------------------------------------------------------

async def run_exploration_round(state: GameState) -> None:
    state.is_processing = True
    try:
        results = await broadcast_ai_turns(state)

        for player, action in results:
            await send_player_action(state, player, action)
            if SMS_SEND_DELAY > 0:
                await asyncio.sleep(SMS_SEND_DELAY)

        gm = await run_game_master(state)

        if gm.action == "end_game":
            await end_game(state, gm.outcome or "Game Over.")
        elif gm.action == "trigger_meeting" or state.round_num >= MAX_EXPLORATION_ROUNDS:
            announcement = gm.outcome or "-------- Body found! --------"
            await send_system(state, announcement)
            state.phase = GamePhase.EMERGENCY_MEETING
            await run_meeting(state)
        else:
            state.round_num += 1
    finally:
        state.is_processing = False


async def run_meeting(state: GameState) -> None:
    state.is_processing = True
    try:
        results = await broadcast_ai_turns(state)
        for player, action in results:
            await send_player_action(state, player, action)
            if SMS_SEND_DELAY > 0:
                await asyncio.sleep(SMS_SEND_DELAY)

        alive_names = ", ".join(p.name for p in state.players if p.is_alive)
        await send_system(
            state,
            f"Alive: {alive_names}. Reply with the name of the player to eject.",
        )
        state.phase = GamePhase.VOTING
    finally:
        state.is_processing = False


async def resolve_vote(state: GameState, voted_name: str) -> None:
    target = next(
        (p for p in state.players if p.is_alive and p.name.lower() == voted_name.lower()),
        None,
    )
    if not target:
        await send_system(state, f"'{voted_name}' is not a valid living player. Vote again.")
        return

    target.is_alive = False
    await send_system(
        state,
        f"{target.name} has been ejected. They were a {target.role.value}.",
    )

    imposters_alive = [p for p in state.players if p.is_alive and p.role == PlayerRole.IMPOSTER]
    crewmates_alive = [p for p in state.players if p.is_alive and p.role == PlayerRole.CREWMATE]

    if not imposters_alive:
        await end_game(state, "CREWMATES WIN. All imposters have been ejected.")
        return
    if len(imposters_alive) >= len(crewmates_alive):
        await end_game(state, "IMPOSTERS WIN. They have taken over the ship.")
        return

    state.phase = GamePhase.EXPLORATION
    state.round_num += 1
    await run_exploration_round(state)


async def end_game(state: GameState, announcement: str) -> None:
    state.phase = GamePhase.GAME_OVER
    receipts_url = os.getenv("BASE_URL", "http://localhost:8000") + f"/receipts/{state.game_id}"
    await send_system(state, f"{announcement}")
    await send_system(state, f"Game Over. View the AI's secret thoughts: {receipts_url}")


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    phone: str
    token: str


@app.post("/game/start")
async def start_game(req: StartRequest):
    if req.token != GAME_START_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    state = await initialize_game(req.phone)
    asyncio.create_task(run_exploration_round(state))
    return {"game_id": state.game_id, "status": "started"}


class WebhookPayload(BaseModel):
    # Saperly / Twilio webhook shape — adjust field names to match provider
    From: str = ""
    Body: str = ""
    # Alternative casing for mock webhook
    from_: str = ""
    body: str = ""

    def sender(self) -> str:
        return self.From or self.from_

    def text(self) -> str:
        return self.Body or self.body


@app.post("/saperly/webhook")
async def saperly_webhook(req: Request):
    data = await req.form()
    sender = str(data.get("From", data.get("from", "")))
    body = str(data.get("Body", data.get("body", ""))).strip()
    await _handle_inbound(sender, body)
    return {"status": "ok"}


@app.post("/internal/mock-webhook")
async def mock_webhook(payload: WebhookPayload):
    """Dev-only endpoint to simulate inbound SMS from the human."""
    await _handle_inbound(payload.sender(), payload.text().strip())
    return {"status": "ok"}


async def _handle_inbound(sender: str, body: str) -> None:
    state = _find_game_for_phone(sender)
    if not state:
        print(f"[inbound] no game for {sender}, ignoring")
        return

    if state.is_processing:
        print(f"[inbound] game {state.game_id} busy, ignoring '{body}'")
        return

    if body.upper() == "START" and state.phase == GamePhase.EXPLORATION and state.round_num == 1:
        asyncio.create_task(run_exploration_round(state))
        return

    if state.phase == GamePhase.VOTING:
        asyncio.create_task(resolve_vote(state, body))
        return

    # Log human message to history even if we can't act on it
    state.chat_history.append(
        MessageLog(
            sender_name="HUMAN",
            sender_number=sender,
            content=body,
            timestamp=datetime.utcnow(),
        )
    )


def _find_game_for_phone(phone: str) -> GameState | None:
    for state in games.values():
        if state.human_phone_number == phone and state.phase != GamePhase.GAME_OVER:
            return state
    return None


# ---------------------------------------------------------------------------
# Receipts page
# ---------------------------------------------------------------------------

@app.get("/receipts/{game_id}", response_class=HTMLResponse)
async def receipts(game_id: str):
    state = games.get(game_id)
    if not state:
        raise HTTPException(status_code=404, detail="Game not found")

    rows = []
    for msg in state.chat_history:
        private_badge = (
            '<span style="color:#c0392b;font-weight:bold;margin-left:8px;">'
            "🔒 PRIVATE — only you saw this</span>"
            if msg.is_private
            else ""
        )
        reasoning_block = (
            f'<div style="color:#666;font-style:italic;font-size:0.9em;'
            f'margin-top:6px;border-left:3px solid #ccc;padding-left:8px;">'
            f"🧠 <em>{msg.internal_reasoning}</em></div>"
            if msg.internal_reasoning
            else ""
        )
        border = "border:2px solid #c0392b;" if msg.is_private else "border:1px solid #ddd;"
        rows.append(
            f'<div style="margin:12px 0;padding:12px;border-radius:6px;{border}">'
            f'<strong>{msg.sender_name}</strong>'
            f'<span style="color:#999;font-size:0.85em;margin-left:8px;">'
            f'{msg.timestamp.strftime("%H:%M:%S")}</span>'
            f"{private_badge}"
            f'<div style="margin-top:6px;">"{msg.content}"</div>'
            f"{reasoning_block}"
            f"</div>"
        )

    player_rows = "".join(
        f'<tr><td>{p.name}</td><td>{p.role.value}</td>'
        f'<td>{"✅" if p.is_alive else "❌ ejected"}</td>'
        f"<td>{p.personality}</td></tr>"
        for p in state.players
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ROGUE — The Receipts</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 720px; margin: 40px auto; padding: 0 20px; background: #fafafa; }}
  h1 {{ color: #2c3e50; }}
  h2 {{ color: #555; margin-top: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
<h1>🕵️ ROGUE — The Receipts</h1>
<p>Game ID: <code>{game_id}</code> &nbsp;|&nbsp; Status: <strong>{state.phase.value}</strong></p>

<h2>Players</h2>
<table>
  <tr><th>Name</th><th>Role</th><th>Status</th><th>Personality</th></tr>
  {player_rows}
</table>

<h2>Chat History</h2>
{"".join(rows) or "<p>No messages yet.</p>"}
</body>
</html>"""
    return html
