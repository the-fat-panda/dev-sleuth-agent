# Delivery progress

## Product goal

BugAgent's intended end-to-end behavior is:

1. Jira creates or updates a bug ticket.
2. BugAgent receives the event, resolves the linked repository and pinned commit, then attempts a reproduction in a sandbox.
3. If reproduced, it comments on Jira with the proof, starts a bounded fix investigation, validates a patch in a sandbox, opens a pull request, and comments the PR link on the ticket.
4. If it cannot reproduce, it comments on Jira with the exact attempts, evidence, and any information needed to continue.

## Current delivery status

| Product capability | Status | What exists now |
|---|---|---|
| Ticket data model and evidence verdicts | Done | Typed `Ticket`, evidence, verdict, and event records. |
| Reproduction agent loop | Done as a local core | Bounded candidate-test generation, safe repository context, and two clean replays. |
| OpenAI investigation client | Live-validated on banklib | `gpt-5.6-terra` produced a safe regression test from a vague ticket; the real Docker sandbox and two replays returned `REPRODUCED`. |
| Sandbox for reproduction | Done | No-network, read-only, unprivileged Docker execution with resource limits. |
| Evidence scoring and artifact bundles | Done | Deterministic verdict rubric, immutable run bundles, SHA-256 manifest. |
| Independent replay | Done | Verifies bundle hashes and reruns the candidate twice in a disposable copy. |
| Evidence dashboard | Done for local review | Local read-only web console over stored bundles. |
| HTTP investigation API | Done as an in-process service | FastAPI starts a real investigation in a worker thread, exposes job polling, and reads completed bundles through `RunStore`. |
| Investigation web workspace | Done as a local API client | Responsive submit, live SSE progress, collapsible evidence, and history views served at `/app/`. |
| Frozen release gate | Done, intentionally small | Three controls: reproduction, missing input, unsafe candidate refusal. |
| Jira notification intake | Foundation built, not live-configured | Signed `jira:issue_created` webhook endpoint, payload parser, in-process delivery de-duplication, and project-source lookup. |
| Jira comments | Foundation built, not live-configured | Jira Cloud REST v3 client posts a structured ADF evidence comment after a completed verdict. |
| GitHub repository checkout | Done and live-verified | An allow-listed GitHub source is cloned in the background to a disposable checkout, and `backend-main` on `the-fat-panda/e-commerce` resolved to commit `7adbe54…`. |
| Repository selection from Jira | GitHub mapping built | `BUGAGENT_JIRA_PROJECT_SOURCES` maps a project key to either a local checkout or an allow-listed GitHub repository/ref. |
| Fix generation and validation | Not started | No patch schema, disposable patch worktree, or before/after verification gate. |
| Pull-request creation | Not started | No Git provider client, branch, commit, or PR workflow. |
| Jira backlink to PR | Not started | Depends on Jira comments and PR creation. |
| Queue, retries, deployment, observability | Not started | The API job registry is local and in-memory; it has no durability, retry policy, deployment, or observability yet. |

## Completed checkpoints

| Checkpoint | Evidence |
|---|---|
| Evidence core | Unit-tested deterministic score and atomic SHA-256 artifact manifest. |
| Hardened sandbox | Two matching live Docker executions of the sample regression test. |
| Agent-to-proof | Scripted candidate creates a `REPRODUCED` bundle with score 100. |
| Evidence console | Local dashboard renders a bundle and its audit timeline. |
| Replay and release gate | Independent replay produces two matching signatures; 3/3 frozen controls pass. |
| Live investigation | A real `gpt-5.6-terra` call generated `Account().close()` for a vague banklib ticket; it failed with `ZeroDivisionError` at `banklib/account.py:6` and two replays agreed. |

## Remaining implementation plan

### Phase 6 - Jira intake and reproduction comments

The signed Jira webhook endpoint, in-process duplicate-delivery guard, GitHub-backed project-source mapping, and Jira comment renderer are built. The first real checkpoint is now configuration: point a test Jira project's signed `jira:issue_created` webhook at a publicly reachable HTTPS API endpoint, then verify it produces either a `REPRODUCED` evidence comment or a transparent `NEED_INFO` / `INCONCLUSIVE` comment. Durable idempotency remains pending.

### Phase 7 - Fix agent and sandbox validation

Add a separate, bounded patch-generation loop. A patch may only be generated after a verified reproduction. Each candidate patch must be applied to a disposable worktree and pass this gate: generated regression test fails before the patch, passes after the patch, and the selected existing test suite passes. The checkpoint is a fixture issue repaired without touching the source checkout.

### Phase 8 - Pull request and Jira backlink

Add a least-privilege Git provider adapter to create a branch, commit the accepted patch, open a PR, and send its URL plus evidence summary to Jira. The checkpoint is an end-to-end fixture run that creates a reviewable PR and leaves a Jira comment with the PR link.

### Phase 9 - Production controls and evaluation

Add a durable queue, retry and idempotency policy, webhook signature verification, secret storage, audit logging, rate limits, role-based approvals, hosted deployment, and a frozen 8-12 case evaluation set. The checkpoint is a deployed demo environment with a recorded end-to-end run and published evaluation results.

## What is deliberately not claimed

DevSleuthAgent is not yet a live-configured Jira bot, a code-fixing agent, or a PR automation product. It currently proves the middle of that future flow: **local ticket input (or a signed Jira event after configuration) -> real-model sandboxed reproduction attempt -> evidence bundle -> independent replay and review**.
