# HTTP investigation API

This service is a thin FastAPI layer around the existing investigation engine. It accepts either a local source checkout or an explicitly allow-listed GitHub repository, starts the blocking model-and-sandbox work in a worker thread, and stores the resulting immutable bundle in the configured runs directory.

## Install and start

Install the optional API layer, then set all runtime configuration in the environment. The service validates the API key, Docker daemon, and immutable pytest image during startup; it will not accept jobs if any are unavailable.

```powershell
pip install -e ".[api]"
$env:OPENAI_API_KEY = "..."
$env:BUGAGENT_SANDBOX_IMAGE = $image
$env:BUGAGENT_MODEL = "gpt-5.6-terra"
$env:BUGAGENT_MAX_ATTEMPTS = "3"
$env:BUGAGENT_SANDBOX_TIMEOUT_SECONDS = "30"
$env:BUGAGENT_RUNS_ROOT = ".bugagent/runs"
$env:BUGAGENT_GITHUB_ALLOWED_REPOSITORIES = "the-fat-panda/e-commerce"
# Required only when cloning a private GitHub repository or creating future PRs.
$env:BUGAGENT_GITHUB_TOKEN = "..."
python -m bugagent.api
```

`BUGAGENT_MODEL`, `BUGAGENT_MAX_ATTEMPTS`, `BUGAGENT_SANDBOX_TIMEOUT_SECONDS`, and `BUGAGENT_RUNS_ROOT` have defaults. `OPENAI_API_KEY` and `BUGAGENT_SANDBOX_IMAGE` are required. The existing Responses client sends `store:false`.

Open `http://127.0.0.1:8001/app/` for the investigation workspace. It is served by the API so form submission, live progress, evidence, and history use the same origin. The older dependency-free evidence dashboard remains available through `python -m bugagent.web`.

## Start and poll an investigation

The repository source is deliberately an object with a `kind`. A GitHub source is checked against `BUGAGENT_GITHUB_ALLOWED_REPOSITORIES`, cloned only in the background worker, and pinned to the resulting full SHA before the unchanged engine starts. The temporary clone is removed after the investigation.

```json
POST /investigations
{
  "ticket": {
    "id": "LOCAL-1",
    "title": "Fresh records fail during normal close",
    "body": "A customer says the normal close action crashes on a new record before it has activity.",
    "repo_ref": "sandbox-live@fixture",
    "expected_error": "ZeroDivisionError"
  },
  "repository": {
    "kind": "local_path",
    "path": "C:/work/banklib",
    "commit": "a1b2c3d4"
  }
}
```

For the Mercato demo repository, submit a GitHub source instead of a local path:

```json
POST /investigations
{
  "ticket": {
    "id": "SCRUM-7",
    "title": "A customer reports that checkout completes with an incorrect total",
    "body": "It happens after a discount is applied. Please investigate the order flow.",
    "repo_ref": "the-fat-panda/e-commerce@backend-main"
  },
  "repository": {
    "kind": "github",
    "repository": "the-fat-panda/e-commerce",
    "ref": "backend-main"
  }
}
```

The service responds immediately with HTTP `202`:

```json
{
  "job_id": "e372d1a7-58d8-44e5-86cf-775a9550f3c2",
  "status": "queued",
  "status_url": "/investigations/e372d1a7-58d8-44e5-86cf-775a9550f3c2"
}
```

Poll the `status_url`. It reports `queued`, `running`, `done`, or `failed`. A completed job includes the immutable bundle run ID, a link to fetch it, and the verdict summary:

```json
{
  "job_id": "e372d1a7-58d8-44e5-86cf-775a9550f3c2",
  "status": "done",
  "run_id": "b1ac6fca-c727-4b4c-a46c-3d9dd6c334e0",
  "run_url": "/runs/b1ac6fca-c727-4b4c-a46c-3d9dd6c334e0",
  "verdict": {
    "status": "REPRODUCED",
    "score": 100,
    "rationale": ["..."]
  }
}
```

## Live progress stream

Each accepted response also contains an `events_url`. Connect to it with `EventSource` to receive retained Server-Sent Events. A reconnect can use the browser's `Last-Event-ID` header or an `after` query parameter to resume from an event sequence number.

```text
GET /investigations/{job_id}/events
event: progress
data: {"sequence":3,"stage":"form_hypothesis","state":"started","label":"Forming hypothesis","occurred_at":"...","attempt":1}
```

The API observes, but does not alter, the existing engine through adapters around the model client and Docker sandbox. It emits `started`, `completed`, or `failed` boundaries for these stages:

- `form_hypothesis`
- `candidate_sandbox`
- `replay_1`
- `replay_2`
- `verdict`

The browser workspace renders these events as a single focused live timeline with a ticking elapsed time. Job status polling remains active as the completion fallback.

## Endpoints

| Method | Path | Response |
|---|---|---|
| `POST` | `/investigations` | `202` with job ID and polling path; validates a local source or allowed GitHub repository before queuing. |
| `GET` | `/investigations/{job_id}` | Current in-process job state; completed jobs include the run ID and verdict summary, failed jobs include a safe error string. |
| `GET` | `/investigations/{job_id}/events` | Retained Server-Sent Event stream of API-layer stage progress for a job. |
| `GET` | `/runs` | Summaries of completed immutable bundles from `BUGAGENT_RUNS_ROOT`. |
| `GET` | `/runs/{run_id}` | Full JSON-safe artifact bundle: manifest, ticket, candidates, evidence, verdict, and timeline. |
| `GET` | `/` | Redirects to the web workspace. |
| `GET` | `/app/` | Responsive investigation workspace: submit, live progress, evidence, and history. |

The job registry is intentionally in-process for this first service layer. Restarting the API loses active-job state, although completed bundles remain available through the run endpoints. There is no authentication, durable queue, repository cloning, or replay endpoint yet.

## Optional Jira Cloud intake

The API can accept a signed Jira Cloud `jira:issue_created` webhook and comment the resulting evidence back to that issue. This integration is disabled unless all values below are supplied. Secrets stay in environment variables; never place them in a request or commit them.

```powershell
$env:BUGAGENT_JIRA_BASE_URL = "https://your-site.atlassian.net"
$env:BUGAGENT_JIRA_EMAIL = "you@example.com"
$env:BUGAGENT_JIRA_API_TOKEN = "..."
$env:BUGAGENT_JIRA_WEBHOOK_SECRET = "..."
$env:BUGAGENT_JIRA_PROJECT_SOURCES = '{"SCRUM":{"kind":"github","repo_ref":"the-fat-panda/e-commerce@backend-main","repository":"the-fat-panda/e-commerce","ref":"backend-main"}}'
```

`BUGAGENT_JIRA_PROJECT_SOURCES` maps a Jira project to either a `local_path` source or an allow-listed `github` source. The GitHub mapping above clones a clean disposable checkout and records the resolved commit SHA in the evidence bundle. Its repository must also appear in `BUGAGENT_GITHUB_ALLOWED_REPOSITORIES`.

Jira Cloud signs dynamic webhooks with an `X-Hub-Signature` HMAC header. Register `POST /integrations/jira/webhook` only at a publicly reachable **HTTPS** URL and use the same secret in Jira and `BUGAGENT_JIRA_WEBHOOK_SECRET`. The API returns promptly after queueing work, validates the `jira:issue_created` event and project mapping, and ignores a repeated delivery for the same issue while the process remains alive. A final evidence comment includes the verdict, score, candidate path, normalized observed result, and rationale.

| Method | Path | Response |
|---|---|---|
| `GET` | `/integrations/jira/status` | Whether Jira is configured and the configured project keys; never returns secrets. |
| `POST` | `/integrations/jira/webhook` | `202` with the linked investigation job for a valid signed `jira:issue_created` event; `401` for an invalid signature; `422` for an unsupported event or missing project mapping. |
