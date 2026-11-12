from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from coworld.api_client import CoworldHttpClient
from coworld.policy_source.archive import pack_source, validate_source_archive
from coworld.policy_source.schemas import PolicySourceAttachment, PolicySourceOrigin, PolicySourceUpload


class SourceReceipt(BaseModel):
    kind: Literal["source_archive", "submitted_file", "not_recorded"]
    policy_version_id: UUID
    policy_name: str
    version: int
    content_hash: str | None = None


class PolicySourceClient(CoworldHttpClient):
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

    def policy_source(self, version_id: UUID) -> SourceReceipt:
        return self._get(f"/v2/policy-versions/{version_id}/source", SourceReceipt)
