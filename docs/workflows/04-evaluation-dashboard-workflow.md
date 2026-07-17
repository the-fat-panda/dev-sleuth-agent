# Evaluation and evidence-dashboard workflow

## Status

Implemented as a local, read-only review surface and a deliberately small release gate.

## Evaluation flow

[`evals/frozen-cases.json`](../../evals/frozen-cases.json) defines three controls:

1. A known reproducible public-API error.
2. Missing repository information, which must return `NEED_INFO` before sandbox execution.
3. An unsafe candidate containing `subprocess`, which must return `INCONCLUSIVE` without starting a sandbox.

[`scripts/run_evaluation_checkpoint.py`](../../scripts/run_evaluation_checkpoint.py) executes the controls and writes JSON results to `.bugagent/evaluation/baseline.json`. This is a regression gate for core safety and evidence behavior, not a public accuracy benchmark.

```powershell
python -m scripts.run_evaluation_checkpoint --image <immutable-image-id>
```

## Dashboard flow

1. Run [`python -m bugagent.web`](../../bugagent/web.py) with a bundle directory.
2. [`RunStore.list_runs()`](../../bugagent/web.py) lists valid stored manifests and verdict summaries.
3. [`RunStore.get_run()`](../../bugagent/web.py) serves one bundle's ticket, candidate, evidence, verdict, manifest, and timeline through a local JSON endpoint.
4. [`web/app.js`](../../web/app.js) renders the verdict, score, expected and observed symptoms, candidate test, replay metrics, audit timeline, and an independent replay command.
5. The UI is read-only: it never starts a sandbox, calls a model, writes Jira comments, or creates pull requests.

```powershell
python -m bugagent.web --runs-root .bugagent/checkpoint-3 --port 8765
```

## Safety and review properties

- HTTP binding defaults to `127.0.0.1`.
- Run IDs and static file paths are checked for traversal before files are served.
- The browser sees normalized evidence and hashes, not sandbox credentials or full uncontrolled paths.
- The dashboard is suitable for a developer/judge review of existing proof, not production monitoring yet.

Unit coverage for the data API and traversal resistance is in [`tests/test_web.py`](../../tests/test_web.py).
