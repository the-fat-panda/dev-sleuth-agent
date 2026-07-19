# Sandbox and evidence workflow

## Status

Implemented for Python/pytest candidate tests.

## Flow

1. The sandbox validates that the candidate is directly beneath `tests/bugagent_generated/`.
2. It starts an immutable Docker image with pytest collection only.
3. If collection succeeds, it starts a second restricted container to execute that test.
4. It normalizes the exception, repository frame, and message into a stable failure signature.
5. The evidence scorer accepts a positive verdict only when the candidate and two fresh replay runs are clean and independently verified. Crash bugs require matching repository-origin signatures. Silent wrong-output bugs require a repository contract, deterministic expected-value oracle, verified public-API observation, and matching expected-versus-actual replay pairs.

## Code trace

| Step | Code | Result |
|---|---|---|
| Enforce image and candidate-path policy | [`SandboxPolicy`](../../bugagent/sandbox/policy.py) | Requires `sha256:` image reference and generated-test path. |
| Construct restricted Docker invocation | [`SandboxPolicy.docker_prefix()`](../../bugagent/sandbox/policy.py) | No network, read-only root, uid `10001`, dropped capabilities, no-new-privileges, CPU/memory/PID limits, tmpfs, read-only source mount. |
| Run collection and execution | [`DockerSandbox.run()`](../../bugagent/sandbox/docker.py) | A collection error never appears as a product reproduction. |
| Bound runtime and output | [`DockerSandbox._run_command()`](../../bugagent/sandbox/docker.py) | Captures timeout and caps output bytes. |
| Normalize failure | [`normalize_failure_signature()`](../../bugagent/sandbox/docker.py) | Produces `Exception|file.py|message` rather than retaining full host paths. |
| Ground a silent-output claim | [`ground_silent_output()`](../../bugagent/silent_output.py) | Reads a pinned repository contract and deterministically derives expected minor-unit values from a statically verified probe; model metadata, if present, must agree. |
| Convert raw run to evidence | [`_evidence_from_run()`](../../bugagent/agent/orchestrator.py) | Derives symptom/frame/public API signals, output hashes, and any verified actual-versus-expected observation. |
| Verify outcome | [`assess_evidence()`](../../bugagent/scoring.py) | Applies the 100-point rubric and replay rule. |

## Positive-verdict gate

For a crash/exception reproduction, `REPRODUCED` requires all of these:

- candidate collection/setup is valid;
- candidate test fails without timeout;
- the failure matches the ticket symptom;
- the relevant frame belongs to repository code rather than generated test code;
- the candidate exercises a declared public API; and
- two clean replays return the same normalized failure signature.

For a silent wrong-output reproduction, `REPRODUCED` instead requires all of these:

- a supported, exact repository-owned contract citation with its source hash;
- an engine-owned deterministic oracle that derives the claimed expected values from that contract;
- a statically verified public `.quote()` probe that emits typed actual values before its assertions;
- a recorded product mismatch between those observed values and the oracle; and
- two fresh replays with the same contract hash, expected values, and observed values.

Any timeout, setup issue, non-failing candidate, ungrounded assertion, probe-validation failure, or replay disagreement prevents a positive verdict. The evidence bundle records the contract citation, hash, inputs, expected values, observed values, and verification failure when it abstains.

## Entry point and proof

Run the isolated sandbox checkpoint with:

```powershell
python -m scripts.run_sandbox_checkpoint --image <immutable-image-id>
```

The corresponding unit coverage is in [`tests/test_sandbox.py`](../../tests/test_sandbox.py) and [`tests/test_scoring.py`](../../tests/test_scoring.py).
