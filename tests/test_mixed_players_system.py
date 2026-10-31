"""Run a real mixed-policy game through manifest selection, staging, and Docker.

Set COWORLD_MIXED_SYSTEM_MANIFEST to a built four-seat mixed Coworld package.
Its certification fixture must interleave file and container players.
"""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coworld.certifier import build_manifest_episode_job_spec, episode_player_file_paths, load_coworld_package
from coworld.cli import app
from coworld.runner.io import PlayerRuntimeStatuses
from coworld.runner.runner import EpisodeArtifacts
from coworld.types import CoworldPlayerFileSpec, CoworldRunnableSpec


@pytest.mark.parametrize("file_only", [False, True])
def test_player_types_keep_their_seats_and_private_outputs(tmp_path: Path, file_only: bool) -> None:
    configured = os.environ.get("COWORLD_MIXED_SYSTEM_MANIFEST")
    if configured is None:
        pytest.skip("Requires a built mixed-policy game and Docker")
    package = load_coworld_package(Path(configured))
    job = build_manifest_episode_job_spec(package)
    if file_only:
        basic = episode_player_file_paths(package, job.players)[0]
        job = build_manifest_episode_job_spec(
            package, player_files_by_slot={slot: basic for slot in range(len(job.players))}
        )
    file_slots = {slot for slot, player in enumerate(job.players) if isinstance(player, CoworldPlayerFileSpec)}
    container_slots = {slot for slot, player in enumerate(job.players) if isinstance(player, CoworldRunnableSpec)}
    assert file_slots and 0 in file_slots
    assert bool(container_slots) is not file_only
    artifacts = EpisodeArtifacts.create(tmp_path / "episode")
    host_only = tmp_path / "host-only.txt"
    host_only.write_text("outside the game workspace")
    for name in ("player_status.json", "player_containers.json"):
        (artifacts.workspace / name).symlink_to(host_only)
    arguments = [
        "run-episode",
        str(package.manifest_path),
        "--output-dir",
        str(artifacts.workspace),
        "--timeout-seconds",
        "180",
        "--verify-replay",
    ]
    if file_only:
        for slot in range(len(job.players)):
            arguments.extend(["--player-file", f"{slot}={basic}"])
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert host_only.read_text() == "outside the game workspace"
    results = json.loads(artifacts.results_path.read_text())
    assert len(results["scores"]) == len(job.players)
    assert artifacts.replay_path.stat().st_size > 0
    for slot in file_slots | container_slots:
        assert artifacts.policy_log_path(slot).is_file()
    statuses = PlayerRuntimeStatuses.model_validate_json(artifacts.player_status_path.read_bytes())
    by_slot = {status.slot: status for status in statuses.players}
    assert all(by_slot[slot].exit_code == 0 for slot in file_slots | container_slots)
    containers = json.loads((artifacts.workspace / "player_containers.json").read_text())
    assert {receipt["slot"] for receipt in containers["players"]} == container_slots
