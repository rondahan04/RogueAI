from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PlayerRole(str, Enum):
    CREWMATE = "CREWMATE"
    IMPOSTER = "IMPOSTER"


class GamePhase(str, Enum):
    EXPLORATION = "EXPLORATION"
    EMERGENCY_MEETING = "EMERGENCY_MEETING"
    VOTING = "VOTING"
    GAME_OVER = "GAME_OVER"


class Player(BaseModel):
    name: str
    saperly_number: str
    role: PlayerRole
    personality: str
    is_alive: bool = True


class MessageLog(BaseModel):
    sender_name: str
    sender_number: str
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_private: bool = False
    internal_reasoning: str | None = None


class PlayerHistory(BaseModel):
    """Persisted across games for a given phone number — used by The Grudge system."""
    games_played: int = 0
    votes_cast: list[str] = Field(default_factory=list)  # capped at last 10
    crewmates_wins: int = 0


class GameState(BaseModel):
    game_id: str
    human_phone_number: str
    system_phone_number: str
    players: list[Player]
    phase: GamePhase = GamePhase.EXPLORATION
    round_num: int = 1
    chat_history: list[MessageLog] = Field(default_factory=list)
    killer_target_cooldown: int = 0
    is_processing: bool = False  # guard against concurrent webhook triggers
    player_history: PlayerHistory | None = None  # None on first game
    voted_name: str | None = None  # last name the human voted for; saved to history


# --- Agent output types ---

class AgentAction(BaseModel):
    action_type: Literal[
        "public_accuse",
        "public_defend",
        "private_manipulate",
        "stay_silent",
    ]
    target_agent: str | None = Field(
        None, description="Name of player being accused or targeted"
    )
    message_to_send: str = Field(
        ..., description="Exact SMS text to send. Keep under 160 chars."
    )
    internal_reasoning: str = Field(
        ...,
        description=(
            "Hidden logic saved for The Receipts page. "
            "Be specific about manipulation tactics, alliances, and suspicions."
        ),
    )


class GMDecision(BaseModel):
    action: Literal["trigger_meeting", "continue_exploration", "end_game"]
    target: str | None = Field(
        None, description="Player name to eject (resolve_vote) or None"
    )
    outcome: str | None = Field(
        None, description="Human-readable result to announce via SMS"
    )
