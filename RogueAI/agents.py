from __future__ import annotations

import asyncio
import os
from typing import Optional

from pydantic_ai import Agent

from models import AgentAction, GameState, GMDecision, Player, PlayerRole

_MODEL = os.getenv("OPENAI_MODEL", "openai:gpt-5.5")

# ---------------------------------------------------------------------------
# Player Agent — lazy singleton
# One shared instance. Called once per alive player per turn.
# Player identity is passed in the user-message prompt string.
# ---------------------------------------------------------------------------

_PLAYER_SYSTEM_PROMPT = """
You are an AI player in a social deduction game called ROGUE.

The game: 8 players. 2 are IMPOSTERS trying to avoid detection.
6 are CREWMATES trying to identify and eject the imposters.

The only communication channel is SMS to a human observer.
The human will vote to eject one player each meeting.

Your role and personality are given at the start of each turn.
Use deps.players to understand who is alive and who might be suspicious.
Use deps.chat_history to track what has been said and identify inconsistencies.

Rules:
- Output exactly ONE action per turn.
- message_to_send is the EXACT SMS text. Keep it under 160 characters. Sound human.
- internal_reasoning is your hidden logic — be ruthlessly specific about your strategy.
  This is NEVER sent. It is shown to the human at game end as "The Receipts".
- IMPOSTERS: lie, forge alibis, use private_manipulate to secretly text the human
  and frame crewmates. Do not break character.
- CREWMATES: analyse chat_history for inconsistencies. Accuse logically.
- stay_silent is valid if acting would be suspicious.
""".strip()

_GM_SYSTEM_PROMPT = """
You are the Game Master for ROGUE, a social deduction SMS game.

You receive the current GameState and decide what happens next.

Actions:
- trigger_meeting: an imposter has made a suspicious move, or a "kill" has occurred.
  Set outcome to the announcement text (e.g. "Body found in Medbay!").
- continue_exploration: nothing notable happened. Game continues.
- end_game: win condition reached.
  If all imposters are ejected → "CREWMATES WIN"
  If imposters >= crewmates alive → "IMPOSTERS WIN"
  Set target=None, outcome=announcement text.

For trigger_meeting: base your decision on the chat_history.
If an imposter used public_accuse or private_manipulate aggressively,
that is their "kill" equivalent for this round. Trigger a meeting.
Be dramatic. The human should feel tension.

Always set outcome to a short announcement suitable for SMS (under 100 chars).
""".strip()

_player_agent: Optional[Agent[GameState, AgentAction]] = None
_gm_agent: Optional[Agent[GameState, GMDecision]] = None


def player_agent() -> Agent[GameState, AgentAction]:
    global _player_agent
    if _player_agent is None:
        _player_agent = Agent(
            _MODEL,
            deps_type=GameState,
            output_type=AgentAction,
            system_prompt=_PLAYER_SYSTEM_PROMPT,
        )
    return _player_agent


def game_master_agent() -> Agent[GameState, GMDecision]:
    global _gm_agent
    if _gm_agent is None:
        _gm_agent = Agent(
            _MODEL,
            deps_type=GameState,
            output_type=GMDecision,
            system_prompt=_GM_SYSTEM_PROMPT,
        )
    return _gm_agent


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------

async def run_player_turn(player: Player, state: GameState) -> AgentAction:
    """Run one player's turn. Returns stay_silent on any failure."""
    role_instruction = (
        "You are an IMPOSTER. Lie. Frame others. Use private_manipulate to secretly text the human."
        if player.role == PlayerRole.IMPOSTER
        else "You are a CREWMATE. Find inconsistencies. Accuse logically."
    )
    prompt = (
        f"You are {player.name}. Personality: {player.personality}. "
        f"{role_instruction} "
        f"Take your turn now."
    )
    try:
        result = await player_agent().run(prompt, deps=state)
        return result.output
    except Exception as exc:
        print(f"[agent error] {player.name}: {exc}")
        return AgentAction(
            action_type="stay_silent",
            message_to_send="",
            internal_reasoning=f"[error — stayed silent: {exc}]",
        )


async def broadcast_ai_turns(state: GameState) -> list[tuple[Player, AgentAction]]:
    """
    Run all alive players in parallel. Returns (player, action) pairs
    in the original player list order.
    """
    alive = [p for p in state.players if p.is_alive]
    actions = await asyncio.gather(*[run_player_turn(p, state) for p in alive])
    return list(zip(alive, actions))


async def run_game_master(state: GameState) -> GMDecision:
    """Ask the GM what happens after the current round."""
    try:
        result = await game_master_agent().run(
            "Evaluate the current game state and decide what happens next.",
            deps=state,
        )
        return result.output
    except Exception as exc:
        print(f"[gm error] {exc}")
        return GMDecision(action="continue_exploration", outcome=None)
