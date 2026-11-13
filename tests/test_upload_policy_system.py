"""Real local upload regression: image archives must not require whole-image staging.

Run with METTA_COWORLD_UPLOAD_SYSTEM_SERVER pointing at a local frontend proxy,
METTA_COWORLD_UPLOAD_SYSTEM_BASE_IMAGE naming a cached Alpine image, and a local
USER debug credential in SOFTMAX_CONFIG_DIR. This creates an isolated local policy.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from coworld.api_client import CoworldApiClient


def test_upload_policy_with_file_limit_smaller_than_image(tmp_path: Path) -> None:
    server = os.environ.get("METTA_COWORLD_UPLOAD_SYSTEM_SERVER")
    base_image = os.environ.get("METTA_COWORLD_UPLOAD_SYSTEM_BASE_IMAGE")
    if not server or not base_image:
        pytest.skip("Requires a real local stack and cached Alpine base image")
    assert urlparse(server).hostname in {"localhost", "127.0.0.1"}
    name = f"upload-stream-system-{uuid4().hex[:12]}"
    image = f"{name}:local"
    for filename in ("first", "second"):
        (tmp_path / filename).write_bytes(os.urandom(4 * 1024 * 1024))
    (tmp_path / "Dockerfile").write_text(
        f"FROM {base_image} AS base\n"
        "FROM scratch\n"
        "COPY --from=base /bin/busybox /bin/busybox\n"
        "COPY --from=base /lib /lib\n"
        "COPY first /first\n"
        "COPY second /second\n"
        'ENTRYPOINT ["/bin/busybox", "echo", "upload-system-proof"]\n'
    )
    subprocess.run(
        ["docker", "build", "--platform=linux/amd64", "--pull=false", "-t", image, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    try:
        # Kernel-enforced per-file limit applies only to the CLI, not the Docker daemon.
        # Each layer fits; the image archive exceeds the limit even without tar headers.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import resource; resource.setrlimit(resource.RLIMIT_FSIZE, (6*1024*1024, 6*1024*1024)); "
                "from coworld.cli import app; app()",
                "upload-policy",
                image,
                "--name",
                name,
                "--server",
                server,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        (tmp_path / "upload.stdout").write_text(result.stdout)
        (tmp_path / "upload.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Upload complete: {name}:v1" in result.stdout
        with CoworldApiClient.from_login(server_url=server) as client:
            policy = client.lookup_policy_version(name=name, version=1)
        assert policy is not None
        assert policy.name == name
    finally:
        subprocess.run(["docker", "image", "rm", image], check=True, capture_output=True)


def test_file_policy_pull_preserves_edits_and_exact_version(tmp_path: Path) -> None:
    server = os.environ.get("METTA_COWORLD_UPLOAD_SYSTEM_SERVER")
    player = os.environ.get("METTA_COWORLD_UPLOAD_SYSTEM_PLAYER")
    if not server or not player:
        pytest.skip("Requires a real local stack and an owned local player")
    assert urlparse(server).hostname in {"localhost", "127.0.0.1"}
    name = f"pull-source-system-{uuid4().hex[:12]}"
    source = tmp_path / "player"
    source.mkdir()
    for index in range(240):
        (source / f"part-{index:03}.py").write_text(f"value = {index}\n")
    command = [sys.executable, "-c", f"import sys; sys.path[:] = {sys.path!r}; from coworld.cli import app; app()"]
    upload = [
        *command,
        "upload-policy",
        "--file",
        str(source),
        "--name",
        name,
        "--player",
        player,
        "--attributes",
        '{"language":"python"}',
        "--tag",
        "purpose=source-system",
        "--json",
        "--server",
        server,
    ]
    first = subprocess.run(upload, check=True, capture_output=True, text=True, timeout=120)
    receipt = json.loads(first.stdout)
    repeated = subprocess.run(upload, check=True, capture_output=True, text=True, timeout=120)
    assert json.loads(repeated.stdout)["id"] == receipt["id"]
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(tmp_path, target_is_directory=True)
    working = parent_alias / "working"
    pull = [*command, "pull-policy", f"{name}:v1", "--output-dir", str(working), "--json", "--server", server]
    result = subprocess.run(pull, check=True, capture_output=True, text=True, timeout=120)
    assert json.loads(result.stdout)["policy_version_id"] == receipt["id"]
    assert len(list(working.glob("*.py"))) == 240
    edited = working / "part-239.py"
    edited.write_text("local edit\n")
    reopened = subprocess.run(pull, check=True, capture_output=True, text=True, timeout=120)
    assert json.loads(reopened.stdout)["state"] == "already_open"
    assert edited.read_text() == "local edit\n"
    destination_link = tmp_path / "destination-link"
    destination_link.symlink_to(working, target_is_directory=True)
    linked_pull = [
        *command,
        "pull-policy",
        receipt["id"],
        "--output-dir",
        str(destination_link),
        "--json",
        "--server",
        server,
    ]
    assert subprocess.run(linked_pull, capture_output=True, text=True, timeout=120).returncode != 0
    assert edited.read_text() == "local edit\n"
    original = tmp_path / "original"
    clean = [*command, "pull-policy", receipt["id"], "--output-dir", str(original), "--json", "--server", server]
    subprocess.run(clean, check=True, capture_output=True, text=True, timeout=120)
    assert (original / "part-239.py").read_text() == "value = 239\n"
    revised_upload = [str(working) if value == str(source) else value for value in upload]
    revised = subprocess.run(revised_upload, check=True, capture_output=True, text=True, timeout=120)
    revised_receipt = json.loads(revised.stdout)
    assert revised_receipt["version"] == 2
    published_edit = tmp_path / "published-edit"
    subprocess.run(
        [
            *command,
            "pull-policy",
            revised_receipt["id"],
            "--output-dir",
            str(published_edit),
            "--json",
            "--server",
            server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (published_edit / "part-239.py").read_text() == "local edit\n"
    conflict = [*command, "pull-policy", receipt["id"], "--output-dir", str(source), "--json", "--server", server]
    assert subprocess.run(conflict, capture_output=True, text=True, timeout=120).returncode != 0
    missing = [*command, "pull-policy", f"{name}-unknown:v1", "--json", "--server", server]
    assert subprocess.run(missing, capture_output=True, text=True, timeout=120).returncode != 0
