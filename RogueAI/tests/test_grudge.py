"""Tests for The Grudge long-term memory system."""
from __future__ import annotations

import sys
import os
from pathlib import Path

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from models import PlayerHistory


# ---------------------------------------------------------------------------
# Helpers — import grudge functions with GRUDGE_DIR patched via env
# ---------------------------------------------------------------------------

def _import_main_with_grudge_dir(tmp_path):
    """Re-import main with GRUDGE_DIR pointing at tmp_path."""
    os.environ["GRUDGE_DIR"] = str(tmp_path)
    # Remove cached module so env var is re-read
    import importlib
    import main as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_history_new_phone(tmp_path):
    m = _import_main_with_grudge_dir(tmp_path)
    h = m._load_history("+15551234567")
    assert h.games_played == 0
    assert h.votes_cast == []
    assert h.crewmates_wins == 0


def test_load_history_existing(tmp_path):
    m = _import_main_with_grudge_dir(tmp_path)
    data = PlayerHistory(games_played=2, votes_cast=["Alex", "Jordan"], crewmates_wins=1)
    (tmp_path / "15551234567.json").write_text(data.model_dump_json())
    h = m._load_history("+15551234567")
    assert h.games_played == 2
    assert "Alex" in h.votes_cast
    assert h.crewmates_wins == 1


def test_load_history_corrupted_json(tmp_path):
    m = _import_main_with_grudge_dir(tmp_path)
    (tmp_path / "15551234567.json").write_text("not json {{{{")
    h = m._load_history("+15551234567")
    # Must return empty history, not raise
    assert h.games_played == 0


def test_save_history_new_file(tmp_path, monkeypatch):
    m = _import_main_with_grudge_dir(tmp_path)
    from unittest.mock import MagicMock
    state = MagicMock()
    state.player_history = PlayerHistory()
    state.voted_name = "Jordan"
    m._save_history("+15551234567", state, "CREWMATES WIN. All imposters ejected.")
    saved = PlayerHistory.model_validate_json((tmp_path / "15551234567.json").read_text())
    assert saved.games_played == 1
    assert "Jordan" in saved.votes_cast
    assert saved.crewmates_wins == 1


def test_save_history_votes_capped_at_10(tmp_path):
    m = _import_main_with_grudge_dir(tmp_path)
    from unittest.mock import MagicMock
    # Pre-load 10 existing votes
    existing = PlayerHistory(
        games_played=10,
        votes_cast=["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"],
        crewmates_wins=5,
    )
    (tmp_path / "15551234567.json").write_text(existing.model_dump_json())
    state = MagicMock()
    state.player_history = m._load_history("+15551234567")
    state.voted_name = "NewPlayer"
    m._save_history("+15551234567", state, "IMPOSTERS WIN.")
    saved = PlayerHistory.model_validate_json((tmp_path / "15551234567.json").read_text())
    assert len(saved.votes_cast) == 10
    assert saved.votes_cast[-1] == "NewPlayer"
    assert "P1" not in saved.votes_cast  # oldest dropped


def test_grudge_context_injected_into_prompt(tmp_path, monkeypatch):
    """Verify grudge text appears in prompt when history exists."""
    from models import GameState, Player, PlayerRole, PlayerHistory, AgentAction
    import uuid
    import agents as ag

    _import_main_with_grudge_dir(tmp_path)

    captured_prompts = []

    async def fake_run(self, prompt, *, deps):
        captured_prompts.append(prompt)
        return type("R", (), {"output": AgentAction(
            action_type="stay_silent",
            message_to_send="",
            internal_reasoning="test",
        )})()

    fake_agent = type("FakeAgent", (), {"run": fake_run})()
    monkeypatch.setattr(ag, "player_agent", lambda: fake_agent)

    history = PlayerHistory(games_played=3, votes_cast=["Alex", "Jordan"], crewmates_wins=2)
    state = GameState(
        game_id=str(uuid.uuid4()),
        human_phone_number="+15551234567",
        system_phone_number="+15550000000",
        players=[Player(name="Alex", saperly_number="+15550001", role=PlayerRole.CREWMATE, personality="bold")],
        player_history=history,
    )

    import asyncio
    asyncio.run(ag.run_player_turn(state.players[0], state))

    assert captured_prompts, "player_agent().run was never called"
    assert "3 game" in captured_prompts[0]
    assert "Alex" in captured_prompts[0] or "Jordan" in captured_prompts[0]
