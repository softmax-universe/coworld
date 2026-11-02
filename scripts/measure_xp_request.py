"""Measure one pinned, private XP episode through authenticated download and game validation."""

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from coworld.api_client import CoworldApiClient, ExperienceRequestDetail
from softmax_client.auth import fetch_cogames_whoami
from softmax_client.credentials import load_current_token


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_ref: UUID


class Seat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player: Policy
    slot: int = Field(ge=0)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=5, max_length=200)
    private: Literal[True]
    coworld_id: str
    variant_id: str
    roster: list[Seat] = Field(min_length=1)
    num_episodes: Literal[1]
    game_config_overrides: dict[str, JsonValue]


class Receipt(BaseModel):
    server: str
    player: str
    body: Submission
    validator: list[str]
    started_at: datetime
    invocations: int = 1
    request_id: str | None = None
    outcome: Literal["observing", "completed", "failed", "timeout", "validation_failed", "validation_timeout"] = (
        "observing"
    )
    elapsed_seconds: float | None = None
    bundle_sha256: str | None = None
    download_completed_at: datetime | None = None
    validation_completed_at: datetime | None = None


class BundleFiles(BaseModel):
    results: str
    replay: str


class BundleManifest(BaseModel):
    ereq_id: str
    status: Literal["success"]
    files: BundleFiles


def save_receipt(path: Path, receipt: Receipt) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(receipt.model_dump_json(indent=2) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body", type=Path)
    parser.add_argument("--server", default="https://softmax.com/api")
    parser.add_argument("--player", required=True, help="Expected authenticated player ID.")
    parser.add_argument(
        "--output", required=True, type=Path, help="Private directory outside Git; one per observation."
    )
    parser.add_argument("--timeout-seconds", required=True, type=float, help="Maximum parent observation wait.")
    parser.add_argument("--validation-timeout-seconds", required=True, type=float)
    parser.add_argument(
        "--validator", required=True, help="Game validation command; the downloaded ZIP path is appended."
    )
    parser.add_argument(
        "--submit", action="store_true", help="Submit after deployment, qualification, and budget gates pass."
    )
    args = parser.parse_args()
    body = Submission.model_validate_json(args.body.read_text())
    if sorted(seat.slot for seat in body.roster) != list(range(len(body.roster))):
        raise ValueError("Pin every seat exactly once")
    if type(body.game_config_overrides["seed"]) is not int:
        raise ValueError("Pin an integer game seed")
    if args.timeout_seconds <= 0 or args.validation_timeout_seconds <= 0:
        raise ValueError("Timeouts must be positive")
    validator = shlex.split(args.validator)
    if not validator:
        raise ValueError("A game validator is required")
    if not args.submit:
        print(body.model_dump_json(indent=2))
        return

    token = load_current_token(server=args.server)
    if token is None:
        raise ValueError("Log in and select the expected player first")
    identity = fetch_cogames_whoami(api_server=args.server, token=token)
    if identity.subject_type != "player" or identity.subject_id != args.player:
        raise ValueError("Authenticated identity does not match --player")

    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_path = args.output / "receipt.json"
    if receipt_path.exists():
        receipt = Receipt.model_validate_json(receipt_path.read_text())
        if (receipt.server, receipt.player, receipt.body, receipt.validator) != (
            args.server,
            args.player,
            body,
            validator,
        ):
            raise ValueError("Output directory belongs to a different observation")
        if receipt.outcome == "completed":
            print(receipt.model_dump_json(indent=2))
            return
        receipt.invocations += 1
    else:
        receipt = Receipt(
            server=args.server, player=args.player, body=body, validator=validator, started_at=datetime.now(UTC)
        )
    # Persist the key and start before the POST, including when its response is lost.
    save_receipt(receipt_path, receipt)
    deadline = time.monotonic() + args.timeout_seconds
    with CoworldApiClient(server_url=args.server, token=token) as client:
        if receipt.request_id is None:
            created = client.create_experience_request(body.model_dump(mode="json"))
            receipt.request_id = created.id
            save_receipt(receipt_path, receipt)
        with (args.output / "observations.jsonl").open("a") as observations:
            while True:
                payload = client.get_bytes(f"/v2/experience-requests/{receipt.request_id}")
                detail = ExperienceRequestDetail.model_validate_json(payload)
                observations.write(
                    json.dumps({"observed_at": datetime.now(UTC).isoformat(), "response": json.loads(payload)}) + "\n"
                )
                observations.flush()
                if detail.status in {"completed", "failed", "cancelled"}:
                    break
                if time.monotonic() >= deadline:
                    receipt.outcome = "timeout"
                    receipt.elapsed_seconds = (datetime.now(UTC) - receipt.started_at).total_seconds()
                    save_receipt(receipt_path, receipt)
                    raise SystemExit(
                        "Observation timed out; accepted work remains bound. "
                        "Resume this directory or cancel through the API."
                    )
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if detail.status != "completed":
            receipt.outcome = "failed"
            receipt.elapsed_seconds = (datetime.now(UTC) - receipt.started_at).total_seconds()
            save_receipt(receipt_path, receipt)
            raise SystemExit(f"Request ended as {detail.status}; retain it in the failure analysis")
        if len(detail.episodes) != 1:
            raise ValueError("Expected exactly one child episode")
        episode = detail.episodes[0]
        bundle_path = args.output / "episode.zip"
        bundle_path.write_bytes(
            client.get_episode_request_bundle(
                episode.id, include=["results", "replay", "game_logs", "player_logs", "player_artifact"]
            )
        )
        receipt.download_completed_at = datetime.now(UTC)
        save_receipt(receipt_path, receipt)
    with ZipFile(bundle_path) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Corrupt episode bundle")
        manifest = BundleManifest.model_validate_json(bundle.read("manifest.json"))
        if manifest.ereq_id != episode.id:
            raise ValueError("Bundle belongs to another episode")
        json.loads(bundle.read(manifest.files.results))
        if not bundle.read(manifest.files.replay):
            raise ValueError("Empty replay")
    returncode = None
    with (args.output / "validation.log").open("ab") as log:
        try:
            returncode = subprocess.run(
                [*validator, str(bundle_path.resolve())],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=args.validation_timeout_seconds,
            ).returncode
        except subprocess.TimeoutExpired:
            pass  # Recorded below as a failed whole observation.
    receipt.validation_completed_at = datetime.now(UTC)
    receipt.elapsed_seconds = (datetime.now(UTC) - receipt.started_at).total_seconds()
    receipt.bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if returncode is None:
        receipt.outcome = "validation_timeout"
    else:
        receipt.outcome = "completed" if returncode == 0 else "validation_failed"
    save_receipt(receipt_path, receipt)
    print(receipt.model_dump_json(indent=2))
    raise SystemExit(1 if returncode is None else returncode)


if __name__ == "__main__":
    main()
