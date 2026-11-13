import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from coworld.api_client import CoworldApiClient
from coworld.policy_source.archive import (
    existing_source_directory,
    materialize_source,
    pack_source,
    validate_source_archive,
)
from coworld.policy_source.schemas import (
    PolicySourceAttachment,
    PolicySourceOrigin,
    PolicySourceUpload,
    PullPolicyReceipt,
    SourceContent,
    SourceMarker,
)


class SourceReceipt(BaseModel):
    kind: Literal["source_archive", "submitted_file", "not_recorded"]
    policy_version_id: UUID
    policy_name: str
    version: int
    content_hash: str | None = None


class PolicySourceClient(CoworldApiClient):
    _follow_redirects = False

    def stage_policy_source(
        self, source: Path, *, origin: PolicySourceOrigin | None = None, entry_paths: list[str] | None = None
    ) -> PolicySourceAttachment:
        archive = pack_source(source)
        validate_source_archive(archive, entry_paths)
        uploaded = self._request(
            "POST",
            "/v2/policy-source-uploads",
            PolicySourceUpload,
            files={"archive": ("source.zip", archive, "application/zip")},
            timeout=120,
        )
        return PolicySourceAttachment(upload=uploaded, origin=origin, entry_paths=entry_paths or [])

    def attach_policy_source(self, version_id: UUID, source: PolicySourceAttachment) -> SourceReceipt:
        return self._request(
            "PUT",
            f"/v2/policy-versions/{version_id}/source",
            SourceReceipt,
            json=source.model_dump(mode="json"),
            timeout=120,
        )

    def policy_source(self, version_id: UUID | str) -> SourceReceipt:
        return self._get(f"/v2/policy-versions/{version_id}/source", SourceReceipt)

    def pull_policy(
        self, ref: str, *, output_dir: Path | None = None, player_id: str | None = None
    ) -> PullPolicyReceipt:
        version = self.resolve_policy_version(ref, player_id=player_id)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", version.name).strip(".-") or "policy"
        destination = (output_dir or Path("policies") / safe_name / f"v{version.version}").absolute()
        destination = destination.parent.resolve() / destination.name
        with self._http_client.stream(
            "GET", f"/v2/policy-versions/{version.id}/source/content", headers=self._headers(), timeout=120
        ) as response:
            self._check_response(response)
            if response.headers["content-type"].split(";", 1)[0] == "application/json":
                receipt = SourceReceipt.model_validate_json(response.read())
                if receipt.kind != "not_recorded":
                    raise ValueError("Expected source bytes or an explicit missing-source result")
                return PullPolicyReceipt(
                    kind="not_recorded", policy_version_id=version.id, policy_name=version.name, version=version.version
                )
            content = SourceContent.model_validate(
                {
                    "kind": response.headers["X-Policy-Source-Kind"],
                    "content_hash": response.headers["X-Policy-Source-SHA256"],
                    "size_bytes": response.headers["Content-Length"],
                }
            )
            identity = SourceMarker(source_id=str(version.id), content_hash=content.content_hash)
            state: Literal["created", "already_open"] = "already_open"
            if not existing_source_directory(destination, identity):
                destination.parent.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix=".coworld-download-", dir=destination.parent) as temporary:
                    archive_path = Path(temporary) / "content"
                    count = 0
                    with archive_path.open("xb") as output:
                        for chunk in response.iter_bytes():
                            count += len(chunk)
                            if count > content.size_bytes:
                                raise ValueError("Source response exceeds its declared size")
                            output.write(chunk)
                    state = materialize_source(
                        archive_path, content=content, policy_version_id=str(version.id), destination=destination
                    )
        return PullPolicyReceipt(
            kind=content.kind,
            policy_version_id=version.id,
            policy_name=version.name,
            version=version.version,
            path=str(destination),
            content_hash=content.content_hash,
            size_bytes=content.size_bytes,
            state=state,
        )
