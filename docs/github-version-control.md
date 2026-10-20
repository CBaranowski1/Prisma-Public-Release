# GitHub version control

The canonical private remote is `CBaranowski1/Prisma-Public-Release`. Git stores source,
tests, deployment manifests, and bundled skills. It must not store live data,
model weights, API keys, databases, logs, personal brain content, or backups.

## Normal change flow

```bash
git switch main
git pull --ff-only
git switch -c change/short-description
# edit and test
git add <specific-files>
git commit -m "type(scope): concise description"
git push -u origin change/short-description
```

Open a pull request into `main`, wait for CI and the secret scan, then merge.
Do not force-push `main` and do not disable a failing safety check to merge.

Enable the repository's versioned local safeguards once after each clone:

```powershell
./scripts/setup-git-safety.ps1
```

The hook blocks direct pushes to `main` and runs Gitleaks before every push.
GitHub Free does not provide server-side branch protection for private personal
repositories, so this local guard is required unless the account is upgraded.

## Deploy to the Spark

Deploy only a reviewed commit. Record the exact revision first:

```bash
git rev-parse HEAD
```

On the Spark, create a timestamped backup of configuration and persistent data,
pull with `--ff-only`, rebuild the Prisma image, and health-check the API before
removing the previous container image. Keep `.env` and runtime data outside Git.

## Recovery

For a source regression, revert the faulty commit with `git revert`; do not
rewrite shared history. For a deployment failure, restore the timestamped Spark
backup and redeploy the last known-good Git revision.

## Before every push

```bash
git status --short
git diff --check
python scripts/frontier_eval_gate.py --mode offline
python -m pytest -q
gitleaks git --redact --no-banner
```

If Gitleaks reports a real credential, rotate it first and remove it from all
Git history before pushing. Merely deleting it in a later commit is not enough.
