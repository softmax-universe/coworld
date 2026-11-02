"""Bound shared inference and revoke individual leases without stopping peers."""

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from coworld.examples.parley.evidence import ChatRequest
from coworld.runner.gpu_batch import batch_token
from coworld.runner.inference_pool import InferenceAcknowledgement, InferenceSnapshot, InferenceStatus, InferenceUsage


class TokenUsage(BaseModel):
    prompt_tokens: int | None = Field(ge=0)
    completion_tokens: int | None = Field(ge=0)


class CompletionUsage(BaseModel):
    usage: TokenUsage


@dataclass
class ActiveInference:
    request_id: str
    game_id: str
    cancelled: asyncio.Event


class InferenceGateway:
    client: httpx.AsyncClient
    watcher: asyncio.Task[None]

    def __init__(self, *, worker: str, model: str, secret: str, snapshot: Path, endpoint: str, capacity: int) -> None:
        self.worker = worker
        self.model = model
        self.secret = secret
        self.snapshot_path = snapshot
        self.endpoint = endpoint
        self.capacity = capacity
        self.snapshot = InferenceSnapshot(revision=1, expires_at=0, leases=[])
        self.revision = 0
        self.active: dict[str, ActiveInference] = {}
        self.failed_aborts: set[str] = set()
        self.controller_failed = False

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                self.client = client
                self.watcher = asyncio.create_task(self.watch())
                try:
                    yield
                finally:
                    self.watcher.cancel()
                    await asyncio.gather(self.watcher, return_exceptions=True)
                    await self.revoke(set(self.active))
                    if not self.watcher.cancelled():
                        self.watcher.result()

        self.app = FastAPI(lifespan=lifespan)

        @self.app.get("/healthz")
        async def health() -> InferenceStatus:
            if self.watcher.done():
                self.watcher.result()
            if self.controller_failed or self.failed_aborts or self.snapshot.expires_at <= time.time():
                raise HTTPException(status_code=503, detail="Inference leases are not current")
            return InferenceStatus(revision=self.revision, active=len(self.active))

        @self.app.post("/v1/chat/completions")
        async def completion(
            request: ChatRequest,
            connection: Request,
            authorization: Annotated[str, Header()],
            x_coworld_xp_request: Annotated[str, Header()],
            x_parley_game: Annotated[str, Header()],
            x_parley_decision: Annotated[str, Header()],
            x_coworld_inference_timeout: Annotated[float, Header(gt=0, le=300)] = 45,
        ) -> Response:
            if self.watcher.done():
                self.watcher.result()
            expected = "Bearer " + batch_token(self.secret, x_coworld_xp_request, "inference")
            if not hmac.compare_digest(authorization, expected):
                raise HTTPException(status_code=403, detail="Invalid inference lease")
            grants = [lease for lease in self.snapshot.leases if lease.request_id == x_coworld_xp_request]
            now = time.time()
            if (
                self.controller_failed
                or self.failed_aborts
                or self.snapshot.expires_at <= now
                or not grants
                or grants[0].expires_at <= now
            ):
                raise HTTPException(status_code=410, detail="Inference lease ended")
            lease = grants[0]
            if x_parley_game not in lease.games:
                raise HTTPException(status_code=410, detail="Game inference lease ended")
            if request.model != self.model:
                raise HTTPException(status_code=403, detail="Model is outside this lease")
            if len(self.active) >= self.capacity or sum(
                entry.request_id == lease.request_id and entry.game_id in lease.games for entry in self.active.values()
            ) >= len(lease.games):
                raise HTTPException(status_code=429, detail="Inference capacity is already in use")
            identity = uuid4().hex
            started = time.monotonic()
            generating = asyncio.create_task(
                self.client.post(
                    self.endpoint + "/v1/chat/completions",
                    json=request.model_dump(by_alias=True, exclude_none=True),
                    headers={"X-Coworld-Inference-Id": identity},
                )
            )
            cancellation = asyncio.Event()
            self.active[identity] = ActiveInference(lease.request_id, x_parley_game, cancellation)
            cancelled = asyncio.create_task(cancellation.wait())
            disconnected = asyncio.create_task(self.disconnected(connection))
            status = 499
            response_ready_seconds: float | None = None
            usage: TokenUsage | None = None
            try:
                done, _ = await asyncio.wait(
                    (generating, disconnected, cancelled),
                    timeout=min(x_coworld_inference_timeout, lease.expires_at - now),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if generating not in done or generating.cancelled():
                    raise HTTPException(status_code=410, detail="Inference attempt ended")
                response = generating.result()
                response_ready_seconds = time.monotonic() - started
                status = response.status_code
                if response.is_success:
                    usage = CompletionUsage.model_validate_json(response.content).usage
                return Response(response.content, status_code=status, media_type="application/json")
            finally:
                disconnected.cancel()
                cancelled.cancel()
                await asyncio.gather(disconnected, cancelled, return_exceptions=True)
                confirmed = status != 499
                try:
                    if not confirmed:
                        # Keep the native response connection open. An abort
                        # submission alone does not establish generation stopped.
                        async with asyncio.timeout(10):
                            while True:
                                aborted = await self.client.post(
                                    self.endpoint + "/abort", json={"rid": identity}, timeout=3
                                )
                                aborted.raise_for_status()
                                if generating.done():
                                    break
                                await asyncio.wait((generating,), timeout=0.1)
                            terminal = generating.result()
                            terminal.raise_for_status()
                            usage = CompletionUsage.model_validate_json(terminal.content).usage
                        confirmed = True
                finally:
                    if confirmed:
                        self.active.pop(identity, None)
                    else:
                        self.failed_aborts.add(identity)
                    print(
                        InferenceUsage(
                            worker=self.worker,
                            request_id=lease.request_id,
                            user_id=lease.user_id,
                            game_id=x_parley_game,
                            decision_id=x_parley_decision,
                            inference_id=identity,
                            elapsed_seconds=time.monotonic() - started,
                            response_ready_seconds=response_ready_seconds,
                            status_code=status if confirmed else 502,
                            prompt_tokens=usage.prompt_tokens if usage is not None else None,
                            completion_tokens=usage.completion_tokens if usage is not None else None,
                        ).model_dump_json(),
                        flush=True,
                    )

    async def disconnected(self, request: Request) -> None:
        # The body is consumed before this handler runs. Wait for the ASGI
        # disconnect directly: is_disconnected() can swallow task cancellation.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    async def revoke(self, identities: set[str]) -> None:
        for identity, entry in self.active.items():
            if identity in identities:
                entry.cancelled.set()
        # Request handlers own native abort and accounting. A revision is acknowledged
        # only after every revoked handler has completed that cleanup.
        while identities.intersection(self.active):
            await asyncio.sleep(0.01)

    async def watch(self) -> None:
        try:
            while True:
                snapshot = InferenceSnapshot.model_validate_json(self.snapshot_path.read_bytes())
                if snapshot.revision > self.revision:
                    if len({lease.request_id for lease in snapshot.leases}) != len(snapshot.leases):
                        raise ValueError("Inference lease identities must be unique")
                    if sum(len(lease.games) for lease in snapshot.leases) > self.capacity:
                        raise ValueError("Inference grants exceed model capacity")
                    self.snapshot = snapshot
                now = time.time()
                allowed = {
                    (lease.request_id, game)
                    for lease in self.snapshot.leases
                    if min(lease.expires_at, self.snapshot.expires_at) > now
                    for game in lease.games
                }
                await self.revoke(
                    {
                        identity
                        for identity, entry in self.active.items()
                        if (entry.request_id, entry.game_id) not in allowed
                    }
                )
                if self.revision != self.snapshot.revision:
                    self.revision = self.snapshot.revision
                    print(
                        InferenceAcknowledgement(worker=self.worker, revision=self.revision).model_dump_json(),
                        flush=True,
                    )
                await asyncio.sleep(0.2)
        finally:
            self.controller_failed = True
            await self.revoke(set(self.active))


async def serve(gateway: InferenceGateway, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(gateway.app, host="0.0.0.0", port=port, timeout_graceful_shutdown=15))
    async with asyncio.TaskGroup() as tasks:
        http = tasks.create_task(server.serve())
        try:
            while not server.started:
                if http.done():
                    http.result()
                    return
                await asyncio.sleep(0.01)
            done, _ = await asyncio.wait((http, gateway.watcher), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    task.result()
        finally:
            server.should_exit = True


if __name__ == "__main__":
    gateway = InferenceGateway(
        worker=os.environ["COWORLD_INFERENCE_WORKER"],
        model=os.environ["COWORLD_INFERENCE_MODEL"],
        secret=os.environ["COWORLD_INFERENCE_SECRET"],
        snapshot=Path(os.environ["COWORLD_INFERENCE_SNAPSHOT"]),
        endpoint=os.environ.get("COWORLD_INFERENCE_UPSTREAM", "http://127.0.0.1:8002"),
        capacity=int(os.environ["COWORLD_INFERENCE_CAPACITY"]),
    )
    asyncio.run(serve(gateway, int(os.environ.get("COWORLD_INFERENCE_PORT", "8003"))))
