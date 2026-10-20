"""TOOL-04 (Phase 10 Plan 1): Seat tools/skills round-trip + DEFAULT_COUNCIL
ownership (D-02).

Two groups:

  - roundtrip: an OLD-shape Seat/CouncilConfig dict (no tools/skills keys)
    must still build, retain empty [] defaults, and produce the SAME
    `layers()` layering as an equivalent config — proving the new fields are
    byte-for-byte non-regressive and never read by the DAG engine.
  - ownership: the refreshed `DEFAULT_COUNCIL` seeds an owner seat per mapped
    tool/skill (roadmap SC-2) and a `chief` synthesizer that `consumes` every
    specialist seat id, composing them (closes A9).
"""
from __future__ import annotations

from src.council.schemas import CouncilConfig, Seat
from src.settings import DEFAULT_COUNCIL

# Old-shape seat dict — exactly the pre-Phase-10 DEFAULT_COUNCIL shape, with
# NO tools/skills keys present at all.
_OLD_SEAT_A = {
    "id": "seat_a", "name": "First Voice", "role": "panelist",
    "endpoint_id": "", "model": "",
    "persona": "You answer carefully and weigh trade-offs.",
    "color": "#5b9dff", "temperature": 0.4,
    "consumes": [], "enabled": True,
}
_OLD_SEAT_B = {
    "id": "seat_b", "name": "Second Voice", "role": "panelist",
    "endpoint_id": "", "model": "",
    "persona": "You answer decisively and challenge weak reasoning.",
    "color": "#41c980", "temperature": 0.6,
    "consumes": [], "enabled": True,
}
_OLD_CHIEF = {
    "id": "chief", "name": "Synthesizer", "role": "synthesizer",
    "endpoint_id": "", "model": "",
    "persona": "You reconcile the panel into one clear final answer.",
    "color": "#9b7cff", "temperature": 0.3,
    "consumes": ["seat_a", "seat_b"], "enabled": True,
}
_OLD_COUNCIL_DICT = {
    "id": "panel", "name": "The Council",
    "seats": [_OLD_SEAT_A, _OLD_SEAT_B, _OLD_CHIEF],
}

# Mapped skill dir names (D-04 / roadmap SC-2).
_MAPPED_SKILLS = {
    "code-review", "tdd", "diagnosing-bugs",
    "codebase-design", "improve-codebase-architecture",
    "to-prd", "to-issues",
}


# ---------------------------------------------------------------------------
# roundtrip group
# ---------------------------------------------------------------------------

def test_seat_roundtrip_old_shape_dict_yields_empty_defaults():
    seat = Seat(**_OLD_SEAT_A)
    assert seat.tools == []
    assert seat.skills == []


def test_council_config_roundtrip_old_shape_council_dict():
    cfg = CouncilConfig(**_OLD_COUNCIL_DICT)
    for seat in cfg.seats:
        dumped = seat.model_dump()
        assert dumped["tools"] == []
        assert dumped["skills"] == []


def test_council_config_roundtrip_layers_unchanged():
    """layers() output for an old-shape config must match an equivalent
    config built with explicit empty tools/skills — proving the new fields
    are inert to the DAG engine."""
    old_cfg = CouncilConfig(**_OLD_COUNCIL_DICT)

    explicit_dict = {
        "id": "panel", "name": "The Council",
        "seats": [
            {**_OLD_SEAT_A, "tools": [], "skills": []},
            {**_OLD_SEAT_B, "tools": [], "skills": []},
            {**_OLD_CHIEF, "tools": [], "skills": []},
        ],
    }
    explicit_cfg = CouncilConfig(**explicit_dict)

    old_layers = [[s.id for s in layer] for layer in old_cfg.layers()]
    explicit_layers = [[s.id for s in layer] for layer in explicit_cfg.layers()]
    assert old_layers == explicit_layers
    assert old_layers == [["seat_a", "seat_b"], ["chief"]]


def test_seat_tools_skills_not_read_by_active_seats_or_layers():
    """A seat with non-empty tools/skills must layer identically to one
    without — active_seats()/layers() must never branch on these fields."""
    with_extras = CouncilConfig(**{
        "id": "panel", "name": "The Council",
        "seats": [
            {**_OLD_SEAT_A, "tools": ["legacy_tool"], "skills": ["code-review"]},
            {**_OLD_SEAT_B, "tools": [], "skills": []},
            {**_OLD_CHIEF, "tools": [], "skills": []},
        ],
    })
    without_extras = CouncilConfig(**_OLD_COUNCIL_DICT)

    extras_layers = [[s.id for s in layer] for layer in with_extras.layers()]
    plain_layers = [[s.id for s in layer] for layer in without_extras.layers()]
    assert extras_layers == plain_layers

    extras_active = [(s.id, s.consumes) for s in with_extras.active_seats()]
    plain_active = [(s.id, s.consumes) for s in without_extras.active_seats()]
    assert extras_active == plain_active


# ---------------------------------------------------------------------------
# ownership group
# ---------------------------------------------------------------------------

def _default_council_config() -> CouncilConfig:
    return CouncilConfig(**DEFAULT_COUNCIL["councils"][0])


def test_default_council_ownership_builds_and_layers():
    cfg = _default_council_config()
    layers = cfg.layers()  # must not raise (no cycle / missing dep)
    assert len(layers) >= 2


def test_default_council_ownership_covers_every_mapped_skill():
    cfg = _default_council_config()
    skills_owned: set[str] = set()
    for seat in cfg.seats:
        skills_owned.update(seat.skills)
    assert _MAPPED_SKILLS <= skills_owned


def test_default_council_ownership_excludes_retired_run_query_tool():
    cfg = _default_council_config()
    tools_owned: set[str] = set()
    for seat in cfg.seats:
        tools_owned.update(seat.tools)
    assert "run_query" not in tools_owned


def test_default_council_chief_composes_every_specialist():
    cfg = _default_council_config()
    synthesizers = [s for s in cfg.seats if s.role == "synthesizer"]
    assert len(synthesizers) >= 1
    chief = cfg.seat("chief")
    assert chief.role == "synthesizer"
    specialist_ids = {s.id for s in cfg.seats if s.id != chief.id}
    assert specialist_ids <= set(chief.consumes)
