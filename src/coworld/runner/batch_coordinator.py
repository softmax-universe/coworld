"""Authenticate independent XP games and forward concurrent model requests."""

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from coworld.examples.parley.evidence import ChatRequest
from coworld.runner.gpu_batch import BatchGames, BatchPlan, batch_token


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["turn"]
    game_id: str
    decision_id: str
    seat: int = Field(ge=0, le=4)
    budget_ms: int = Field(default=45000, ge=1, le=300000)


class Finished(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["finish"]
    game_id: str


Control = Annotated[Turn | Finished, Field(discriminator="kind")]


@dataclass
class Game:
    decision: int = 0
    seat: int | None = None
    deadline: float = 0


class InferenceLog(BaseModel):
    event: Literal["xp_batch_inference"] = "xp_batch_inference"
    game_id: str
    decision_id: str
    worker: int
    active_games: int
    inference_seconds: float


class Coordinator:
    client: httpx.AsyncClient

    def __init__(self, plan: BatchPlan, secret: str, live_games: Path) -> None:
        if len(set(plan.games)) != len(plan.games):
            raise ValueError("Batch game identities must be unique")
        self.plan = plan
        self.secret = secret
        self.live_games = live_games
        self.games = {game: Game() for game in plan.games}
        self.requests: dict[str, asyncio.Task[bytes]] = {}

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            async with httpx.AsyncClient(timeout=plan.timeout_seconds, trust_env=False) as self.client:
                try:
                    yield
                finally:
                    running = list(self.requests.values())
                    for task in running:
                        task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)

        self.app = FastAPI(lifespan=lifespan)

        @self.app.get("/healthz")
        async def health() -> dict[str, str]:
            return {"status": "ready"}

        @self.app.post("/control")
        async def control(message: Control, authorization: Annotated[str, Header()]) -> Response:
            self.authorize(message.game_id, "game", authorization)
            if isinstance(message, Finished):
                self.finish(message.game_id)
            else:
                if message.game_id not in self.games:
                    raise HTTPException(status_code=410, detail="Batch game ended")
                game = self.games[message.game_id]
                if game.seat is not None and self.plan.seats[game.seat] is not None:
                    raise HTTPException(status_code=409, detail="Game is outside its next decision boundary")
                if message.decision_id != str(game.decision + 1):
                    raise HTTPException(status_code=409, detail="Batch decision identity is not consecutive")
                game.decision += 1
                game.seat = message.seat
                game.deadline = time.monotonic() + message.budget_ms / 1000
            return Response(b"{}", media_type="application/json")

        @self.app.post("/v1/chat/completions")
        async def completion(
            request: ChatRequest,
            authorization: Annotated[str, Header()],
            x_parley_game: Annotated[str, Header()],
            x_parley_decision: Annotated[str, Header()],
            x_coworld_player_slot: Annotated[int, Header()],
        ) -> Response:
            self.authorize(x_parley_game, f"player:{x_coworld_player_slot}", authorization)
            if x_parley_game not in self.games:
                raise HTTPException(status_code=410, detail="Batch game ended")
            game = self.games[x_parley_game]
            if game.seat is None:
                raise HTTPException(status_code=409, detail="No issued decision")
            worker = self.plan.seats[game.seat]
            if (
                worker is None
                or x_parley_decision != str(game.decision)
                or x_coworld_player_slot != game.seat
                or request.model != self.plan.models[worker].model
                or x_parley_game in self.requests
            ):
                raise HTTPException(status_code=409, detail="Inference does not match its issued game decision")
            task = asyncio.create_task(
                self.infer(x_parley_game, game, request, worker), name=f"{x_parley_game}:{x_parley_decision}"
            )
            task.add_done_callback(partial(self.inference_done, x_parley_game))
            self.requests[x_parley_game] = task
            try:
                await asyncio.wait((task,))
                if task.cancelled() or task.exception() is not None:
                    raise HTTPException(status_code=410, detail="Batch inference ended")
                return Response(task.result(), media_type="application/json")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def authorize(self, game_id: str, role: str, authorization: str) -> None:
        expected = "Bearer " + batch_token(self.secret, game_id, role)
        if game_id not in self.plan.games or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=403, detail="Invalid batch participant")

    async def infer(self, game_id: str, game: Game, request: ChatRequest, worker: int) -> bytes:
        started = time.monotonic()
        remaining = game.deadline - started
        if remaining <= 0:
            raise HTTPException(status_code=410, detail="Game decision deadline ended")
        response = await self.client.post(
            self.plan.models[worker].endpoint + "/v1/chat/completions",
            json=request.model_dump(by_alias=True, exclude_none=True),
            headers={
                "Authorization": "Bearer " + self.plan.models[worker].token,
                "X-Coworld-Xp-Request": self.plan.request_id,
                "X-Parley-Game": game_id,
                "X-Parley-Decision": str(game.decision),
                "X-Coworld-Inference-Timeout": str(remaining),
            },
            timeout=remaining,
        )
        response.raise_for_status()
        print(
            InferenceLog(
                game_id=game_id,
                decision_id=str(game.decision),
                worker=worker,
                active_games=len(self.games),
                inference_seconds=time.monotonic() - started,
            ).model_dump_json(),
            flush=True,
        )
        game.seat = None
        return response.content

    def inference_done(self, game_id: str, task: asyncio.Task[bytes]) -> None:
        self.requests.pop(game_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            asyncio.get_running_loop().call_exception_handler(
                {"message": "XP game inference failed", "exception": error, "task": task}
            )

    def finish(self, game_id: str) -> None:
        self.games.pop(game_id, None)
        if game_id in self.requests:
            self.requests[game_id].cancel()

    async def watch_games(self) -> None:
        while self.games:
            live = set(BatchGames.model_validate_json(self.live_games.read_bytes()).games)
            if not live.issubset(self.plan.games):
                raise ValueError("Live games are outside this XP request")
            for game_id in self.games.keys() - live:
                self.finish(game_id)
            await asyncio.sleep(0.2)


async def serve(coordinator: Coordinator, *, host: str = "0.0.0.0", port: int = 8080) -> None:
    server = uvicorn.Server(
        uvicorn.Config(coordinator.app, host=host, port=port, access_log=False, timeout_graceful_shutdown=5)
    )
    async with asyncio.TaskGroup() as tasks:
        http = tasks.create_task(server.serve())
        watcher = tasks.create_task(coordinator.watch_games())
        try:
            done, _ = await asyncio.wait(
                {http, watcher}, timeout=coordinator.plan.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise TimeoutError("XP batch lifetime ended")
            for task in done:
                task.result()
        finally:
            server.should_exit = True
            watcher.cancel()


if __name__ == "__main__":
    asyncio.run(
        serve(
            Coordinator(
                BatchPlan.model_validate_json(os.environ["COWORLD_GPU_BATCH_PLAN"]),
                os.environ["COWORLD_GPU_BATCH_SECRET"],
                Path(os.environ["COWORLD_GPU_BATCH_GAMES_FILE"]),
            ),
            port=int(os.environ.get("COWORLD_GPU_BATCH_PORT", "8080")),
        )
    )
