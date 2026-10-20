"""Live test of the gated answer pipeline against the REAL Ollama + brain.

Runs in-process (no HTTP/auth): builds the brain providers, wires the offline
model, and runs retrieve -> draft -> agent<->brain consensus. Prints the transcript.

  python scripts/test_answer.py --model vera-coder --prompt "your question"
"""
from __future__ import annotations
import argparse
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Reg:
    def __init__(self, providers): self._p = providers
    def active(self): return self._p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="How does the brain avoid storing duplicate notes?")
    ap.add_argument("--model", default="vera-coder")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--reviewer", default="vera-reasoner", help="separate reviewer model (real second opinion)")
    ap.add_argument("--escalate-model", default="",
                    help="strong model that AUTHORS hard answers (e.g. the 30B). Empty = same model, deep-reasoning pass.")
    ap.add_argument("--escalate-after", type=int, default=1,
                    help="hand authoring to the strong model after this many rejections (0 = from first draft)")
    args = ap.parse_args()

    from src.brains import build_brain_providers
    from src.answer_pipeline import wire_defaults, run_pipeline
    providers = build_brain_providers()
    print(f"brain providers active: {[getattr(p,'provider_id',getattr(p,'display_name','?')) for p in providers]}")
    fns = wire_defaults(model=args.model, registry=_Reg(providers), ollama_url=args.ollama_url,
                        reviewer_model=args.reviewer, escalate_model=args.escalate_model or None)
    res = run_pipeline(args.prompt, max_rounds=args.max_rounds, escalate_after=args.escalate_after, **fns)

    print("\n=== GATED ANSWER ===")
    print("approved :", res.approved)
    print("rounds   :", res.rounds)
    print("grounded :", res.grounded, "| sources:", res.sources[:6])
    print("\nANSWER:\n" + res.answer)
    print("\n--- transcript (model <-> agent) ---")
    for t in res.transcript:
        if t["role"] == "agent":
            tag = "AGENT approved=" + str(t.get("approved"))
        elif t["role"] == "model":
            tag = "MODEL(30B)" if t.get("escalated") else "MODEL"
        else:
            tag = t["role"].upper()
        print(f"[{tag}] {t['text'][:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
