from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any
from uuid import uuid4

from coworld.agent_guidance import update_agent_guidance
from coworld.api_client import AutoChampion, CoworldApiClient
from coworld.cli_support import console, emit_json, observatory_web_url
from coworld.config import DEFAULT_SUBMIT_SERVER, DOCS_PAGES
from coworld.feed_cli import print_feed_reminder


def submit_policy_to_league_cmd(
    policy_identifier: str,
    *,
    league_id: str,
    server: str = DEFAULT_SUBMIT_SERVER,
    open_browser: bool = True,
    auto_champion: AutoChampion = AutoChampion.always,
    preferences: dict[str, Any] | None = None,
    player_id: str | None = None,
    notes: str | None = None,
    json_output: bool = False,
    request_id: str | None = None,
) -> None:
    request_id = request_id or str(uuid4())
    if not json_output:
        update_agent_guidance(Path.cwd())
    with CoworldApiClient.from_login(server_url=server) as client:
        policy_version = client.resolve_policy_version(policy_identifier, player_id=player_id)
        if not json_output:
            coworld_name = client.get_league(league_id).game.coworld_name
            if coworld_name is not None:
                print_feed_reminder(server=server, coworld_name=coworld_name)
            console.print(
                f"[bold]Submitting {policy_version.name}:v{policy_version.version} to league {league_id}[/bold]"
            )
        existing = []
        cursor = None
        while True:
            page = client.list_submissions(
                league_id=league_id,
                player_id=player_id,
                policy_version_id=policy_version.id,
                mine=True,
                limit=200,
                cursor=cursor,
            )
            existing.extend(page.entries)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        matches = [
            row
            for row in existing
            if row.auto_champion == auto_champion.value
            and row.notes == notes
            and (row.preferences or {}) == (preferences or {})
            and (player_id is None or (row.player is not None and row.player.id == player_id))
        ]
        if existing and not matches:
            raise ValueError(
                "An existing submission for this player, league, and version has different options; "
                f"inspect submission {existing[0].id}"
            )
        if matches:
            submission = matches[0]
        else:
            submission = client.submit_to_league(
                league_id,
                policy_version.id,
                auto_champion=auto_champion,
                preferences=preferences,
                player_id=player_id,
                notes=notes,
                request_id=request_id,
            )
    if json_output:
        emit_json({**submission.model_dump(mode="json"), "request_id": request_id, "reconciled": bool(matches)})
        return

    placement_runs_async = submission.status in {"pending", "processing"}
    policy_path = f"/policies/versions/{policy_version.id}"
    policy_url = observatory_web_url(server, policy_path)
    console.print("[green]Submitted to league[/green]")
    console.print(f"[dim]League:[/dim] {league_id}")
    console.print(f"[dim]Submission:[/dim] {submission.id}")
    console.print(f"[dim]Status:[/dim] {submission.status}")
    console.print(f"[dim]Auto champion:[/dim] {auto_champion.value}")
    if submission.league_policy_membership_id is not None:
        console.print(f"[dim]Membership:[/dim] {submission.league_policy_membership_id}")
    elif placement_runs_async:
        console.print("[dim]Placement runs asynchronously; check the status page for updates.[/dim]")
    page_label = "Status page" if placement_runs_async else "Policy page"
    console.print(f"[dim]{page_label}:[/dim] {policy_url}", soft_wrap=True)
    console.print(f"[dim]Docs:[/dim] {DOCS_PAGES['submit-to-a-league']}", soft_wrap=True)
    if submission.league.forum_markdown_url is not None:
        console.print(f"[dim]League forum:[/dim] {submission.league.forum_markdown_url}", soft_wrap=True)
    if submission.league.wiki_markdown_url is not None:
        console.print(f"[dim]League wiki:[/dim] {submission.league.wiki_markdown_url}", soft_wrap=True)
    if open_browser:
        webbrowser.open(policy_url)
