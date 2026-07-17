# First-Place Submission Plan

## One-sentence pitch

**BugAgent turns a bug ticket into a reproduction PR: a deterministic failing test, clean-environment replay command, and auditable proof—not an AI opinion.**

## Three-minute demo storyboard

| Time | Show | Judge takeaway |
|---:|---|---|
| 0:00–0:20 | Engineer receives an ambiguous issue and lacks a reliable reproduction. | Real, expensive problem. |
| 0:20–0:45 | Paste ticket and select a pinned repository commit. | Clear, focused workflow. |
| 0:45–1:35 | Timeline: Codex investigates code, writes candidate test, sandbox executes it. | Codex is doing real repository-scale work. |
| 1:35–2:05 | Evidence card: expected vs observed error, score, trace, generated test. | Proof is inspectable, not hallucinated. |
| 2:05–2:25 | Click replay; show matching clean execution. | Determinism and trust. |
| 2:25–2:40 | Create/show reproduction patch and Jira-ready comment. | Immediate developer value. |
| 2:40–3:00 | Show frozen evaluation table, one abstention, and Codex/GPT-5.6 build usage. | Honest impact and technical depth. |

Record the happy path beforehand, but use the actual working product and preserve the run ID/artifacts used in the video.

## Devpost checklist

- Select **Developer Tools**.
- Public <3-minute YouTube video with audio explaining how Codex and GPT-5.6 were used.
- Public repository with licence, setup instructions, fixtures/sample data, architecture, evaluation results, and troubleshooting.
- One-command local demo or a hosted judge sandbox/test account; do not require judges to construct a repository.
- Include the required `/feedback` Codex session ID from the session that built core functionality.
- Explain the supported scope and safety model candidly.

## README content judges should see first

1. 20-second GIF/video of ticket → failing test → replay.
2. Exact problem statement and target user.
3. “How it works” diagram and the evidence threshold.
4. Run locally / judge demo in under five minutes.
5. Evaluation table with links to fixture evidence bundles.
6. Codex and GPT-5.6 contribution log.

## What not to spend time on

- Auto-fix before reliable reproduction.
- Broad language support before a complete Python experience.
- Full Jira OAuth and marketplace polish before a working local product.
- Inflated accuracy claims or a demo that hides inconclusive cases.

## Build sequence

1. Implement the deterministic sandbox and bundle writer.
2. Build the Codex investigation loop with a single fixture.
3. Add replay verification and matcher evidence UI.
4. Freeze the evaluation set and capture metrics.
5. Polish intake, evidence card, and PR patch export.
6. Record the demo; make a judge runbook and submit.
