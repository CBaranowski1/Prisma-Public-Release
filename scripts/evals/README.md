# Frontier architecture gate

`frontier_contract_v1.json` is the fixed, schema-versioned deployment gate
derived from the frontier architecture AI-SPEC. It complements the existing
HumanEval/polyglot A/B harnesses: those measure code-task lift, while this gate
detects wiring regressions in skill disclosure, brain recall, reasoning
routing, tool choice, provenance, and recovery.

Fast deterministic CI run:

```bash
python scripts/frontier_eval_gate.py --mode offline --output artifacts/frontier-offline.json
```

Live Spark/GLM run (the endpoint may be a base URL, `/v1`, or the full
`/v1/chat/completions` URL):

```bash
FRONTIER_EVAL_ENDPOINT=http://172.18.0.1:1919/v1 \
FRONTIER_EVAL_MODEL=dealignai/GLM-5.3-Flash-UNCENSORED-NVFP4 \
python scripts/frontier_eval_gate.py --mode live \
  --output artifacts/frontier-live.json
```

If the endpoint requires a bearer token, put it in
`FRONTIER_EVAL_API_KEY`; credentials are never accepted in endpoint URLs or
written into the suite. An optional independent endpoint can be supplied with
`--fallback-endpoint` to measure successful endpoint recovery.

After one accepted live run, record the hardware/model baseline explicitly:

```bash
python scripts/frontier_eval_gate.py --mode live \
  --endpoint http://172.18.0.1:1919/v1 \
  --model dealignai/GLM-5.3-Flash-UNCENSORED-NVFP4 \
  --record-baseline scripts/evals/baselines/spark-glm53-nvfp4.json
```

Future runs use `--baseline` with that file. The report and exit status
separate capability regressions from latency/throughput regressions. Do not
record a baseline until the live cases pass and the Spark is otherwise idle.

For model/code completion lift, continue to use `scripts/ab_harness.py`,
`scripts/ab_report.py`, and `scripts/polyglot_ab/`; this gate deliberately does
not duplicate their execution scorers or McNemar reporting.
