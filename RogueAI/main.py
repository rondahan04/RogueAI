from __future__ import annotations

import asyncio
import os
import re

from dotenv import load_dotenv

load_dotenv()
import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import broadcast_ai_turns, run_game_master
from models import (
    GamePhase,
    GameState,
    MessageLog,
    Player,
    PlayerHistory,
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
GRUDGE_DIR = Path(os.getenv("GRUDGE_DIR", ".grudges"))

_sms_config: dict[str, Any] = {
    "mock_webhook_url": os.getenv("BASE_URL", "http://localhost:8001") + "/internal/mock-webhook",
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

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/api/info")
async def api_info():
    return {"system_phone": SYSTEM_PHONE_NUMBER}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Grudge system — long-term memory per phone number
# ---------------------------------------------------------------------------

def _phone_slug(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _load_history(phone: str) -> PlayerHistory:
    path = GRUDGE_DIR / f"{_phone_slug(phone)}.json"
    if not path.exists():
        return PlayerHistory()
    try:
        return PlayerHistory.model_validate_json(path.read_text())
    except Exception:
        print(f"[grudge] corrupted history for {phone} — resetting")
        return PlayerHistory()


def _save_history(phone: str, state: GameState, announcement: str) -> None:
    try:
        h = state.player_history or PlayerHistory()
        h.games_played += 1
        if state.voted_name:
            h.votes_cast = (h.votes_cast + [state.voted_name])[-10:]
        if "CREWMATES WIN" in announcement:
            h.crewmates_wins += 1
        GRUDGE_DIR.mkdir(exist_ok=True)
        (GRUDGE_DIR / f"{_phone_slug(phone)}.json").write_text(h.model_dump_json())
    except Exception as exc:
        print(f"[grudge] failed to save history for {phone}: {exc}")


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
        player_history=_load_history(human_phone),
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
            timestamp=datetime.now(timezone.utc),
        )
    )


async def send_player_action(state: GameState, player: Player, action) -> None:
    if not action.message_to_send:
        return
    sms_body = f"[{player.name}]: {action.message_to_send}"
    await transport.send(player.saperly_number, state.human_phone_number, sms_body)
    state.chat_history.append(
        MessageLog(
            sender_name=player.name,
            sender_number=player.saperly_number,
            content=action.message_to_send,
            timestamp=datetime.now(timezone.utc),
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
    state.voted_name = target.name  # record for grudge history
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
    if state.phase == GamePhase.GAME_OVER:
        return  # prevent double-call
    state.phase = GamePhase.GAME_OVER
    _save_history(state.human_phone_number, state, announcement)
    receipts_url = os.getenv("BASE_URL", "http://localhost:8001") + f"/receipts/{state.game_id}"
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
    # Saperly sends JSON; fall back to form for other transports
    ct = req.headers.get("content-type", "")
    if "application/json" in ct:
        data = await req.json()
    else:
        data = dict(await req.form())
    sender = str(data.get("from") or data.get("From") or "")
    body = str(data.get("text") or data.get("body") or data.get("Body") or "").strip()
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
            timestamp=datetime.now(timezone.utc),
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
        return HTMLResponse(
            status_code=404,
            content="""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROGUE — Game Not Found</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 720px; margin: 40px auto; padding: 0 20px; background: #fafafa; }
  h1 { color: #2c3e50; }
  p { color: #666; }
</style>
</head>
<body>
<h1>🕵️ ROGUE — Game Not Found</h1>
<p>No game found for this ID. The game may have ended or the link may be incorrect.</p>
</body>
</html>""",
        )

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

    # Load grudge history for the track record section
    history = _load_history(state.human_phone_number)
    if history.games_played > 0:
        accuracy = f"{history.crewmates_wins}/{history.games_played}"
        votes_str = ", ".join(history.votes_cast) if history.votes_cast else "none"
        track_record_html = f"""
<div style="background:#fff8e1;border:1px solid #f9a825;border-radius:8px;padding:16px;margin-bottom:24px;">
  <strong>📋 Your Track Record</strong><br>
  <span style="color:#555;">Games played before this one: <b>{history.games_played}</b></span><br>
  <span style="color:#555;">Crewmate wins: <b>{accuracy}</b></span><br>
  <span style="color:#555;">Past ejection votes: <b>{votes_str}</b></span><br>
  <span style="color:#888;font-size:0.85em;">The AIs knew this. They were watching.</span>
</div>"""
    else:
        track_record_html = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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

{track_record_html}

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


# ---------------------------------------------------------------------------
# Dashboard — live game view
# ---------------------------------------------------------------------------

@app.get("/api/game/{game_id}")
async def api_game_state(game_id: str):
    state = games.get(game_id)
    if not state:
        raise HTTPException(status_code=404, detail="Game not found")
    return JSONResponse({
        "game_id": state.game_id,
        "phase": state.phase.value,
        "round_num": state.round_num,
        "is_processing": state.is_processing,
        "players": [
            {
                "name": p.name,
                "role": p.role.value,
                "personality": p.personality,
                "is_alive": p.is_alive,
            }
            for p in state.players
        ],
        "chat_history": [
            {
                "sender_name": msg.sender_name,
                "content": msg.content,
                "timestamp": msg.timestamp.strftime("%H:%M:%S"),
                "is_private": msg.is_private,
                "internal_reasoning": msg.internal_reasoning,
            }
            for msg in state.chat_history
        ],
    })


@app.get("/dashboard/{game_id}", response_class=HTMLResponse)
async def dashboard(game_id: str):
    state = games.get(game_id)
    if not state:
        return HTMLResponse(
            status_code=404,
            content="""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROGUE — Game Not Found</title>
<style>
  body{background:#0F172A;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
  h1{color:#D97706;}p{color:#94a3b8;}
</style>
</head>
<body><div style="text-align:center"><h1>Game Not Found</h1><p>No game found for this ID.</p></div></body>
</html>""",
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROGUE — Live Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Josefin+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --bg: #0F172A;
    --surface: #1E293B;
    --surface2: #0f1e33;
    --border: rgba(255,255,255,0.08);
    --amber: #D97706;
    --amber-light: #F59E0B;
    --indigo: #6366F1;
    --red: #EF4444;
    --green: #22C55E;
    --muted: #64748B;
    --text: #F1F5F9;
    --text-dim: #94A3B8;
  }}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{
    background:var(--bg);
    color:var(--text);
    font-family:'Josefin Sans',sans-serif;
    font-size:14px;
    min-height:100vh;
  }}

  /* ── HEADER ── */
  header{{
    background:var(--surface);
    border-bottom:1px solid var(--border);
    padding:16px 24px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    position:sticky;
    top:0;
    z-index:100;
  }}
  .logo{{
    font-family:'Cinzel',serif;
    font-size:20px;
    font-weight:700;
    letter-spacing:0.15em;
    color:var(--amber);
    text-shadow:0 0 20px rgba(217,119,6,0.5);
  }}
  .logo span{{color:var(--text-dim);font-size:12px;font-family:'Josefin Sans',sans-serif;margin-left:12px;letter-spacing:0.05em;font-weight:300;}}
  .header-right{{display:flex;align-items:center;gap:12px;}}
  .phase-badge{{
    padding:4px 12px;
    border-radius:20px;
    font-size:11px;
    font-weight:600;
    letter-spacing:0.1em;
    text-transform:uppercase;
  }}
  .phase-EXPLORATION{{background:rgba(99,102,241,0.2);color:#818CF8;border:1px solid rgba(99,102,241,0.4);}}
  .phase-EMERGENCY_MEETING{{background:rgba(239,68,68,0.2);color:#FCA5A5;border:1px solid rgba(239,68,68,0.4);animation:pulse-red 1.5s ease-in-out infinite;}}
  .phase-VOTING{{background:rgba(217,119,6,0.2);color:#FCD34D;border:1px solid rgba(217,119,6,0.4);animation:pulse-amber 1.5s ease-in-out infinite;}}
  .phase-GAME_OVER{{background:rgba(100,116,139,0.2);color:#94A3B8;border:1px solid rgba(100,116,139,0.4);}}
  .round-indicator{{
    font-size:12px;
    color:var(--text-dim);
    font-weight:500;
    letter-spacing:0.05em;
  }}
  .live-dot{{
    width:8px;height:8px;border-radius:50%;
    background:var(--green);
    box-shadow:0 0 8px rgba(34,197,94,0.8);
    animation:pulse-green 2s ease-in-out infinite;
    display:inline-block;
    margin-right:6px;
  }}
  .live-dot.processing{{background:var(--amber);box-shadow:0 0 8px rgba(217,119,6,0.8);animation:pulse-amber-dot 0.8s ease-in-out infinite;}}
  .live-dot.offline{{background:var(--muted);box-shadow:none;animation:none;}}

  /* ── LAYOUT ── */
  .layout{{
    display:grid;
    grid-template-columns:1fr 380px;
    grid-template-rows:auto 1fr;
    gap:1px;
    background:var(--border);
    height:calc(100vh - 57px);
  }}
  .panel{{background:var(--bg);overflow:hidden;}}
  .panel-header{{
    padding:14px 20px;
    border-bottom:1px solid var(--border);
    font-family:'Cinzel',serif;
    font-size:11px;
    font-weight:600;
    letter-spacing:0.2em;
    color:var(--text-dim);
    text-transform:uppercase;
    display:flex;
    align-items:center;
    justify-content:space-between;
  }}
  .panel-header .count{{
    font-family:'Josefin Sans',sans-serif;
    font-size:11px;
    font-weight:500;
    color:var(--text-dim);
    background:var(--surface);
    padding:2px 8px;
    border-radius:10px;
    letter-spacing:0;
  }}

  /* ── STATS BAR ── */
  .stats-bar{{
    grid-column:1/-1;
    display:grid;
    grid-template-columns:repeat(4,1fr);
    background:var(--surface2);
    border-bottom:1px solid var(--border);
  }}
  .stat{{
    padding:14px 20px;
    border-right:1px solid var(--border);
    display:flex;
    flex-direction:column;
    gap:3px;
  }}
  .stat:last-child{{border-right:none;}}
  .stat-label{{
    font-size:10px;
    font-weight:600;
    letter-spacing:0.15em;
    text-transform:uppercase;
    color:var(--muted);
  }}
  .stat-value{{
    font-family:'Cinzel',serif;
    font-size:24px;
    font-weight:700;
    color:var(--text);
    line-height:1;
  }}
  .stat-value.amber{{color:var(--amber);text-shadow:0 0 16px rgba(217,119,6,0.4);}}
  .stat-value.red{{color:var(--red);text-shadow:0 0 16px rgba(239,68,68,0.4);}}
  .stat-value.green{{color:var(--green);text-shadow:0 0 16px rgba(34,197,94,0.4);}}

  /* ── PLAYERS GRID ── */
  .players-panel{{grid-row:2;overflow-y:auto;}}
  .players-grid{{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
    gap:12px;
    padding:16px;
  }}
  .player-card{{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:12px;
    padding:16px;
    transition:border-color 0.2s ease,box-shadow 0.2s ease;
    cursor:default;
    position:relative;
    overflow:hidden;
  }}
  .player-card::before{{
    content:'';
    position:absolute;
    top:0;left:0;right:0;
    height:2px;
  }}
  .player-card.crewmate::before{{background:linear-gradient(90deg,var(--indigo),transparent);}}
  .player-card.imposter::before{{background:linear-gradient(90deg,var(--red),transparent);}}
  .player-card.ejected{{
    opacity:0.4;
    filter:grayscale(0.8);
  }}
  .player-card:hover:not(.ejected){{
    border-color:rgba(255,255,255,0.15);
    box-shadow:0 4px 24px rgba(0,0,0,0.4);
  }}
  .player-top{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:10px;}}
  .player-name{{
    font-family:'Cinzel',serif;
    font-size:15px;
    font-weight:600;
    color:var(--text);
  }}
  .player-status-icon{{font-size:16px;line-height:1;}}
  .player-role{{
    display:inline-block;
    font-size:9px;
    font-weight:700;
    letter-spacing:0.15em;
    text-transform:uppercase;
    padding:2px 8px;
    border-radius:4px;
    margin-bottom:8px;
  }}
  .role-CREWMATE{{background:rgba(99,102,241,0.15);color:#818CF8;border:1px solid rgba(99,102,241,0.3);}}
  .role-IMPOSTER{{background:rgba(239,68,68,0.15);color:#FCA5A5;border:1px solid rgba(239,68,68,0.3);}}
  .player-personality{{
    font-size:11px;
    color:var(--text-dim);
    font-weight:300;
    font-style:italic;
  }}

  /* ── CHAT FEED ── */
  .chat-panel{{grid-row:2;display:flex;flex-direction:column;overflow:hidden;}}
  .chat-feed{{
    flex:1;
    overflow-y:auto;
    padding:12px;
    display:flex;
    flex-direction:column;
    gap:8px;
    scroll-behavior:smooth;
  }}
  .chat-msg{{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:10px;
    padding:12px;
    animation:slide-in 0.25s ease-out;
  }}
  .chat-msg.private{{
    border-color:rgba(239,68,68,0.3);
    background:rgba(239,68,68,0.04);
  }}
  .chat-msg.system{{
    border-color:rgba(99,102,241,0.3);
    background:rgba(99,102,241,0.04);
  }}
  .chat-msg.human{{
    border-color:rgba(34,197,94,0.3);
    background:rgba(34,197,94,0.04);
  }}
  .msg-header{{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap;}}
  .msg-sender{{font-weight:600;font-size:12px;color:var(--text);}}
  .msg-time{{font-size:10px;color:var(--muted);margin-left:auto;}}
  .msg-badge{{
    font-size:9px;
    font-weight:700;
    letter-spacing:0.1em;
    text-transform:uppercase;
    padding:1px 6px;
    border-radius:3px;
  }}
  .badge-private{{background:rgba(239,68,68,0.2);color:#FCA5A5;}}
  .badge-system{{background:rgba(99,102,241,0.2);color:#818CF8;}}
  .badge-human{{background:rgba(34,197,94,0.2);color:#86EFAC;}}
  .msg-content{{
    font-size:13px;
    color:var(--text-dim);
    line-height:1.5;
    word-break:break-word;
  }}
  .msg-reasoning{{
    margin-top:8px;
    padding:8px 10px;
    background:rgba(0,0,0,0.3);
    border-left:2px solid var(--amber);
    border-radius:0 6px 6px 0;
    font-size:11px;
    color:var(--muted);
    font-style:italic;
    line-height:1.5;
  }}
  .msg-reasoning-label{{
    font-size:9px;
    font-weight:700;
    letter-spacing:0.1em;
    color:var(--amber);
    text-transform:uppercase;
    margin-bottom:4px;
    font-style:normal;
  }}
  .empty-chat{{
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    height:100%;
    color:var(--muted);
    gap:8px;
  }}
  .empty-chat svg{{opacity:0.3;}}

  /* ── SCROLLBAR ── */
  ::-webkit-scrollbar{{width:4px;}}
  ::-webkit-scrollbar-track{{background:transparent;}}
  ::-webkit-scrollbar-thumb{{background:var(--border);border-radius:2px;}}
  ::-webkit-scrollbar-thumb:hover{{background:rgba(255,255,255,0.15);}}

  /* ── ANIMATIONS ── */
  @keyframes pulse-red{{0%,100%{{box-shadow:0 0 0 0 rgba(239,68,68,0);}}50%{{box-shadow:0 0 0 4px rgba(239,68,68,0.2);}}}}
  @keyframes pulse-amber{{0%,100%{{box-shadow:0 0 0 0 rgba(217,119,6,0);}}50%{{box-shadow:0 0 0 4px rgba(217,119,6,0.2);}}}}
  @keyframes pulse-green{{0%,100%{{opacity:1;}}50%{{opacity:0.4;}}}}
  @keyframes pulse-amber-dot{{0%,100%{{opacity:1;transform:scale(1);}}50%{{opacity:0.6;transform:scale(0.8);}}}}
  @keyframes slide-in{{from{{opacity:0;transform:translateY(6px);}}to{{opacity:1;transform:translateY(0);}}}}

  @media(prefers-reduced-motion:reduce){{
    *{{animation:none!important;transition:none!important;}}
  }}

  /* ── RESPONSIVE ── */
  @media(max-width:768px){{
    .layout{{grid-template-columns:1fr;grid-template-rows:auto auto 1fr;height:auto;}}
    .players-panel{{grid-row:auto;max-height:400px;}}
    .chat-panel{{grid-row:auto;height:60vh;}}
    .stats-bar{{grid-template-columns:repeat(2,1fr);}}
  }}
  @media(max-width:480px){{
    .stats-bar{{grid-template-columns:repeat(2,1fr);}}
    .players-grid{{grid-template-columns:1fr 1fr;}}
    header{{padding:12px 16px;}}
    .logo{{font-size:16px;}}
  }}
</style>
</head>
<body>

<header>
  <div class="logo">
    ROGUE
    <span>THE GAME</span>
  </div>
  <div class="header-right">
    <div id="phase-badge" class="phase-badge phase-EXPLORATION">Exploration</div>
    <div id="round-badge" class="round-indicator">Round <span id="round-num">1</span></div>
    <div>
      <span id="live-dot" class="live-dot"></span>
      <span id="live-label" style="font-size:11px;color:var(--text-dim);font-weight:500;">Live</span>
    </div>
  </div>
</header>

<div class="layout">

  <div class="stats-bar" id="stats-bar">
    <div class="stat">
      <div class="stat-label">Alive</div>
      <div class="stat-value green" id="stat-alive">8</div>
    </div>
    <div class="stat">
      <div class="stat-label">Ejected</div>
      <div class="stat-value red" id="stat-ejected">0</div>
    </div>
    <div class="stat">
      <div class="stat-label">Imposters</div>
      <div class="stat-value amber" id="stat-imposters">2</div>
    </div>
    <div class="stat">
      <div class="stat-label">Messages</div>
      <div class="stat-value" id="stat-messages">0</div>
    </div>
  </div>

  <div class="panel players-panel">
    <div class="panel-header">
      Players
      <span class="count" id="player-count">8 active</span>
    </div>
    <div class="players-grid" id="players-grid">
    </div>
  </div>

  <div class="panel chat-panel">
    <div class="panel-header">
      Live Feed
      <span class="count" id="msg-count">0 messages</span>
    </div>
    <div class="chat-feed" id="chat-feed">
      <div class="empty-chat" id="empty-state">
        <svg width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8.625 9.75a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375m-13.5 3.01c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.184-4.183a1.14 1.14 0 01.778-.332 48.294 48.294 0 005.83-.498c1.585-.233 2.708-1.626 2.708-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z"/></svg>
        <span style="font-size:12px;">Waiting for messages...</span>
      </div>
    </div>
  </div>

</div>

<script>
const GAME_ID = "{game_id}";
let lastMsgCount = 0;
let isAutoScrolling = true;

const chatFeed = document.getElementById('chat-feed');
chatFeed.addEventListener('scroll', () => {{
  const atBottom = chatFeed.scrollHeight - chatFeed.scrollTop - chatFeed.clientHeight < 60;
  isAutoScrolling = atBottom;
}});

const PHASE_LABELS = {{
  EXPLORATION: 'Exploration',
  EMERGENCY_MEETING: 'Emergency!',
  VOTING: 'Voting',
  GAME_OVER: 'Game Over',
}};

function renderPlayers(players) {{
  const grid = document.getElementById('players-grid');
  const alive = players.filter(p => p.is_alive).length;
  const ejected = players.filter(p => !p.is_alive).length;
  const imposters = players.filter(p => p.role === 'IMPOSTER' && p.is_alive).length;

  document.getElementById('stat-alive').textContent = alive;
  document.getElementById('stat-ejected').textContent = ejected;
  document.getElementById('stat-imposters').textContent = imposters;
  document.getElementById('player-count').textContent = `${{alive}} active`;

  grid.innerHTML = players.map(p => `
    <div class="player-card ${{p.role.toLowerCase()}} ${{p.is_alive ? '' : 'ejected'}}">
      <div class="player-top">
        <div class="player-name">${{p.name}}</div>
        <div class="player-status-icon">${{p.is_alive ? '●' : '✕'}}</div>
      </div>
      <div class="player-role role-${{p.role}}">${{p.role}}</div>
      <div class="player-personality">${{p.personality}}</div>
    </div>
  `).join('');
}}

function renderChat(messages) {{
  const feed = document.getElementById('chat-feed');
  const emptyState = document.getElementById('empty-state');
  const msgCount = document.getElementById('msg-count');
  const statMessages = document.getElementById('stat-messages');

  msgCount.textContent = `${{messages.length}} messages`;
  statMessages.textContent = messages.length;

  if (messages.length === 0) {{
    emptyState.style.display = 'flex';
    return;
  }}
  emptyState.style.display = 'none';

  if (messages.length === lastMsgCount) return;

  const newMessages = messages.slice(lastMsgCount);
  lastMsgCount = messages.length;

  newMessages.forEach(msg => {{
    const cls = msg.sender_name === 'SYSTEM' ? 'system'
              : msg.sender_name === 'HUMAN' ? 'human'
              : msg.is_private ? 'private' : '';

    const badge = msg.sender_name === 'SYSTEM'
      ? '<span class="msg-badge badge-system">System</span>'
      : msg.sender_name === 'HUMAN'
      ? '<span class="msg-badge badge-human">Human</span>'
      : msg.is_private
      ? '<span class="msg-badge badge-private">Private</span>'
      : '';

    const reasoning = msg.internal_reasoning
      ? `<div class="msg-reasoning"><div class="msg-reasoning-label">AI Reasoning</div>${{msg.internal_reasoning}}</div>`
      : '';

    const el = document.createElement('div');
    el.className = `chat-msg ${{cls}}`;
    el.innerHTML = `
      <div class="msg-header">
        <span class="msg-sender">${{msg.sender_name}}</span>
        ${{badge}}
        <span class="msg-time">${{msg.timestamp}}</span>
      </div>
      <div class="msg-content">${{msg.content}}</div>
      ${{reasoning}}
    `;
    feed.appendChild(el);
  }});

  if (isAutoScrolling) {{
    feed.scrollTop = feed.scrollHeight;
  }}
}}

function updatePhase(phase, roundNum, isProcessing) {{
  const badge = document.getElementById('phase-badge');
  badge.className = `phase-badge phase-${{phase}}`;
  badge.textContent = PHASE_LABELS[phase] || phase;
  document.getElementById('round-num').textContent = roundNum;

  const dot = document.getElementById('live-dot');
  const label = document.getElementById('live-label');
  if (phase === 'GAME_OVER') {{
    dot.className = 'live-dot offline';
    label.textContent = 'Ended';
  }} else if (isProcessing) {{
    dot.className = 'live-dot processing';
    label.textContent = 'Processing';
  }} else {{
    dot.className = 'live-dot';
    label.textContent = 'Live';
  }}
}}

async function poll() {{
  try {{
    const res = await fetch(`/api/game/${{GAME_ID}}`);
    if (!res.ok) return;
    const data = await res.json();
    updatePhase(data.phase, data.round_num, data.is_processing);
    renderPlayers(data.players);
    renderChat(data.chat_history);
  }} catch(e) {{
    console.error('Poll error:', e);
  }}
}}

poll();
const interval = setInterval(poll, 3000);
</script>
</body>
</html>""")


# ---------------------------------------------------------------------------
# Home — Game Command Center
# ---------------------------------------------------------------------------

_HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RogueAI — Command Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Galindo&family=JetBrains+Mono:ital,wght@0,400;0,500;1,400&family=Orbitron:wght@700;900&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#020617;--surface:rgba(10,18,40,0.88);--accent:#22C55E;
  --accent-dim:rgba(34,197,94,0.12);--accent-glow:0 0 12px rgba(34,197,94,0.55),0 0 32px rgba(34,197,94,0.2);
  --red:#EF4444;--amber:#F59E0B;--blue:#60A5FA;--purple:#A78BFA;
  --border:rgba(51,65,85,0.7);--text:#F8FAFC;--muted:#94A3B8;
  --font-h:'Galindo','Orbitron',monospace;--font-b:'JetBrains Mono',monospace
}
html,body{width:100%;height:100%;background:var(--bg);color:var(--text);font-family:var(--font-b);overflow-x:hidden}
body::after{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px);pointer-events:none;z-index:9999}
#bg{position:fixed;inset:0;z-index:0}
#app{position:relative;z-index:10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem 1rem}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);padding:2.5rem;width:100%;max-width:860px;box-shadow:0 0 60px rgba(34,197,94,0.04),inset 0 1px 0 rgba(255,255,255,0.04)}
.panel-header{text-align:center;margin-bottom:2rem}
.logo-img{height:44px;width:auto;margin-bottom:.75rem;filter:drop-shadow(0 0 8px rgba(34,197,94,0.4));display:block;margin-left:auto;margin-right:auto}
.panel-title{font-family:var(--font-h);font-size:clamp(1.6rem,4vw,2.8rem);font-weight:900;letter-spacing:.25em;color:var(--accent);text-shadow:var(--accent-glow)}
.panel-sub{font-size:.65rem;color:var(--muted);letter-spacing:.18em;margin-top:.35rem;text-transform:uppercase}

/* ── Launcher ── */
.form-group{margin-bottom:.9rem}
.form-label{display:block;font-size:.6rem;letter-spacing:.2em;color:var(--muted);text-transform:uppercase;margin-bottom:.35rem}
.form-input{width:100%;background:rgba(2,6,23,.8);border:1px solid var(--border);border-radius:8px;padding:.75rem 1rem;font-family:var(--font-b);font-size:1rem;color:var(--text);outline:none;transition:border-color 200ms,box-shadow 200ms}
.form-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(34,197,94,.18)}
.form-input::placeholder{color:var(--muted);opacity:.55}
.btn-start{width:100%;margin-top:.4rem;padding:1rem;background:var(--accent);color:#020617;border:none;border-radius:8px;font-family:var(--font-h);font-size:.85rem;font-weight:700;letter-spacing:.2em;text-transform:uppercase;cursor:pointer;transition:transform 150ms,box-shadow 150ms}
.btn-start:hover:not(:disabled){box-shadow:var(--accent-glow);transform:translateY(-1px)}
.btn-start:active:not(:disabled){transform:translateY(0)}
.btn-start:disabled{opacity:.45;cursor:not-allowed}
.status-msg{margin-top:.9rem;text-align:center;font-size:.75rem;color:var(--muted);min-height:1.2em;letter-spacing:.05em}
.status-msg.err{color:var(--red)}.status-msg.ok{color:var(--accent)}

/* ── Dashboard ── */
#dashboard{display:none}
#dashboard.on{display:block}
.dash-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.75rem;margin-bottom:1.5rem;padding-bottom:1rem;border-bottom:1px solid var(--border)}
.gid-label{font-size:.6rem;color:var(--muted);letter-spacing:.1em}
.gid-label code{color:var(--text);background:rgba(255,255,255,.05);padding:.15em .5em;border-radius:4px;cursor:pointer;font-size:.75rem}
.phase-badge{font-family:var(--font-h);font-size:.65rem;letter-spacing:.15em;padding:.35em .9em;border-radius:999px;border:1px solid currentColor;text-transform:uppercase;white-space:nowrap}
.ph-EXPLORATION{color:var(--blue);box-shadow:0 0 10px rgba(96,165,250,.15)}
.ph-EMERGENCY_MEETING{color:var(--amber);animation:pulseA 1s ease-in-out infinite}
.ph-VOTING{color:var(--red);animation:pulseR .75s ease-in-out infinite}
.ph-GAME_OVER{color:var(--purple);box-shadow:0 0 14px rgba(167,139,250,.25)}
@keyframes pulseA{0%,100%{box-shadow:0 0 8px rgba(245,158,11,.2)}50%{box-shadow:0 0 20px rgba(245,158,11,.6)}}
@keyframes pulseR{0%,100%{box-shadow:0 0 8px rgba(239,68,68,.25)}50%{box-shadow:0 0 22px rgba(239,68,68,.7)}}
.round-label{font-size:.7rem;color:var(--muted)}
.round-label span{color:var(--text);font-weight:500}

/* ── Waiting ── */
.waiting{text-align:center;padding:1.5rem 0}
.pulse-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);margin-right:8px;animation:pd 1.4s ease-in-out infinite;vertical-align:middle}
@keyframes pd{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}
.gid-box{margin:1rem 0;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:8px;padding:1rem}
.gid-box strong{font-size:.58rem;letter-spacing:.18em;color:var(--muted);display:block;margin-bottom:.3rem}
.gid-box span{color:var(--accent);font-size:.95rem;cursor:pointer;word-break:break-all}
.sys-phone{font-size:.7rem;color:var(--muted);margin-top:.5rem}
.sys-phone b{color:var(--accent)}

/* ── Players grid ── */
.players-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:.85rem;margin-bottom:1.5rem}
.pcard{background:rgba(2,6,23,.6);border:1px solid var(--border);border-radius:12px;padding:.9rem .75rem;display:flex;flex-direction:column;align-items:center;gap:.4rem;transition:border-color .4s,opacity .4s;position:relative;overflow:hidden}
.pcard.alive{border-color:rgba(34,197,94,.3)}
.pcard.dead{opacity:.35;border-color:rgba(239,68,68,.15)}
.pcard.dead::after{content:'✕';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:2.5rem;color:var(--red);opacity:.18;pointer-events:none}
.pavatar{width:34px;height:auto;image-rendering:pixelated}
.pname{font-family:var(--font-h);font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;text-align:center}
.pstatus{font-size:.52rem;letter-spacing:.15em;text-transform:uppercase}
.pcard.alive .pstatus{color:var(--accent)}.pcard.dead .pstatus{color:var(--red)}
.prole{font-size:.55rem;padding:.12em .55em;border-radius:4px;letter-spacing:.1em;text-transform:uppercase;margin-top:.1rem}
.r-imposter{background:rgba(239,68,68,.18);color:#FCA5A5;border:1px solid rgba(239,68,68,.3)}
.r-crewmate{background:rgba(34,197,94,.1);color:#86EFAC;border:1px solid rgba(34,197,94,.2)}

/* ── Game over banner ── */
.gameover-banner{text-align:center;padding:1.5rem;background:rgba(167,139,250,.07);border:1px solid rgba(167,139,250,.25);border-radius:12px;margin-bottom:1.5rem}
.gameover-banner h2{font-family:var(--font-h);font-size:1.1rem;letter-spacing:.2em;color:var(--purple);text-shadow:0 0 16px rgba(167,139,250,.5);margin-bottom:.5rem}
.gameover-banner p{font-size:.75rem;color:var(--muted)}
.btn-new{margin-top:1rem;padding:.6rem 1.6rem;background:transparent;border:1px solid var(--accent);color:var(--accent);border-radius:8px;font-family:var(--font-h);font-size:.7rem;letter-spacing:.15em;cursor:pointer;transition:background 150ms}
.btn-new:hover{background:var(--accent-dim)}

.panel-footer{text-align:center;margin-top:1.5rem;font-size:.55rem;color:rgba(148,163,184,.3);letter-spacing:.12em}
@media(max-width:600px){.panel{padding:1.25rem .9rem}.players-grid{grid-template-columns:repeat(2,1fr)}.panel-title{font-size:1.6rem}}
</style>
</head>
<body>
<canvas id="bg"></canvas>
<div id="app">
  <div class="panel">

    <div class="panel-header">
      <img class="logo-img" src="/static/logo.png" alt="ROGUE" onerror="this.style.display='none'">
      <div class="panel-title">RogueAI</div>
      <div class="panel-sub">AI social deception engine &mdash; SMS edition</div>
    </div>

    <!-- Launcher -->
    <div id="launcher">
      <div class="form-group">
        <label class="form-label" for="inp-phone">Your Phone Number</label>
        <input class="form-input" type="tel" id="inp-phone" placeholder="+1 (555) 000-0000" autocomplete="tel">
      </div>
      <div class="form-group">
        <label class="form-label" for="inp-token">Game Token</label>
        <input class="form-input" type="password" id="inp-token" placeholder="••••••••" autocomplete="off">
      </div>
      <button class="btn-start" id="btn-start" onclick="startGame()">&#9654; INITIATE GAME</button>
      <div class="status-msg" id="status-msg"></div>
      <div style="margin-top:1.5rem;border-top:1px solid rgba(255,255,255,0.1);padding-top:1.25rem;">
        <label class="form-label" for="inp-watch">Watch Existing Game</label>
        <div style="display:flex;gap:.5rem;">
          <input class="form-input" type="text" id="inp-watch" placeholder="Paste game ID..." autocomplete="off" style="flex:1;font-size:.75rem;">
          <button class="btn-start" style="flex:0;padding:.55rem 1rem;font-size:.7rem;" onclick="watchGame()">WATCH</button>
        </div>
      </div>
    </div>

    <!-- Dashboard -->
    <div id="dashboard">

      <!-- Waiting for player to text START -->
      <div id="view-wait" class="waiting">
        <p style="font-size:.8rem;color:var(--muted);margin-bottom:.25rem">
          <span class="pulse-dot"></span>GAME CREATED &mdash; AWAITING YOUR FIRST SMS
        </p>
        <div class="gid-box">
          <strong>GAME ID</strong>
          <span id="gid-copy" onclick="copyId()" title="Click to copy"></span>
        </div>
        <p class="sys-phone">Text <b>START</b> to <b id="sys-phone-num">...</b> to begin</p>
        <p style="font-size:.6rem;color:var(--muted);margin-top:.5rem">Dashboard auto-updates every 3 seconds</p>
      </div>

      <!-- Live game view -->
      <div id="view-live" style="display:none">
        <div id="gameover-banner" class="gameover-banner" style="display:none">
          <h2 id="gameover-title">GAME OVER</h2>
          <p id="gameover-sub"></p>
          <button class="btn-new" onclick="resetToLauncher()">&#9654; NEW GAME</button>
        </div>
        <div class="dash-header">
          <div class="gid-label">GAME: <code id="gid-live" onclick="copyId()" title="Copy ID"></code></div>
          <span class="phase-badge" id="phase-badge">&mdash;</span>
          <div class="round-label">ROUND <span id="round-num">&mdash;</span></div>
        </div>
        <div class="players-grid" id="players-grid"></div>
      </div>

    </div>

    <div class="panel-footer">RogueAI &bull; AI SMS DECEPTION ENGINE &bull; AMONG US INSPIRED</div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════
//  CANVAS: Starfield + Walking Crewmates
// ═══════════════════════════════════════════════════════
const canvas = document.getElementById('bg');
const ctx = canvas.getContext('2d');

function resize() { canvas.width = innerWidth; canvas.height = innerHeight; }
resize();
window.addEventListener('resize', resize);

// Stars
const stars = Array.from({length:200}, () => ({
  x: Math.random(), y: Math.random(),
  r: Math.random() * 1.3 + 0.3,
  a: Math.random() * 0.7 + 0.2,
  da: (Math.random() * 0.007 + 0.002) * (Math.random() > .5 ? 1 : -1)
}));

// Crewmate data — 26 unique crewmates floating across full screen
const ALL_HUES = [0, 220, 120, 280, 50, 25, 320, 180, 160, 200, 260, 340,
                  10, 240, 100, 300, 70, 40, 190, 270, 80, 150, 230, 350, 15, 195];

function makeCrewmate(hue) {
  const sz = 28 + Math.random() * 32; // varied sizes: 28-60px (depth illusion)
  return {
    x: Math.random() * innerWidth,
    y: Math.random() * innerHeight,         // full screen, not just bottom
    vx: (Math.random() * 0.5 + 0.18) * (Math.random() > .5 ? 1 : -1),
    vy: (Math.random() * 0.22 - 0.11),      // slow vertical drift in space
    spin: (Math.random() - 0.5) * 0.012,    // gentle tumble
    angle: Math.random() * Math.PI * 2,
    hue,
    dead: false,
    sz,
    alpha: 0.45 + Math.random() * 0.3       // varied opacity for depth
  };
}
const crews = ALL_HUES.map(makeCrewmate);

const playerImg = new Image();
playerImg.src = '/static/single_crew.png';

// single_crew.png is 178×264 — aspect ratio ~0.674 (width/height)
const CREW_W_RATIO = 178 / 264;

function drawFrame() {
  // Background gradient
  const g = ctx.createRadialGradient(innerWidth/2, innerHeight/2, 0, innerWidth/2, innerHeight/2, Math.max(innerWidth,innerHeight)*.8);
  g.addColorStop(0, '#0c1428');
  g.addColorStop(1, '#020617');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Stars
  stars.forEach(s => {
    s.a += s.da;
    if (s.a > .95 || s.a < .12) s.da *= -1;
    ctx.beginPath();
    ctx.arc(s.x * canvas.width, s.y * canvas.height, s.r, 0, Math.PI*2);
    ctx.fillStyle = `rgba(248,250,252,${s.a})`;
    ctx.fill();
  });

  // Crewmates floating in space
  if (playerImg.complete && playerImg.naturalWidth > 0) {
    const W = innerWidth, H = innerHeight;
    crews.forEach(c => {
      c.x += c.vx;
      c.y += c.vy;
      c.angle += c.spin;
      // Wrap around all edges
      const pad = c.sz + 10;
      if (c.x < -pad) c.x = W + pad;
      if (c.x > W + pad) c.x = -pad;
      if (c.y < -pad) c.y = H + pad;
      if (c.y > H + pad) c.y = -pad;

      const drawH = c.sz;
      const drawW = drawH * CREW_W_RATIO;

      ctx.save();
      ctx.globalAlpha = c.dead ? 0.12 : c.alpha;
      ctx.filter = `hue-rotate(${c.hue}deg) saturate(1.7) brightness(0.85)`;
      ctx.translate(c.x, c.y);
      ctx.rotate(c.angle);
      // Flip to face direction of travel
      if (c.vx < 0) ctx.scale(-1, 1);
      ctx.drawImage(playerImg, -drawW / 2, -drawH / 2, drawW, drawH);
      ctx.restore();
    });
  }

  requestAnimationFrame(drawFrame);
}
requestAnimationFrame(drawFrame);

// ═══════════════════════════════════════════════════════
//  GAME STATE
// ═══════════════════════════════════════════════════════
let gameId = null;
let pollTimer = null;
let sysPhone = '';

const PHASE_LABELS = {
  EXPLORATION:'EXPLORING',
  EMERGENCY_MEETING:'EMERGENCY',
  VOTING:'VOTING',
  GAME_OVER:'GAME OVER'
};

const CREW_CSS_FILTERS = ALL_HUES.map(h => `hue-rotate(${h}deg) saturate(1.4) brightness(0.9)`);

// Fetch system phone on load
fetch('/api/info').then(r=>r.json()).then(d => {
  sysPhone = d.system_phone || '';
  document.getElementById('sys-phone-num').textContent = sysPhone;
}).catch(() => {});

function setStatus(msg, type='') {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = 'status-msg' + (type ? ' '+type : '');
}

async function startGame() {
  const phone = document.getElementById('inp-phone').value.trim();
  const token = document.getElementById('inp-token').value.trim();
  if (!phone) { setStatus('Phone number required.', 'err'); return; }
  if (!token) { setStatus('Game token required.', 'err'); return; }

  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  btn.textContent = 'INITIATING...';
  setStatus('Contacting game server...');

  try {
    const res = await fetch('/game/start', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone, token})
    });
    const data = await res.json();
    if (!res.ok) {
      setStatus(data.detail || 'Failed to start.', 'err');
      btn.disabled = false; btn.textContent = '&#9654; INITIATE GAME';
      return;
    }
    gameId = data.game_id;
    showDashboard();
    startPolling();
  } catch(e) {
    setStatus('Network error: '+e.message, 'err');
    btn.disabled = false; btn.textContent = '&#9654; INITIATE GAME';
  }
}

function watchGame() {
  const id = document.getElementById('inp-watch').value.trim();
  if (!id) return;
  gameId = id;
  showDashboard();
  startPolling();
}

function showDashboard() {
  document.getElementById('launcher').style.display = 'none';
  const dash = document.getElementById('dashboard');
  dash.className = 'on';
  document.getElementById('gid-copy').textContent = gameId;
  document.getElementById('gid-live').textContent = gameId;
}

function resetToLauncher() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  gameId = null;
  document.getElementById('launcher').style.display = 'block';
  document.getElementById('dashboard').className = '';
  document.getElementById('view-wait').style.display = 'block';
  document.getElementById('view-live').style.display = 'none';
  document.getElementById('gameover-banner').style.display = 'none';
  document.getElementById('players-grid').innerHTML = '';
  document.getElementById('btn-start').disabled = false;
  document.getElementById('btn-start').textContent = '&#9654; INITIATE GAME';
  setStatus('');
  crews.forEach(c => c.dead = false);
}

function copyId() {
  if (gameId) navigator.clipboard.writeText(gameId).catch(()=>{});
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  doPoll();
  pollTimer = setInterval(doPoll, 3000);
}

async function doPoll() {
  if (!gameId) return;
  try {
    const res = await fetch('/api/game/'+gameId);
    if (!res.ok) return;
    const d = await res.json();
    applyState(d);
  } catch(_) {}
}

function applyState(state) {
  const phase = state.phase;
  const players = state.players || [];
  const isOver = phase === 'GAME_OVER';

  // Switch to live view if we have players
  if (players.length > 0) {
    document.getElementById('view-wait').style.display = 'none';
    document.getElementById('view-live').style.display = 'block';
  }

  // Phase badge
  const badge = document.getElementById('phase-badge');
  badge.textContent = PHASE_LABELS[phase] || phase;
  badge.className = 'phase-badge ph-'+phase;

  // Round
  document.getElementById('round-num').textContent = state.round_num || '—';

  // Game over banner
  if (isOver) {
    const impostersAlive = players.filter(p => p.is_alive && p.role === 'imposter').length;
    const banner = document.getElementById('gameover-banner');
    const title = document.getElementById('gameover-title');
    const sub = document.getElementById('gameover-sub');
    banner.style.display = 'block';
    if (impostersAlive === 0) {
      title.textContent = 'CREWMATES WIN';
      title.style.color = 'var(--accent)';
      sub.textContent = 'All imposters ejected. The crew prevails.';
    } else {
      title.textContent = 'IMPOSTERS WIN';
      title.style.color = 'var(--red)';
      sub.textContent = 'Imposters overwhelm the crew. Mission failed.';
    }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Players grid
  const grid = document.getElementById('players-grid');
  if (players.length > 0) {
    grid.innerHTML = players.map((p, i) => {
      const alive = p.is_alive;
      const filter = CREW_CSS_FILTERS[i % CREW_CSS_FILTERS.length];
      const roleHtml = isOver
        ? `<div class="prole ${p.role==='imposter'?'r-imposter':'r-crewmate'}">${p.role==='imposter'?'IMPOSTER':'CREW'}</div>`
        : '';
      return `<div class="pcard ${alive?'alive':'dead'}">
        <img class="pavatar" src="/static/single_crew.png" style="filter:${filter}" alt="${p.name}" onerror="this.style.display='none'">
        <div class="pname">${p.name}</div>
        <div class="pstatus">${alive?'ALIVE':'EJECTED'}</div>
        ${roleHtml}
      </div>`;
    }).join('');

    // Sync background crewmate deaths
    players.forEach((p, i) => {
      if (crews[i]) crews[i].dead = !p.is_alive;
    });
  }

  document.title = 'RogueAI — ' + (PHASE_LABELS[phase] || phase);
}

// Enter key support
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !gameId) startGame();
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(_HOME_HTML)

