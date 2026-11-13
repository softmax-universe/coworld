from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SOURCE_MAX_BYTES = 100 * 1024 * 1024
SOURCE_MAX_ENTRIES = 4096


class PolicySourceUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=SOURCE_MAX_BYTES)


class PolicySourceOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+$", max_length=2048)
    revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class PolicySourceAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload: PolicySourceUpload
    origin: PolicySourceOrigin | None = None
    entry_paths: list[str] = Field(default_factory=list, max_length=128)


class SourceMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_id: str = Field(alias="sourceId")
    content_hash: str = Field(alias="contentHash", pattern=r"^[0-9a-f]{64}$")


class SourceContent(BaseModel):
    kind: Literal["source_archive", "submitted_file"]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=SOURCE_MAX_BYTES)


class PullPolicyReceipt(BaseModel):
    kind: Literal["source_archive", "submitted_file", "not_recorded"]
    policy_version_id: UUID
    policy_name: str
    version: int
    path: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    state: Literal["created", "already_open"] | None = None
