# BugAgent - Evidence-to-PR for bug triage

BugAgent turns a bug ticket into a reviewable proof bundle: a deterministic failing test, two clean-container replays, and a signed evidence trail. It is deliberately optimized for the moment a maintainer asks, "Can you prove this?"

It does not award `REPRODUCED` because a model sounds confident. The verdict is gated by collection success, a repository-level failure frame, symptom matching, public-API use, and two matching clean replays.

## Judge quick start

Prerequisite: Docker Desktop running locally. Build the locked Python test image once, then capture its immutable local image ID:

```powershell
docker build -f containers/python-pytest.Dockerfile -t bugagent-python-pytest:dev .
$image = docker image inspect bugagent-python-pytest:dev --format '{{.Id}}'
python -m scripts.run_agent_checkpoint --image $image
```

The checkpoint writes an immutable bundle beneath `.bugagent/checkpoint-3/<run-id>`. Replay it independently against the original fixture (this runs the generated test twice in new restricted containers):

```powershell
$bundle = Get-ChildItem .bugagent/checkpoint-3 -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
python -m bugagent replay --bundle $bundle.FullName --repo fixtures/sandbox_live --image $image
```

Open the evidence console in a second terminal:

```powershell
python -m bugagent.web --runs-root .bugagent/checkpoint-3 --port 8765
```

Then visit `http://127.0.0.1:8765`. The dashboard shows the ticket, generated test, normalized failure, replay count, hashes, and a copyable independent-replay command.

## What is implemented

- Bounded investigation loop with repository-context allowlists, an optional OpenAI Responses API client, and a strict candidate-test schema.
- Sandboxed pytest execution: immutable image only, no network, read-only filesystem, unprivileged user, dropped capabilities, CPU/memory/PID limits, output cap, and execution timeout.
- Conservative verifier: setup failures, timeouts, generated-test failures, and mismatched replays can never become a positive verdict.
- Hash-addressed artifacts: candidate, evidence, verdict, timeline, and manifest are atomically published and signed with SHA-256 hashes.
- Independent `bugagent replay` verifier: refuses tampered bundles, never modifies the supplied checkout, and demands matching signatures from two fresh runs.
- Judge-facing evidence console and frozen baseline controls for a positive, a `NEED_INFO` abstention, and an unsafe-test refusal.

## Release-gate checks

```powershell
python -m unittest discover -v
python -m scripts.run_evaluation_checkpoint --image $image
```

The current frozen baseline is intentionally small. It is a regression gate, not an accuracy claim; expand it to the documented 8-12 independently adjudicated cases before submission.

## OpenAI integration

`ResponsesInvestigationClient` uses the current Responses API with strict JSON schema output and defaults to `gpt-5.6`. Set `OPENAI_API_KEY` before wiring it into a production intake surface. The offline scripted fixture keeps the judge demo repeatable without credentials.

## Design and submission materials

- [High-level design](docs/HLD.md)
- [Low-level design](docs/LLD.md)
- [Delivery progress and remaining work](docs/progress.md)
- [Current and target architecture](docs/architecture.md)
- [Implemented workflow traces](docs/workflow.md)
- [Evaluation protocol](docs/EVALUATION.md)
- [Demo and submission plan](docs/SUBMISSION.md)
