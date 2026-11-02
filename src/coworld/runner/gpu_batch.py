"""Explicit opt-in for fixed-roster Parley inference batches."""

import hashlib
import hmac

from pydantic import BaseModel, ConfigDict, Field


class GpuBatchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concurrency: int = Field(default=4, ge=1, le=100, description="Maximum simultaneous games and model requests.")
    timeout_seconds: int = Field(default=3600, ge=60, le=21600, description="Batch lifetime, including model startup.")
    full_matches: bool = Field(
        default=False, description="Play every sampled round; the batch lifetime is a hard limit."
    )


class GpuBatchRequestSpec(BaseModel):
    """Read inference settings from a persisted XP request without loading its roster."""

    gpu_batch: GpuBatchOptions | None = None


class SharedInferenceModel(BaseModel):
    """The image supports launch.py --serve-only and the canonical CPU Parley player."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(pattern=r"^qwen4-[0-9a-f]{64}$")


def batch_token(secret: str, game_id: str, role: str) -> str:
    return hmac.new(secret.encode(), f"{game_id}:{role}".encode(), hashlib.sha256).hexdigest()


class BatchModel(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    model: str
    endpoint: str
    token: str = Field(min_length=1, repr=False)


class BatchGames(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    games: list[str] = Field(max_length=100)


class BatchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    games: list[str] = Field(min_length=1, max_length=100)
    seats: list[int | None] = Field(min_length=5, max_length=5)
    models: list[BatchModel] = Field(min_length=1, max_length=5)
    timeout_seconds: int = Field(ge=60, le=21600)
    request_id: str = Field(min_length=1)
