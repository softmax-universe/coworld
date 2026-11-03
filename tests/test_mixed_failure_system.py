"""Preserve file-player fault attribution through a real mixed Kubernetes episode.

COWORLD_MIXED_FAILURE_SYSTEM_CONFIG supplies coordinator_image, job (the public
CoworldEpisodeJobSpec), player_files, and failed_policy_index. Use local images
and a game/file fixture that declares a terminal player failure.
"""

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from kubernetes import client, config
from pydantic import BaseModel

from coworld.runner.io import RunnerError
from coworld.runner.llm_metadata import CoworldEpisodeLlmMetadata
from coworld.types import CoworldEpisodeJobSpec, CoworldPlayerFileSpec, CoworldRunnableSpec


class MixedFailureProof(BaseModel):
    coordinator_image: str
    job: CoworldEpisodeJobSpec
    player_files: list[Path]
    failed_policy_index: int


def test_file_failure_keeps_its_seat_in_a_mixed_hosted_episode(tmp_path: Path) -> None:
    configured = os.environ.get("COWORLD_MIXED_FAILURE_SYSTEM_CONFIG")
    if configured is None:
        pytest.skip("Requires local mixed-game images and a terminal file-player fault fixture")
    proof = MixedFailureProof.model_validate_json(Path(configured).read_bytes())
    assert isinstance(proof.job.players[proof.failed_policy_index], CoworldPlayerFileSpec)
    assert any(isinstance(player, CoworldRunnableSpec) for player in proof.job.players)
    assert len(proof.player_files) == sum(isinstance(player, CoworldPlayerFileSpec) for player in proof.job.players)
    namespace = f"jobs-mixed-failure-{uuid4().hex[:12]}"
    with config.new_client_from_config(context="orbstack") as cluster:
        core = client.CoreV1Api(cluster)
        rbac = client.RbacAuthorizationV1Api(cluster)
        core.create_namespace({"metadata": {"name": namespace}})
        try:
            core.create_namespaced_service_account(namespace, {"metadata": {"name": "episode-runner"}})
            rbac.create_namespaced_role(
                namespace,
                {
                    "metadata": {"name": "episode-runner"},
                    "rules": [
                        {
                            "apiGroups": [""],
                            "resources": ["pods", "pods/log", "services"],
                            "verbs": ["create", "get", "list", "delete"],
                        }
                    ],
                },
            )
            rbac.create_namespaced_role_binding(
                namespace,
                {
                    "metadata": {"name": "episode-runner"},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "episode-runner"},
                    "subjects": [{"kind": "ServiceAccount", "name": "episode-runner", "namespace": namespace}],
                },
            )
            core.create_namespaced_config_map(
                namespace,
                {
                    "metadata": {"name": "inputs"},
                    "data": {"job.json": proof.job.model_dump_json()},
                    "binaryData": {
                        f"player-{slot}": base64.b64encode(path.read_bytes()).decode()
                        for slot, path in enumerate(proof.player_files)
                    },
                },
            )
            values = {
                "JOB_SPEC_URI": "file:///inputs/job.json",
                "COGAME_CONFIG_URI": "file:///coworld/config.json",
                "ERROR_INFO_URI": "file:///coworld/error-info.json",
                "DEBUG_URI": "file:///coworld/debug.zip",
                "JOB_NAMESPACE": namespace,
                "JOB_ID": namespace,
                "COWORLD_SERVICE_NAME": "episode-game",
                "COWORLD_COORDINATOR_IMAGE": proof.coordinator_image,
                "COWORLD_PLAYER_IMAGE_PULL_POLICY": "IfNotPresent",
                "COWORLD_TIMEOUT_SECONDS": "120",
                "COWORLD_CHILD_POD_LIFETIME_SECONDS": "180",
                "COWORLD_PLAYER_CPU_REQUEST": "100m",
                "COWORLD_PLAYER_MEMORY_REQUEST": "128Mi",
                "LLM_REQUEST_METADATA": CoworldEpisodeLlmMetadata(
                    schema_version="1",
                    source="coworld_episode",
                    metadata_origin="dispatcher",
                    job_request_id=uuid4(),
                    role="game",
                    slot="game",
                    image_digest=proof.job.manifest.game.runnable.image,
                ).model_dump_json(),
                "PLAYER_FILE_URLS": json.dumps(
                    {slot: f"file:///inputs/player-{slot}" for slot in range(len(proof.player_files))}
                ),
            }
            env = [{"name": name, "value": value} for name, value in values.items()]
            env += [
                {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
                for name, field in (("POD_NAME", "metadata.name"), ("POD_UID", "metadata.uid"))
            ]
            mounts = [
                {"name": name, "mountPath": path}
                for name, path in (
                    ("work", "/coworld"),
                    ("private", "/var/run/coworld-coordinator"),
                    ("inputs", "/inputs"),
                )
            ]
            worker = {
                "name": "worker",
                "command": ["python", "-m", "coworld.runner.kubernetes_runner", "run-core-sidecars-v1"],
                "image": proof.coordinator_image,
                "imagePullPolicy": "IfNotPresent",
                "env": env,
                "volumeMounts": mounts,
            }
            game = proof.job.manifest.game.runnable
            game_env = dict(game.env) | {
                "COGAME_CONFIG_URI": "file:///coworld/config.json",
                "COGAME_PLAYER_SEATS_URI": "file:///coworld/player_seats.json",
                "COGAME_RESULTS_URI": "file:///coworld/results.json",
                "COGAME_SAVE_REPLAY_URI": "file:///coworld/replay",
                "COGAME_PLAYER_FAILURE_URI": "file:///coworld/player_failure.json",
            }
            pod = {
                "metadata": {"name": "episode", "labels": {"job-id": namespace, "coworld-component": "game"}},
                "spec": {
                    "serviceAccountName": "episode-runner",
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": 180,
                    "volumes": [
                        {"name": "work", "emptyDir": {}},
                        {"name": "private", "emptyDir": {}},
                        {"name": "inputs", "configMap": {"name": "inputs"}},
                    ],
                    "initContainers": [
                        worker | {"name": "init-config", "command": ["python", "-m", "coworld.runner.init_config"]},
                        worker
                        | {
                            "name": "launch-players",
                            "command": ["python", "-m", "coworld.runner.kubernetes_runner", "prepare-players"],
                        },
                    ],
                    "containers": [
                        worker,
                        {
                            "name": "evidence",
                            "image": proof.coordinator_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-c", "import threading; threading.Event().wait(180)"],
                            "volumeMounts": mounts[:1],
                        },
                        {
                            "name": "game",
                            "image": game.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": game.run,
                            "env": [{"name": name, "value": value} for name, value in game_env.items()],
                            "volumeMounts": mounts[:1],
                        },
                    ],
                },
            }
            core.create_namespaced_pod(namespace, pod)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current = core.read_namespaced_pod("episode", namespace)
                (tmp_path / "pod.json").write_text(json.dumps(cluster.sanitize_for_serialization(current)))
                failed_init = [
                    status.name
                    for status in current.status.init_container_statuses or []
                    if status.state.terminated and status.state.terminated.exit_code != 0
                ]
                for name in failed_init:
                    (tmp_path / f"{name}.log").write_text(
                        core.read_namespaced_pod_log("episode", namespace, container=name)
                    )
                assert not failed_init, f"Failed episode initialization: {failed_init}; inspect {tmp_path}"
                if any(
                    status.name == "worker" and status.state.terminated
                    for status in current.status.container_statuses or []
                ):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(f"Mixed episode did not publish its verdict; inspect {tmp_path / 'pod.json'}")
            for name in ("init-config", "launch-players", "worker", "game"):
                (tmp_path / f"{name}.log").write_text(
                    core.read_namespaced_pod_log("episode", namespace, container=name)
                )
            raw = subprocess.check_output(
                [
                    "kubectl",
                    "--context",
                    "orbstack",
                    "exec",
                    "-n",
                    namespace,
                    "episode",
                    "-c",
                    "evidence",
                    "--",
                    "cat",
                    "/coworld/error-info.json",
                ],
                text=True,
                timeout=30,
            )
            (tmp_path / "error-info.json").write_text(raw)
            verdict = RunnerError.model_validate_json(raw)
            assert verdict.error_type == "player_error", verdict
            assert verdict.failed_policy_index == proof.failed_policy_index, verdict
            cleanup_deadline = time.monotonic() + 45
            while core.list_namespaced_pod(namespace, label_selector="coworld-component=player").items:
                assert time.monotonic() < cleanup_deadline, "Coordinator left player pods after its terminal verdict"
                time.sleep(0.5)
        finally:
            core.delete_namespace(namespace)
