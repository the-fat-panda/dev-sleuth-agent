# Sandbox and evidence workflow

## Status

Implemented for Python/pytest candidate tests.

## Flow

1. The sandbox validates that the candidate is directly beneath `tests/bugagent_generated/`.
2. It starts an immutable Docker image with pytest collection only.
3. If collection succeeds, it starts a second restricted container to execute that test.
4. It normalizes the exception, repository frame, and message into a stable failure signature.
5. The evidence scorer accepts a positive verdict only when the candidate and two fresh replay runs are clean, relevant, and signature-identical.

## Code trace

| Step | Code | Result |
|---|---|---|
| Enforce image and candidate-path policy | [`SandboxPolicy`](../../bugagent/sandbox/policy.py) | Requires `sha256:` image reference and generated-test path. |
| Construct restricted Docker invocation | [`SandboxPolicy.docker_prefix()`](../../bugagent/sandbox/policy.py) | No network, read-only root, uid `10001`, dropped capabilities, no-new-privileges, CPU/memory/PID limits, tmpfs, read-only source mount. |
| Run collection and execution | [`DockerSandbox.run()`](../../bugagent/sandbox/docker.py) | A collection error never appears as a product reproduction. |
| Bound runtime and output | [`DockerSandbox._run_command()`](../../bugagent/sandbox/docker.py) | Captures timeout and caps output bytes. |
| Normalize failure | [`normalize_failure_signature()`](../../bugagent/sandbox/docker.py) | Produces `Exception|file.py|message` rather than retaining full host paths. |
| Convert raw run to evidence | [`_evidence_from_run()`](../../bugagent/agent/orchestrator.py) | Derives symptom/frame/public API signals and output hashes. |
| Verify outcome | [`assess_evidence()`](../../bugagent/scoring.py) | Applies the 100-point rubric and replay rule. |

## Positive-verdict gate

`REPRODUCED` requires all of these:

- candidate collection/setup is valid;
- candidate test fails without timeout;
- the failure matches the ticket symptom;
- the relevant frame belongs to repository code rather than generated test code;
- the candidate exercises a declared public API; and
- two clean replays return the same normalized failure signature.

Any timeout, setup issue, non-failing candidate, generated-test-only failure, or replay disagreement prevents a positive verdict.

## Entry point and proof

Run the isolated sandbox checkpoint with:

```powershell
python -m scripts.run_sandbox_checkpoint --image <immutable-image-id>
```

The corresponding unit coverage is in [`tests/test_sandbox.py`](../../tests/test_sandbox.py) and [`tests/test_scoring.py`](../../tests/test_scoring.py).
