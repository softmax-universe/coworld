from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from coworld.config import DEFAULT_SUBMIT_SERVER
from coworld.policy_source.client import PolicySourceClient
from coworld.policy_source.schemas import PolicySourceOrigin


def register_policy_source_commands(app: typer.Typer) -> None:
    @app.command("attach-policy-source", help="Attach private source to an existing policy version.")
    def attach(
        version_id: UUID,
        source: Annotated[Path, typer.Option("--source", exists=True)],
        repository: Annotated[str | None, typer.Option("--repository")] = None,
        revision: Annotated[str | None, typer.Option("--revision")] = None,
        entry: Annotated[list[str] | None, typer.Option("--entry")] = None,
        server: Annotated[str, typer.Option("--server")] = DEFAULT_SUBMIT_SERVER,
    ) -> None:
        if (repository is None) != (revision is None):
            raise typer.BadParameter("Supply --repository and --revision together")
        origin = PolicySourceOrigin(repository=repository, revision=revision) if repository and revision else None
        with PolicySourceClient.from_login(server_url=server) as client:
            uploaded = client.stage_policy_source(source, origin=origin, entry_paths=entry)
            receipt = client.attach_policy_source(version_id, uploaded)
            observed = client.policy_source(version_id)
        if receipt.content_hash != uploaded.upload.content_hash or observed.content_hash != receipt.content_hash:
            raise RuntimeError("Source readback does not match the uploaded archive")
        typer.echo(f"Source attached: {receipt.policy_name}:v{receipt.version}")
        typer.echo(f"Policy version ID: {receipt.policy_version_id}")
        typer.echo(f"Source SHA-256: {receipt.content_hash}")
