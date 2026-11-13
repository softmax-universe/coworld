from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import websockets
from run_stats import RunRecorder


def choose_action(observation: dict[str, Any]) -> dict[str, str]:
    if observation["type"] == "observation":
        return {"type": "action", "action": "noop"}
    raise ValueError(f"Unsupported observation type: {observation['type']}")


def observation_score(observation: dict[str, Any]) -> float | None:
    """Pull a running score/reward out of an observation for the charts page. Adapt to your game's protocol."""
    for key in ("score", "reward"):
        if isinstance(observation.get(key), (int, float)):
            return observation[key]
    return None


async def run(ws_url: str) -> None:
    recorder = RunRecorder()  # writes player_runs/runs.jsonl and player_runs/report.html (open it in a browser)
    # ping_timeout=None protects previously deployed games that don't answer
    # WebSocket Ping frames; certification now requires new games to answer.
    # Keepalive pings are still sent without closing on a missing Pong.
    try:
        async with websockets.connect(ws_url, ping_timeout=None) as websocket:
            async for message in websocket:
                observation = json.loads(message)
                action = choose_action(observation)
                try:
                    recorder.step(action.get("action"), observation_score(observation))
                except Exception as error:  # noqa: BLE001 - stats must never break play
                    print(f"run_stats: step skipped: {error}", file=sys.stderr)
                await websocket.send(json.dumps(action))
    finally:
        recorder.finish()  # always record the episode, even if the connection drops; pass won=True/False if known


def main() -> None:
    asyncio.run(run(os.environ["COWORLD_PLAYER_WS_URL"]))


if __name__ == "__main__":
    main()
