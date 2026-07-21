# DevSleuthAgent - Build Week demo script

## Submission facts to satisfy

- Track: **Developer Tools**.
- Upload a **public YouTube video under three minutes**. An unlisted public link is acceptable.
- The voiceover must explain what was built and how **both Codex and GPT-5.6** were used.
- Submit a code-repository URL. If it remains private, share it with `testing@devpost.com` and `build-week-event@openai.com` before the deadline.
- Include setup instructions, sample demo inputs, and the Codex/GPT-5.6 contribution in the README.

## Recording plan

Target **2:40 to 2:50**, leaving a safety margin below the three-minute cap. Record the individual scenes, then cut out model and sandbox wait time. Do not imply a stage was skipped: say that the video is edited for time and show the actual evidence and PR produced by the same run.

Use one known-good, unmerged planted bug. Start from clean History, a Jira ticket created during the recording, a browser signed in to the demo GitHub repository, and the DevSleuthAgent app already open. Turn YOLO mode on before the Jira ticket is created.

Avoid showing API keys, Jira tokens, terminal environment variables, or GitHub token settings.

## Timed voiceover and shots

| Time | Screen action | Voiceover |
|---|---|---|
| 0:00-0:14 | Title card, then DevSleuthAgent home screen. | "Bug reports are expensive because teams first have to decide whether the problem is real, reproduce it, and then safely turn a fix into a reviewable change. DevSleuthAgent closes that loop." |
| 0:14-0:31 | Show the Jira bug ticket. Keep the report intentionally non-technical. | "A customer reports that checkout fails when shipping to Ireland. There is no stack trace or file name. Creating this labelled Jira ticket is the only trigger." |
| 0:31-0:48 | Switch to Activity. Show the Jira-created job and the live stages: checkout, hypothesis, sandbox, replays. | "The signed webhook maps the Jira project to an allow-listed GitHub repository. The agent pins the branch to a commit, reads bounded source context, and works only in restricted Docker sandboxes." |
| 0:48-1:10 | Cut to the completed Evidence view. Expand Generated regression test, Sandbox result, and Independent replays. | "GPT-5.6 turns the vague report into a candidate regression test. But the model does not decide the outcome: DevSleuthAgent runs that test and requires two clean replays with the same failure before it calls the bug reproduced." |
| 1:10-1:28 | Highlight REPRODUCED, score, crash or assertion, and regression code. | "Here the evidence is reproducible: the generated test fails against the pinned commit, the two replays agree, and the evidence bundle records the test, sandbox result, score, and run ID." |
| 1:28-1:53 | Expand Local PR preparation. Show the green validated status, source diff, and regression test. | "Only after that proof does GPT-5.6 propose one minimal source patch. A separate validation gate proves the regression fails before the patch, passes after it, and the repository test suite still passes. The patch is validated in a disposable checkout; the original repository is never edited." |
| 1:53-2:15 | Show the GitHub draft PR: diff, generated test, branch, and draft state. | "With YOLO mode explicitly enabled, the validated plan creates a draft pull request, never a merge. Maintainers get a small, reviewable diff plus the regression test that proves both the failure and the fix." |
| 2:15-2:32 | Return to Jira; show the evidence comment and PR backlink. | "Jira receives the proof first and then the pull-request link, so the original reporter and the engineering team share one auditable trail from report to verified fix." |
| 2:32-2:48 | Show the collapsed workflow / a simple architecture slide. | "I used Codex to design and implement the service, API, integrations, UI, sandbox boundaries, and test suite. I used GPT-5.6 in the live investigation and fix-generation steps where code understanding and hypothesis formation are needed. The deterministic sandbox and replay gates keep that capability trustworthy." |
| 2:48-2:55 | Title card with repository URL and product name. | "DevSleuthAgent: from a vague Jira report to replayed evidence and a validated draft fix." |

## Editing notes

- Keep all voiceover at normal speed. It is fine to speed up or cut waiting time, as long as the before/after evidence shown comes from the same recorded ticket run.
- Prefer one happy-path ticket. Do not demonstrate an inconclusive ticket in the three-minute video; mention in the written description that non-reproductions result in an evidence comment rather than a speculative fix.
- Show the word **Draft** on GitHub. This reinforces that the agent does not merge or deploy.
- Do not spend time on setup forms, terminal commands, model request payloads, or typing.
- If a live run is slow, record the ticket intake and first live status separately, then cut to its final evidence bundle, PR, and Jira backlink after it completes.

## Read-aloud narration

Use this as one continuous narration. It is about 375 words, leaving only a small margin for pauses in a three-minute video.

> Every vague bug report creates the same costly question: is the problem real, can we reproduce it, and can we fix it without creating a new regression? DevSleuthAgent turns that uncertain handoff into a verified, reviewable workflow.
>
> Here is a real Jira ticket. A customer says checkout fails when shipping to Ireland. There is no stack trace, file name, or proposed fix. This labelled ticket is the only trigger.
>
> A signed webhook enters DevSleuthAgent. The orchestrator maps the project to an allow-listed GitHub repository, pins the exact commit, and starts the investigation. This is a local demo environment connected to real Jira, GitHub, GPT-5.6, and restricted Docker sandboxes.
>
> GPT-5.6 reads bounded repository context and turns that vague report into a candidate regression test. But the model never declares success by itself. The candidate runs in a no-network sandbox, then two clean replays. The verifier compares failure signatures and calculates the evidence score.
>
> If the proof threshold is not met, DevSleuthAgent stops and comments on Jira with what it tried. It never opens a speculative fix. In this run, the generated regression fails on the pinned commit, the replays agree, and the immutable evidence bundle records the test, sandbox output, verdict, and run ID.
>
> Only then does GPT-5.6 propose one minimal source-only patch. A separate sandbox gate requires the regression to fail before the patch, pass after it, and the repository suite to pass. Patch work stays in a disposable checkout, so the original source is never edited.
>
> With YOLO mode explicitly enabled, the validated plan creates a draft GitHub pull request, never a merge or deployment. Jira receives the evidence and the PR link.
>
> This diagram makes the trust boundary clear. Jira and GitHub are source systems. The orchestrator controls policy and pinned inputs. GPT-5.6 supplies code understanding and patch proposals. Sandboxes and replay gates supply deterministic proof. The evidence bundle is the audit record. In production, durable queueing, observability, role-based access, secrets, and approvals surround this path.
>
> I used Codex to build the service, integrations, interface, tests, and safety gates. I used GPT-5.6 in the live investigation and fix-generation steps where deep code reasoning matters. DevSleuthAgent does not guess a fix. It earns the right to propose one.

## Before upload checklist

- [ ] The video runs under 3:00 and has clear audio.
- [ ] Voiceover explicitly says both "Codex" and "GPT-5.6," and accurately describes their roles.
- [ ] The project is named **DevSleuthAgent**, not "Untitled."
- [ ] Category is **Developer Tools**.
- [ ] The repository link points to the current code, not an older incomplete revision.
- [ ] The README has local setup, Docker/Jira/GitHub configuration, demo ticket instructions, and a section on how Codex and GPT-5.6 were used.
- [ ] If the repository is private, it is shared with both required judging addresses.
- [ ] `/feedback` has been run in the primary Codex build session and its session ID is ready for the submission form.
- [ ] The YouTube link is public or unlisted, has finished processing, and is pasted into Devpost.
- [ ] The Devpost entry is marked **Submitted**, not merely saved as a draft.
