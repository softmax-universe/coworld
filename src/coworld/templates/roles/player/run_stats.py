"""Dependency-free run recorder and local charts page for your player.

Each episode appends one JSON line to ``player_runs/runs.jsonl`` and rewrites ``player_runs/report.html``: a static page
with inline-SVG charts (score per episode, rolling win rate, steps per episode, action mix, and the latest episode's
reward curve). Open it in a browser; no server, network, or extra package is needed.

Run ``python run_stats.py [runs_dir]`` to rebuild the page from an existing ``runs.jsonl``.
"""

from __future__ import annotations

import html
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_DIR = "player_runs"
MAX_CURVE_POINTS = 400


class RunRecorder:
    """Collects one episode's numbers. Call ``step`` per message and ``finish`` once at the end."""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = Path(directory or os.environ.get("COWORLD_PLAYER_STATS_DIR", DEFAULT_DIR))
        self.started = time.time()
        self.actions: Counter[str] = Counter()
        self.curve: list[float] = []
        self.steps = 0
        self.score: float | None = None

    def step(self, action: str | None = None, score: float | None = None) -> None:
        """Record one step: the action name taken and the running score/reward the game reported, if any."""
        self.steps += 1
        if action:
            self.actions[action] += 1
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            self.score = float(score)
            self.curve.append(self.score)

    def finish(self, won: bool | None = None) -> Path | None:
        """Append this episode to runs.jsonl and rebuild report.html. Never raises: stats must not break play."""
        record = {
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seconds": round(time.time() - self.started, 2),
            "steps": self.steps,
            "score": self.score,
            "won": won,
            "actions": dict(self.actions),
            "curve": _downsample(self.curve),
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "runs.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            return write_report(self.directory)
        except Exception as error:  # noqa: BLE001 - stats are best-effort and must never break play
            print(f"run_stats: could not write stats: {error}", file=sys.stderr)
            return None


def _downsample(values: list[float]) -> list[float]:
    if len(values) <= MAX_CURVE_POINTS:
        return values
    stride = len(values) / MAX_CURVE_POINTS
    return [values[int(i * stride)] for i in range(MAX_CURVE_POINTS)]


def load_runs(directory: str | os.PathLike[str]) -> list[dict[str, Any]]:
    path = Path(directory) / "runs.jsonl"
    if not path.is_file():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written line must not hide the rest
    return runs


def write_report(directory: str | os.PathLike[str]) -> Path:
    out = Path(directory) / "report.html"
    out.write_text(render_report(load_runs(directory)), encoding="utf-8")
    return out


W, H, PAD_L, PAD_B, PAD_T, PAD_R = 440, 200, 44, 24, 12, 12
INK, GRID, BLUE, ORANGE = "#1f2933", "#e4e7eb", "#2f6fdb", "#d9822b"


def _nice(lo: float, hi: float) -> tuple[float, float]:
    if hi == lo:
        return lo - 1, hi + 1
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def _fmt(v: float) -> str:
    if not math.isfinite(v):
        return str(v)
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.2f}"


def _axes(lo: float, hi: float, n: int) -> str:
    parts = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = H - PAD_B - (H - PAD_B - PAD_T) * i / 4
        parts.append(
            f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
            f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="{INK}">{_fmt(v)}</text>'
        )
    parts.append(f'<text x="{PAD_L}" y="{H - 6}" font-size="10" fill="{INK}">1</text>')
    parts.append(f'<text x="{W - PAD_R}" y="{H - 6}" text-anchor="end" font-size="10" fill="{INK}">{n}</text>')
    return "".join(parts)


def _line_chart(title: str, ys: list[float | None], *, fixed: tuple[float, float] | None = None) -> str:
    pts = [(i, y) for i, y in enumerate(ys) if y is not None]
    if not pts:
        return _card(title, '<p class="empty">No data yet.</p>')
    lo, hi = fixed or _nice(min(y for _, y in pts), max(y for _, y in pts))
    n = max(len(ys), 2)

    def xy(i: int, y: float) -> tuple[float, float]:
        x = PAD_L + (W - PAD_L - PAD_R) * i / (n - 1)
        return x, H - PAD_B - (H - PAD_B - PAD_T) * (y - lo) / (hi - lo)

    coords = [xy(i, y) for i, y in pts]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    dots = (
        "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{BLUE}"/>' for x, y in coords) if len(pts) <= 40 else ""
    )
    svg = (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">{_axes(lo, hi, len(ys))}'
        f'<polyline points="{path}" fill="none" stroke="{BLUE}" stroke-width="2"/>{dots}</svg>'
    )
    return _card(title, svg)


def _bar_chart(title: str, counts: dict[str, int]) -> str:
    if not counts:
        return _card(title, '<p class="empty">No actions recorded.</p>')
    items = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    top = max(v for _, v in items)
    rows = "".join(
        f'<div class="bar"><span class="k">{html.escape(k)}</span>'
        f'<span class="b"><i style="width:{100 * v / top:.1f}%"></i></span><span class="v">{v}</span></div>'
        for k, v in items
    )
    return _card(title, rows)


def _card(title: str, body: str) -> str:
    return f'<section class="card"><h2>{html.escape(title)}</h2>{body}</section>'


def _tile(label: str, value: str) -> str:
    return f'<div class="tile"><div class="n">{html.escape(value)}</div><div class="l">{html.escape(label)}</div></div>'


def render_report(runs: list[dict[str, Any]]) -> str:
    scores = [r.get("score") for r in runs]
    scored = [s for s in scores if isinstance(s, (int, float))]
    judged = [r["won"] for r in runs if isinstance(r.get("won"), bool)]
    window = 10
    rolling: list[float | None] = []
    seen: list[bool] = []
    for r in runs:
        if isinstance(r.get("won"), bool):
            seen.append(r["won"])
        rolling.append(100 * sum(seen[-window:]) / len(seen[-window:]) if seen else None)
    actions: Counter[str] = Counter()
    for r in runs:
        actions.update(r.get("actions") or {})
    tiles = [
        _tile("episodes", str(len(runs))),
        _tile("mean score", _fmt(sum(scored) / len(scored)) if scored else "-"),
        _tile("best score", _fmt(max(scored)) if scored else "-"),
        _tile("win rate", f"{100 * sum(judged) / len(judged):.0f}%" if judged else "-"),
    ]
    latest = runs[-1].get("curve", []) if runs else []
    cards = [
        _line_chart("Score per episode", scores),
        _line_chart(f"Win rate (rolling {window} episodes, %)", rolling, fixed=(0, 100)),
        _line_chart("Steps per episode", [r.get("steps") for r in runs]),
        _bar_chart("Action mix (all episodes)", dict(actions)),
        _line_chart("Latest episode: score by step", latest),
    ]
    body = "".join(cards) if runs else '<p class="empty">No episodes recorded yet. Run your player, then reload.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My player: run charts</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;color:{INK};background:#f7f8fa;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#616e7c;margin:0 0 16px}}
.tiles{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.tile{{background:#fff;border:1px solid {GRID};border-radius:8px;padding:10px 16px;min-width:110px}}
.tile .n{{font-size:24px;font-weight:600}} .tile .l{{color:#616e7c;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:12px}}
.card{{background:#fff;border:1px solid {GRID};border-radius:8px;padding:12px 14px}}
.card h2{{font-size:13px;margin:0 0 8px}} svg{{width:100%;height:auto}}
.empty{{color:#616e7c}} .bar{{display:flex;align-items:center;gap:8px;margin:4px 0}}
.bar .k{{width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar .b{{flex:1;background:{GRID};border-radius:3px;height:12px}}
.bar i{{display:block;height:12px;background:{ORANGE};border-radius:3px}}
.bar .v{{width:44px;text-align:right;color:#616e7c}}
</style></head><body>
<h1>My player: run charts</h1>
<p class="sub">Generated from player_runs/runs.jsonl by run_stats.py. Reload after each episode.</p>
<div class="tiles">{"".join(tiles)}</div>
<div class="grid">{body}</div>
</body></html>
"""


if __name__ == "__main__":
    target = write_report(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR)
    print(f"Wrote {target}")
