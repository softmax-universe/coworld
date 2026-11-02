"""Complete five-seat games through independent routing and native model adapters.

PARLEY_BATCH_SYSTEM_CONFIG supplies game_binary, game_directory, manifest,
models (five local BatchModel endpoints), and optionally games (default two).
One game pauses before player registration; a ready peer must advance before
it resumes. Use already-running batch workers. The game's certification fixture
verifies infrastructure only; it does not qualify training data or GPU performance.
"""

import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, Field

from coworld.examples.parley.evidence import read_capture_archive
from coworld.runner.batch_coordinator import InferenceLog
from coworld.runner.gpu_batch import BatchGames, BatchModel, BatchPlan, batch_token
from coworld.runner.inference_pool import InferenceLease, InferenceSnapshot


class Settings(BaseModel):
    game_binary: Path
    game_directory: Path
    manifest: Path
    models: list[BatchModel] = Field(min_length=5, max_length=5)
    games: int = Field(default=2, ge=2, le=100)
    decoding: Literal["model", "legal-actions"] = "model"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    game_ids: list[str] = Field(default_factory=list)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _ready(endpoint: str) -> None:
    with httpx.Client(transport=httpx.HTTPTransport(retries=5), timeout=5, trust_env=False) as client:
        client.get(endpoint + "/healthz").raise_for_status()


@pytest.mark.skipif("PARLEY_BATCH_SYSTEM_CONFIG" not in os.environ, reason="requires Parley and local batch workers")
def test_independent_games_keep_their_own_decisions_and_complete_all_rounds(tmp_path: Path) -> None:
    settings = Settings.model_validate_json(Path(os.environ["PARLEY_BATCH_SYSTEM_CONFIG"]).read_text())
    assert all(urlsplit(model.endpoint).hostname in {"localhost", "127.0.0.1"} for model in settings.models)
    fixture = json.loads(settings.manifest.read_text())["certification"]["game_config"]
    assert len(fixture["players"]) == 5
    games = settings.game_ids or [str(uuid4()) for _ in range(settings.games)]
    live_games = tmp_path / "games.json"
    live_games.write_text(BatchGames(games=games).model_dump_json())
    secret = secrets.token_hex(32)
    timeout = int(fixture["episodeTimeoutSeconds"]) + 120
    port = _port()
    endpoint = f"http://127.0.0.1:{port}"
    processes: list[subprocess.Popen] = []
    gateways: list[subprocess.Popen] = []
    game_processes: list[subprocess.Popen] = []
    with ExitStack() as files:
        try:
            snapshot = tmp_path / "leases.json"
            snapshot.write_text(
                InferenceSnapshot(
                    revision=1,
                    expires_at=time.time() + timeout,
                    leases=[
                        InferenceLease(
                            request_id=settings.request_id,
                            user_id="parley-batch-system",
                            games=games,
                            expires_at=time.time() + timeout,
                        )
                    ],
                ).model_dump_json()
            )
            models = []
            for index, model in enumerate(settings.models):
                gateway_port = _port()
                gateway_endpoint = f"http://127.0.0.1:{gateway_port}"
                gateways.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "coworld.runner.inference_gateway"],
                        env={
                            **os.environ,
                            "COWORLD_INFERENCE_WORKER": f"model-{index}",
                            "COWORLD_INFERENCE_MODEL": model.model,
                            "COWORLD_INFERENCE_SECRET": secret,
                            "COWORLD_INFERENCE_SNAPSHOT": str(snapshot),
                            "COWORLD_INFERENCE_UPSTREAM": model.endpoint,
                            "COWORLD_INFERENCE_CAPACITY": str(len(games)),
                            "COWORLD_INFERENCE_PORT": str(gateway_port),
                        },
                        stdout=files.enter_context((tmp_path / f"gateway-{index}.log").open("w")),
                        stderr=subprocess.STDOUT,
                    )
                )
                _ready(gateway_endpoint)
                models.append(
                    BatchModel(
                        model=model.model,
                        endpoint=gateway_endpoint,
                        token=batch_token(secret, settings.request_id, "inference"),
                    )
                )
            plan = BatchPlan(
                games=games,
                seats=list(range(5)),
                models=models,
                timeout_seconds=timeout,
                request_id=settings.request_id,
            )
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "coworld.runner.batch_coordinator"],
                env={
                    **os.environ,
                    "COWORLD_GPU_BATCH_PLAN": plan.model_dump_json(),
                    "COWORLD_GPU_BATCH_SECRET": secret,
                    "COWORLD_GPU_BATCH_GAMES_FILE": str(live_games),
                    "COWORLD_GPU_BATCH_PORT": str(port),
                },
                stdout=files.enter_context((tmp_path / "coordinator.log").open("w")),
                stderr=subprocess.STDOUT,
            )
            processes.append(coordinator)
            _ready(endpoint)
            for index, game in enumerate(games):
                folder = tmp_path / game
                folder.mkdir()
                config = {**fixture, "seed": 17 + index, "tokens": [secrets.token_hex(16) for _ in range(5)]}
                (folder / "config.json").write_text(json.dumps(config))
                game_port = _port()
                process = subprocess.Popen(
                    [str(settings.game_binary)],
                    cwd=settings.game_directory,
                    env={
                        **os.environ,
                        "COGAME_HOST": "127.0.0.1",
                        "COGAME_PORT": str(game_port),
                        "COGAME_CONFIG_URI": (folder / "config.json").as_uri(),
                        "COGAME_RESULTS_URI": (folder / "results.json").as_uri(),
                        "COGAME_SAVE_REPLAY_URI": (folder / "replay.json").as_uri(),
                        "COGAME_SAVE_TRAJECTORY_URI": (folder / "trajectory.jsonl").as_uri(),
                        "COWORLD_XP_BATCH_URL": endpoint,
                        "COWORLD_XP_BATCH_GAME_ID": game,
                        "COWORLD_XP_BATCH_KEY": batch_token(secret, game, "game"),
                        "COWORLD_XP_FULL_MATCH": "true",
                    },
                    stdout=files.enter_context((folder / "game.log").open("w")),
                    stderr=subprocess.STDOUT,
                )
                processes.append(process)
                game_processes.append(process)
                _ready(f"http://127.0.0.1:{game_port}")
                if index == 0:
                    # Hold this game before its players can register. Its peers
                    # must reach inference without a cross-game startup barrier.
                    process.send_signal(signal.SIGSTOP)
                for seat in range(5):
                    process = subprocess.Popen(
                        [sys.executable, "-m", "coworld.examples.parley.player"],
                        env={
                            **os.environ,
                            "COWORLD_PLAYER_WS_URL": f"ws://127.0.0.1:{game_port}/player?slot={seat}&token={config['tokens'][seat]}",
                            "PARLEY_ENDPOINT": endpoint,
                            "PARLEY_MODEL": settings.models[seat].model,
                            "PARLEY_OPERATOR_PROMPT": "Play legal actions and maximize your score.",
                            "PARLEY_DECODING": settings.decoding,
                            "COWORLD_XP_BATCH_GAME_ID": game,
                            "COWORLD_XP_BATCH_KEY": batch_token(secret, game, f"player:{seat}"),
                            "COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": (folder / f"player-{seat}.zip").as_uri(),
                        },
                        stdout=files.enter_context((folder / f"player-{seat}.log").open("w")),
                        stderr=subprocess.STDOUT,
                    )
                    processes.append(process)
            deadline = time.monotonic() + timeout
            paused = game_processes[0]
            try:
                assert paused.poll() is None
                while not any(
                    line.startswith("{") and InferenceLog.model_validate_json(line).game_id != games[0]
                    for line in (tmp_path / "coordinator.log").read_text().splitlines()
                ):
                    assert coordinator.poll() is None, (tmp_path / "coordinator.log").read_text()
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                assert paused.poll() is None
            finally:
                paused.send_signal(signal.SIGCONT)
            for process in game_processes:
                assert process.wait(timeout=max(0, deadline - time.monotonic())) == 0
            for process in processes:
                assert process.wait(timeout=max(0, deadline - time.monotonic())) == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
            for process in processes:
                process.wait(timeout=10)
            for gateway in gateways:
                if gateway.poll() is None:
                    gateway.terminate()
            for gateway in gateways:
                assert gateway.wait(timeout=20) in {0, -signal.SIGTERM}

    call_ids: set[str] = set()
    decision_count = 0
    for game in games:
        folder = tmp_path / game
        rows = [json.loads(line) for line in (folder / "trajectory.jsonl").read_text().splitlines()]
        assert rows[-1]["status"] == "completed"
        terminal = rows[-1]["outcome"]
        assert terminal["results"]["rounds"] == terminal["input_config"]["rounds"]
        assert len(json.loads((folder / "results.json").read_text())["scores"]) == 5
        assert (folder / "replay.json").stat().st_size > 0
        captures = {
            (capture.decision_id, capture.seat): capture
            for seat in range(5)
            for capture in read_capture_archive((folder / f"player-{seat}.zip").read_bytes())
        }
        assert len(captures) == sum(row["event_type"] == "decision" for row in rows)
        for row in rows:
            if row["event_type"] != "decision":
                continue
            decision_count += 1
            assert row["action_status"] == "accepted"
            attempt = row["attempts"][0]
            if settings.decoding == "legal-actions":
                assert (
                    attempt["request"]["response_format"]["json_schema"]["schema"] == row["observation"]["actionSchema"]
                )
            assert attempt["request"]["model"] == settings.models[int(row["seat"])].model
            assert attempt["request"]["messages"] == [
                {"role": "system", "content": row["observation"]["input"]["system"]},
                {"role": "user", "content": row["observation"]["input"]["user"]},
            ]
            capture = captures[(row["decision_id"], int(row["seat"]))]
            assert capture.raw_response == attempt["raw_response"]
            assert base64.b64decode(capture.response_body_b64).decode() == capture.raw_response
            identity = json.loads(capture.raw_response)["id"]
            assert identity not in call_ids
            call_ids.add(identity)
    requests = [
        InferenceLog.model_validate_json(line)
        for line in (tmp_path / "coordinator.log").read_text().splitlines()
        if line.startswith("{")
    ]
    assert len(requests) == decision_count
    assert {request.game_id for request in requests} == set(games)
