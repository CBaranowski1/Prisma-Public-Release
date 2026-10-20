<#
run_ab.ps1  --  One-command A/B for the evolved brain + retrieval stack.

Runs two arms over the SAME problem slice against your local model, then prints
the paired lift with a McNemar p-value, and (optionally) evolves the brain from
the losing arm's failures so the next run is smarter.

  Arm A (baseline)  : no hints, single sample        -> cc_base.jsonl
  Arm B (evolved)   : technique hints + best-of-N     -> cc_evolved.jsonl

USAGE (from repo root):
  powershell -ExecutionPolicy Bypass -File scripts\run_ab.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run_ab.ps1 -Model vera-coder -Limit 60 -Samples 3 -Evolve

Params:
  -Model     Ollama model tag to test          (default vera-coder)
  -Problems  problem jsonl                      (default data\evals\problems_cc_test.jsonl)
  -Limit     number of problems (0 = all)       (default 60)
  -Skip      skip first N (use unseen problems) (default 0)
  -Samples   best-of-N for the evolved arm      (default 3)
  -Evolve    after the run, fold the evolved arm's fails back into the technique notes
#>
param(
  [string]$Model    = "vera-coder",
  [string]$Problems = "data\evals\problems_cc_test.jsonl",
  [int]$Limit       = 60,
  [int]$Skip        = 0,
  [int]$Samples     = 3,
  [switch]$Evolve
)

$ErrorActionPreference = "Stop"
$py = "python"
$base = "data\evals\cc_base.jsonl"
$evo  = "data\evals\cc_evolved.jsonl"

Write-Host "== Arm A: baseline (no hints, 1 sample) ==" -ForegroundColor Cyan
& $py scripts\code_trainer.py --problems $Problems --model $Model `
    --limit $Limit --skip $Skip --hint-mode solutions --show-fails --out $base

Write-Host "== Arm B: evolved (technique hints + best-of-$Samples) ==" -ForegroundColor Cyan
& $py scripts\code_trainer.py --problems $Problems --model $Model `
    --limit $Limit --skip $Skip --hint-mode techniques --samples $Samples --show-fails --out $evo

Write-Host "== Lift (paired, McNemar) ==" -ForegroundColor Green
& $py scripts\ab_report.py --baseline $base --withbrain $evo

if ($Evolve) {
  $fails = "$evo.fails.json"
  if (Test-Path $fails) {
    Write-Host "== Evolving brain from $fails ==" -ForegroundColor Yellow
    & $py scripts\evolve_brain.py --fails $fails
    Write-Host "Brain updated. Re-run to measure the next-generation lift." -ForegroundColor Yellow
  } else {
    Write-Host "No fails file to learn from (perfect run or --show-fails off)." -ForegroundColor DarkGray
  }
}
