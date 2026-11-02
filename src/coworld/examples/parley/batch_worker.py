"""Preserve native inference evidence while SGLang batches concurrent requests."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from coworld.examples.parley.evidence import ChatRequest, SchemaFormat
from coworld.examples.parley.inference import SchemaResponseFormat


class FinishReason(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["stop", "length"]


class AbortReason(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["abort"]


class GenerationMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    finish_reason: FinishReason
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class AbortedGenerationMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    finish_reason: AbortReason
    # Prefill cancellation can terminate before native token usage is known.
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class Generation(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str
    meta_info: GenerationMetadata | AbortedGenerationMetadata


class AbortRequest(BaseModel):
    rid: str = Field(min_length=1)


def create_app(tokenizer: Any, model: str, endpoint: str) -> FastAPI:
    generation_client: httpx.AsyncClient
    control_client: httpx.AsyncClient
    tokenizer_pool: ThreadPoolExecutor

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal generation_client, control_client, tokenizer_pool
        # Serialize access to the tokenizer while keeping CPU work off the
        # request loop. Native inference and cancellation can advance meanwhile.
        with ThreadPoolExecutor(max_workers=1) as tokenizer_pool:
            async with (
                httpx.AsyncClient(timeout=None, trust_env=False) as generation_client,
                # Abort must not wait for a generation connection to become free.
                httpx.AsyncClient(timeout=10, trust_env=False) as control_client,
            ):
                yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/chat/completions")
    async def completion(
        request: ChatRequest, x_coworld_inference_id: Annotated[str | None, Header()] = None
    ) -> dict[str, JsonValue]:
        if request.model != model or request.chat_template_kwargs["enable_thinking"]:
            raise ValueError("Batch worker requires its frozen model and nonthinking decoding")
        if request.custom_params is not None:
            raise ValueError("Batch inference does not support custom thinking parameters")
        inputs = await asyncio.get_running_loop().run_in_executor(
            tokenizer_pool,
            partial(
                tokenizer.apply_chat_template,
                [message.model_dump() for message in request.messages],
                tokenize=True,
                return_dict=False,
                add_generation_prompt=True,
                **request.chat_template_kwargs,
            ),
        )
        sampling: dict[str, JsonValue] = {
            "temperature": request.temperature,
            "top_p": request.top_p if request.top_p is not None else 1.0,
            "top_k": request.top_k if request.top_k is not None else -1,
            "max_new_tokens": request.max_tokens,
            "stop": list(request.stop),
            "stop_regex": list(request.stop_regex),
            "no_stop_trim": request.no_stop_trim,
        }
        if request.seed is not None:
            sampling["sampling_seed"] = request.seed
        grammar = request.response_format
        if isinstance(grammar, SchemaFormat | SchemaResponseFormat):
            sampling["json_schema"] = json.dumps(grammar.json_schema.schema_)
        elif grammar is not None:
            if grammar["type"] == "json_object":
                sampling["json_schema"] = json.dumps({"type": "object"})
            elif grammar["type"] != "text":
                raise ValueError("Unsupported batch response format")
        identity = x_coworld_inference_id if x_coworld_inference_id is not None else uuid4().hex
        response = await generation_client.post(
            endpoint + "/generate", json={"input_ids": inputs, "sampling_params": sampling, "rid": identity}
        )
        response.raise_for_status()
        generation = Generation.model_validate_json(response.content)
        if generation.meta_info.id != identity:
            raise ValueError("Native generation returned a different request identity")
        return {
            "id": generation.meta_info.id,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": generation.text},
                    "finish_reason": generation.meta_info.finish_reason.type,
                }
            ],
            "usage": {
                "prompt_tokens": generation.meta_info.prompt_tokens,
                "completion_tokens": generation.meta_info.completion_tokens,
            },
            # Preserve the original native response alongside the chat envelope.
            "sglang_response": generation.model_dump(mode="json", exclude_unset=True),
        }

    @app.post("/abort")
    async def abort(request: AbortRequest) -> dict[str, bool]:
        response = await control_client.post(endpoint + "/abort_request", json=request.model_dump())
        response.raise_for_status()
        return {"aborted": True}

    return app


if __name__ == "__main__":
    # The GPU serving image owns the pinned Transformers dependency.
    from transformers import AutoTokenizer  # ty: ignore[unresolved-import]

    uvicorn.run(
        create_app(
            AutoTokenizer.from_pretrained("/models", local_files_only=True),
            "qwen4-" + os.environ["CHECKPOINT_SHA256"],
            "http://127.0.0.1:8000",
        ),
        host="0.0.0.0",
        port=8002,
        access_log=False,
    )
