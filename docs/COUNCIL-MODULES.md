# Council runtime modules

This public module inventory describes implementation files, not private
personas, prompts, datasets or operational endpoints.

- `schemas.py`: CouncilConfig and Seat models; consumes edges form a DAG.
- `orchestrator.py`: run_council schedules dependency layers and streams progress.
- `seat_runner.py`: executes individual seat model calls and synthesis streams.
- `fresh_context.py`: curates upstream contributions within context budgets.
- `verify.py`: code verification support and bounded execution policy.

The web entry point is `routes/council_routes.py`, which calls `run_council`
and serializes progress as server-sent events. Independent seats in a layer
can run concurrently; downstream seats consume their configured predecessors.
Private brain knowledge and specialist business configurations are excluded.
