# src/council/schemas.py
"""Pydantic models for the Council feature + the tiny DAG-layering engine.

The `consumes` graph on the seats is the *only* orchestration primitive: layers
are derived topologically (see `CouncilConfig.layers`), independent seats in a
layer run in parallel, and a seat that `consumes` others receives their answers
as context. Ported/condensed from llm-council's `schemas.DagSpec`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# A seat's role only changes how its prompt is framed and whether it streams:
#   panelist    — answers the prompt independently (a leaf in the DAG)
#   aggregator  — consolidates a subset of upstream seats (optional middle layer)
#   synthesizer — the final seat; reconciles everything into the answer the user sees
SeatRole = Literal["panelist", "aggregator", "synthesizer"]


class Seat(BaseModel):
    """One model playing one role in the council."""

    id: str
    name: str = ""
    role: SeatRole = "panelist"
    # Prisma model selection — resolved via endpoint_resolver.resolve_endpoint_by_id.
    endpoint_id: str = ""
    model: str = ""
    persona: str = ""                       # system prompt / stance for this seat
    color: str = "#5b9dff"                  # per-seat accent (UI only)
    temperature: float = 0.3
    consumes: list[str] = Field(default_factory=list)  # upstream seat ids
    tools: list[str] = Field(default_factory=list)   # tool names this seat owns
    skills: list[str] = Field(default_factory=list)  # .claude/skills dir names to inject
    enabled: bool = True


class CouncilConfig(BaseModel):
    """One named council = an ordered list of seats."""

    id: str = "panel"
    name: str = "The Council"
    seats: list[Seat] = Field(default_factory=list)

    def seat(self, seat_id: str) -> Seat:
        for s in self.seats:
            if s.id == seat_id:
                return s
        raise KeyError(seat_id)

    def active_seats(self) -> list[Seat]:
        """Enabled seats, with `consumes` pruned to other enabled seats so a
        disabled seat never blocks a layer or leaves a dangling dependency."""
        live = {s.id for s in self.seats if s.enabled}
        out: list[Seat] = []
        for s in self.seats:
            if not s.enabled:
                continue
            out.append(s.model_copy(update={"consumes": [c for c in s.consumes if c in live]}))
        return out

    def layers(self) -> list[list[Seat]]:
        """Topologically derive parallel execution layers from `consumes`.

        Operates on `active_seats()` so disabled seats are ignored. Raises on a
        cycle or a dependency that points outside the (enabled) seat set.
        """
        seats = self.active_seats()
        remaining = {s.id: set(s.consumes) for s in seats}
        by_id = {s.id: s for s in seats}
        done: set[str] = set()
        layers: list[list[Seat]] = []
        while remaining:
            ready = [sid for sid, deps in remaining.items() if deps <= done]
            if not ready:
                raise ValueError(
                    f"Council DAG cycle or missing dependency among {list(remaining)}"
                )
            layers.append([by_id[sid] for sid in ready])
            done |= set(ready)
            for sid in ready:
                remaining.pop(sid)
        return layers


class SeatResult(BaseModel):
    """The outcome of running one seat."""

    seat_id: str
    name: str = ""
    role: SeatRole = "panelist"
    ok: bool = True
    content: str = ""
    error: str = ""
