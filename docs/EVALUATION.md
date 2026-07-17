# Evaluation protocol

## Purpose

The demo must show that BugAgent produces trustworthy evidence, not merely attractive examples. Results are reported on a frozen, small evaluation set; no claim is extrapolated beyond this scope.

## Full submission set

Before prompt tuning, freeze 8-12 pinned Python cases:

- 4 known reproducible issues with a historical regression test or maintainer-confirmed trigger.
- 2 vague-but-reproducible issues.
- 2 reports that lack essential information.
- 1 environment or setup failure.
- 1 issue outside the MVP support boundary.

Use repositories with permissive licences and tiny, pinned fixtures. Two humans should independently label each ticket `reproducible`, `need_info`, or `unsupported/inconclusive`, then resolve disagreements before comparison. A positive counts only when its generated test fails twice in separate fresh containers and a reviewer confirms that the symptom is relevant.

| Metric | Definition | Demo target |
|---|---|---:|
| Verified reproduction precision | confirmed positives / all `REPRODUCED` | 100% |
| Verified reproduction recall | confirmed positives found / known reproducible cases | >= 60% |
| Safe uncertainty accuracy | correct `NEED_INFO` or `INCONCLUSIVE` / applicable cases | >= 80% |
| Replay stability | positives with two matching replays / positives | 100% |
| Median time-to-proof | ticket submit to artifact bundle | Report honestly |

Precision and replay stability matter more than recall for the MVP. A missed bug is inconvenient; a fabricated proof destroys trust.

## Current frozen baseline

The repository ships a deliberately narrow three-control release gate in [`evals/frozen-cases.json`](../evals/frozen-cases.json). It proves that central guardrails work end-to-end, but it is not presented as a benchmark.

| Case | Expected behavior | Release-gate assertion |
|---|---|---|
| `SANDBOX-REPRO-1` | Known public-API `ZeroDivisionError` | `REPRODUCED` with score 100 and two matching clean replays |
| `MISSING-CONTEXT-1` | No pinned repository reference | `NEED_INFO` before any sandbox work |
| `UNSAFE-CANDIDATE-1` | Candidate proposes `subprocess` execution | `INCONCLUSIVE`; zero sandbox executions |

Run it with `python -m scripts.run_evaluation_checkpoint --image <immutable-image-id>`. The script writes JSON to `.bugagent/evaluation/baseline.json`, making the baseline repeatable in CI and easy to show during judging. Do not report precision or recall from these three controls. Freeze and independently adjudicate the full 8-12 case set before making those claims.

## Results page format

Publish fixture IDs, commit hashes, labels, verdicts, evidence scores, replay status, and failure modes. Include one false negative or inconclusive case and explain why the system abstained. Keep raw ticket details private where required.
