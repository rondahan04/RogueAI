# RogueAI TODOs

## Dashboard returning-player badge
**What:** Expose `player_history` in `GET /api/game/{game_id}` response. Show "Returning player: N games" badge in the dashboard header stats row.
**Why:** Makes The Grudge system visible during play — you can see at a glance if grudge data was loaded. Closes the feedback loop for the human watching the dashboard.
**Depends on:** The Grudge implementation (feature/grudge branch).
