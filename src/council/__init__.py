"""Council — modular multi-model deliberation.

A *council* is a user-editable set of *seats*. Each seat is one model (referenced
by a Prisma ModelEndpoint id + model name) playing one role. Seats form a small
DAG via `consumes` edges; the final seat synthesizes the panel into one answer.

This package ports llm-council's orchestration concept (DAG layering + roles +
synthesis) but runs every seat through Prisma's existing model layer
(`resolve_endpoint_by_id` + `llm_call_async` / `stream_llm`), so adding, removing,
swapping, renaming, and recoloring models is all driven by the existing
model-endpoint registry and the council config — no backend/pool code of its own.
"""

from src.council.schemas import CouncilConfig, Seat, SeatResult
from src.council.orchestrator import run_council

__all__ = ["CouncilConfig", "Seat", "SeatResult", "run_council"]
