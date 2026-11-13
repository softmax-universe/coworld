from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from coworld.api_client import CoworldApiClient
from coworld.cli_support import emit_json
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

    @app.command("policies", help="List policy versions with exact name and player filters.")
    def policies(
        mine: Annotated[bool, typer.Option("--mine", help="Show only your policy versions.")] = False,
        name: Annotated[str | None, typer.Option("--name", help="Exact policy name.")] = None,
        player: Annotated[str | None, typer.Option("--player", help="Player ID.")] = None,
        limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        server: Annotated[str, typer.Option("--server")] = DEFAULT_SUBMIT_SERVER,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        with CoworldApiClient.from_login(server_url=server) as client:
            page = client.list_policy_versions(mine=mine, name=name, player_id=player, limit=limit, cursor=cursor)
        if json_output:
            emit_json(page.model_dump(mode="json"))
            return
        for row in page.entries:
            typer.echo(f"{row.name}:v{row.version}  {row.id}  {row.player_id or 'unassigned'}")
        if page.next_cursor is not None:
            typer.echo(f"Next cursor: {page.next_cursor}")

    @app.command("show-policy", help="Show an exact owned policy reference or version UUID.")
    def show_policy(
        ref: str,
        player: Annotated[str | None, typer.Option("--player", help="Player ID for an ambiguous name.")] = None,
        server: Annotated[str, typer.Option("--server")] = DEFAULT_SUBMIT_SERVER,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        with CoworldApiClient.from_login(server_url=server) as client:
            row = client.resolve_policy_version(ref, player_id=player)
        if json_output:
            emit_json(row.model_dump(mode="json"))
            return
        typer.echo(f"{row.name}:v{row.version}")
        typer.echo(f"Policy version ID: {row.id}")
        typer.echo(f"Player: {row.player_id or 'unassigned'}")

    @app.command("pull-policy", help="Open an editable copy of an exact owned policy's published source.")
    def pull_policy(
        ref: str,
        output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
        player: Annotated[str | None, typer.Option("--player", help="Player ID for an ambiguous name.")] = None,
        server: Annotated[str, typer.Option("--server")] = DEFAULT_SUBMIT_SERVER,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        with PolicySourceClient.from_login(server_url=server) as client:
            receipt = client.pull_policy(ref, output_dir=output_dir, player_id=player)
        if json_output:
            emit_json(receipt.model_dump(mode="json"))
        elif receipt.kind == "not_recorded":
            typer.echo(f"Source was not recorded for {receipt.policy_name}:v{receipt.version}")
        else:
            typer.echo(f"{receipt.policy_name}:v{receipt.version}  {receipt.policy_version_id}")
            typer.echo(f"Directory: {receipt.path} ({receipt.state})")
            typer.echo(f"SHA-256: {receipt.content_hash}")
        if receipt.kind == "not_recorded":
            raise typer.Exit(1)
