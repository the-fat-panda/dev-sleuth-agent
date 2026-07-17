# Implemented workflow index

This index covers only flows that exist in the current codebase. The future Jira-to-fix-to-PR flow is documented as planned work in [architecture.md](architecture.md) and [progress.md](progress.md).

| Workflow | What it covers |
|---|---|
| [Investigation](workflows/01-investigation-workflow.md) | Ticket validation, bounded repository context, candidate generation, temporary worktree, retries, and verdicts. |
| [Sandbox and evidence](workflows/02-sandbox-evidence-workflow.md) | Restricted Docker execution, normalized signatures, and the reproduction threshold. |
| [Artifacts and replay](workflows/03-artifact-replay-workflow.md) | Atomic signed bundles and two-run independent verification. |
| [Evaluation and dashboard](workflows/04-evaluation-dashboard-workflow.md) | Frozen safety controls and the local evidence-review console. |
