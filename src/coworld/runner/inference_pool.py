"""Wire contracts for model workers shared by independently owned XP requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InferenceLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    user_id: str
    games: list[str] = Field(min_length=1, max_length=100)
    expires_at: float


class InferenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    expires_at: float
    leases: list[InferenceLease]


class InferenceStatus(BaseModel):
    revision: int
    active: int


class InferenceAcknowledgement(BaseModel):
    event: Literal["xp_inference_ack"] = "xp_inference_ack"
    worker: str
    revision: int = Field(ge=1)


class InferenceUsage(BaseModel):
    event: Literal["xp_shared_inference"] = "xp_shared_inference"
    worker: str
    request_id: str
    user_id: str
    game_id: str
    decision_id: str
    inference_id: str
    elapsed_seconds: float
    response_ready_seconds: float | None = Field(default=None, ge=0)
    status_code: int
    prompt_tokens: int | None
    completion_tokens: int | None
