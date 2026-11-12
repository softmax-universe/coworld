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
