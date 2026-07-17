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
| OpenAI investigation client | Implemented, not live-validated | Strict-schema Responses API client; local demo uses a deterministic scripted client. |
| Sandbox for reproduction | Done | No-network, read-only, unprivileged Docker execution with resource limits. |
| Evidence scoring and artifact bundles | Done | Deterministic verdict rubric, immutable run bundles, SHA-256 manifest. |
| Independent replay | Done | Verifies bundle hashes and reruns the candidate twice in a disposable copy. |
| Evidence dashboard | Done for local review | Local read-only web console over stored bundles. |
| Frozen release gate | Done, intentionally small | Three controls: reproduction, missing input, unsafe candidate refusal. |
| Jira notification intake | Not started | No webhook endpoint, Jira credentials, event validation, or issue lookup. |
| Jira comments | Not started | No Jira REST client or rendered result-comment templates. |
| Repository selection from Jira | Not started | No project-to-repository mapping or pinned-commit resolver. |
| Fix generation and validation | Not started | No patch schema, disposable patch worktree, or before/after verification gate. |
| Pull-request creation | Not started | No Git provider client, branch, commit, or PR workflow. |
| Jira backlink to PR | Not started | Depends on Jira comments and PR creation. |
| Queue, retries, deployment, observability | Not started | Current runner is local and synchronous. |

## Completed checkpoints

| Checkpoint | Evidence |
|---|---|
| Evidence core | Unit-tested deterministic score and atomic SHA-256 artifact manifest. |
| Hardened sandbox | Two matching live Docker executions of the sample regression test. |
| Agent-to-proof | Scripted candidate creates a `REPRODUCED` bundle with score 100. |
| Evidence console | Local dashboard renders a bundle and its audit timeline. |
| Replay and release gate | Independent replay produces two matching signatures; 3/3 frozen controls pass. |

## Remaining implementation plan

### Phase 6 - Jira intake and reproduction comments

Build a signed Jira webhook endpoint, idempotent job record, project/repository mapping, and Jira comment renderer. The first real checkpoint is a test webhook that produces either a `REPRODUCED` evidence comment or a transparent `NEED_INFO` / `INCONCLUSIVE` comment on a test issue.

### Phase 7 - Fix agent and sandbox validation

Add a separate, bounded patch-generation loop. A patch may only be generated after a verified reproduction. Each candidate patch must be applied to a disposable worktree and pass this gate: generated regression test fails before the patch, passes after the patch, and the selected existing test suite passes. The checkpoint is a fixture issue repaired without touching the source checkout.

### Phase 8 - Pull request and Jira backlink

Add a least-privilege Git provider adapter to create a branch, commit the accepted patch, open a PR, and send its URL plus evidence summary to Jira. The checkpoint is an end-to-end fixture run that creates a reviewable PR and leaves a Jira comment with the PR link.

### Phase 9 - Production controls and evaluation

Add a durable queue, retry and idempotency policy, webhook signature verification, secret storage, audit logging, rate limits, role-based approvals, hosted deployment, and a frozen 8-12 case evaluation set. The checkpoint is a deployed demo environment with a recorded end-to-end run and published evaluation results.

## What is deliberately not claimed

BugAgent is not yet a Jira bot, a code-fixing agent, or a PR automation product. It currently proves the middle of that future flow: **local ticket input -> sandboxed reproduction attempt -> evidence bundle -> independent replay and review**.
