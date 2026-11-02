# Player Template

This scaffold demonstrates `platform-hosted` players. Authors may instead choose `game-hosted` file players; run
`coworld docs --local PLAYER_RUNTIMES.md` before adopting the scaffold. Game-hosted execution requires implementing the
game's file interface and per-seat output contract.

The player connects to the game-owned `/player` WebSocket URL provided by `COWORLD_PLAYER_WS_URL`, receives
game-specific observations, sends game-specific actions, and exits when the episode ends.

Keep `ping_timeout=None` on the `websockets.connect` call: certified game engines answer WebSocket Ping frames, but
previously deployed engines may not, and the client's default pong timeout would silently drop the connection ~40 s into
a hosted episode — long after the short local smoke episode has already passed. Keepalive pings are still sent; the
client just never kills the connection over a missing pong.

Contract reference: `coworld docs --local roles/PLAYER.md`.

Files:

- `player.py` - async WebSocket player loop scaffold.
- `run_stats.py` - dependency-free recorder; after each episode it appends to `player_runs/runs.jsonl` and rewrites
  `player_runs/report.html`, a static page of charts for your player (score and rolling win rate per episode, steps,
  action mix, latest reward curve). Open the file in a browser after a local run, or rebuild it with
  `python run_stats.py`. Adapt `observation_score` in `player.py` to your game's protocol and pass `won=` to
  `recorder.finish()` to get the win-rate chart. Set `COWORLD_PLAYER_STATS_DIR` to change the output directory. The
  charts are written next to the process that runs `player.py`, so run it directly
  (`COWORLD_PLAYER_WS_URL=... python player.py`) to keep them; inside a `coworld play` or `run-episode` container the
  directory is discarded with the container unless you mount it. Recording is best effort: a failure is logged to stderr
  and never stops the player.
- `Dockerfile` - minimal image shape for packaging the player runnable.
