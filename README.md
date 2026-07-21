# DevSleuthAgent

DevSleuthAgent investigates bug tickets against a pinned code revision. It uses GPT-5.6 to propose a regression test and, after a verified reproduction, a source fix. The model does not decide the result. Restricted Docker execution, independent replays, and a deterministic scoring rubric decide the verdict. A reproduced bug can continue through local fix validation to a GitHub draft pull request, with evidence and the PR link posted back to Jira.

## How it works

1. Jira sends a signed issue-created webhook. The Jira route verifies the HMAC signature and maps the Jira project to a configured repository source.
2. The API starts a background job. For a GitHub source, it clones the allowed repository, resolves the requested ref to a full commit SHA, and records that SHA in the evidence bundle.
3. `ReadOnlyRepository` builds bounded context from the repository. It includes selected source, public API information, usage snippets, and supported contract files. It excludes secret-like paths and dependency directories.
4. GPT-5.6 proposes one regression test. Candidate validation restricts the test to `tests/bugagent_generated/` and rejects unsafe imports and calls before execution.
5. `DockerSandbox` collects and runs the candidate in a restricted container. A clean failure is replayed twice in fresh sandbox executions.
6. `bugagent/scoring.py` decides the result from execution facts. The outcomes are `REPRODUCED`, `NOT_REPRODUCED` or `INCONCLUSIVE`, and `NEED_INFO`. The agent abstains when it cannot prove an application-level defect.
7. Only a `REPRODUCED` run can enter the fix workflow. GPT-5.6 proposes a source edit. A disposable checkout proves the regression fails before the patch, passes after the patch, and that the repository test suite passes.
8. A validated plan can create a GitHub draft PR when publishing is explicitly enabled. Jira receives an evidence comment for every completed ticket and a second comment with the draft PR link when publication succeeds.

## How Codex and GPT-5.6 were used

OpenAI Codex was used during the hackathon to design, implement, test, and refine the service layer, web UI, Jira intake, GitHub draft PR flow, sandbox validation, and evidence reporting.

GPT-5.6 is used at runtime in two constrained roles:

- `ResponsesInvestigationClient` in `bugagent/agent/client.py` proposes a candidate regression test.
- `ResponsesFixClient` in `bugagent/fix.py` proposes a bounded source edit after a reproduced run.

Both clients use the Responses API with `store: false` and strict structured output. Neither client has direct shell, Docker, Jira, GitHub, or repository write access. The model proposes. Sandbox execution and the scoring rubric decide whether a bug is reproduced. Patch validation decides whether a fix is safe to prepare for review.

```text
Codex /feedback session ID: 019f6e4f-b3bb-7253-b7ea-75ed798f0b7e
```

## Architecture

| Component | Modules | Responsibility |
|---|---|---|
| HTTP service and jobs | `bugagent/api.py`, `bugagent/api_jobs.py`, `bugagent/api_progress.py`, `bugagent/api_repositories.py` | FastAPI endpoints, background workers, progress events, local and GitHub source resolution. |
| Investigation engine | `bugagent/agent/orchestrator.py`, `bugagent/agent/repository.py`, `bugagent/agent/client.py` | Bounded context, candidate validation, GPT-5.6 test proposals, and the investigation loop. |
| Sandbox | `bugagent/sandbox/policy.py`, `bugagent/sandbox/docker.py` | `Sandbox` protocol and the current Docker implementation. |
| Evidence and replay | `bugagent/scoring.py`, `bugagent/silent_output.py`, `bugagent/artifacts.py`, `bugagent/replay.py` | Deterministic verdicts, contract-backed silent-output checks, immutable bundles, and independent replay. |
| Jira | `bugagent/jira.py`, `bugagent/jira_api.py`, `bugagent/jira_gateway.py` | Jira configuration, signature validation, webhook intake, comments, and the narrow loopback gateway. |
| Fix and publication | `bugagent/fix.py`, `bugagent/fix_jobs.py`, `bugagent/publish_jobs.py`, `bugagent/github.py` | Patch validation, local PR plans, GitHub draft PR publication, and publication status. |
| Web UI | `ui/index.html`, `ui/app.js`, `ui/styles.css` | New investigations, live activity, evidence, fix status, publication status, and history. |

The API is implemented in `bugagent/api.py` and listens on loopback port `8001`. The public Jira tunnel targets the separate gateway in `bugagent/jira_gateway.py`, which listens on loopback port `8002` by default and exposes only the signed webhook route.

## Requirements

- Python 3.11 or newer.
- Docker Desktop or Docker Engine running locally. The API checks Docker and the configured image during startup.
- An OpenAI API key for live investigation and fix preparation.
- Git for GitHub-backed investigations and draft PR publication.
- The target repository must run inside the configured pytest image.

The package metadata and optional API dependencies are defined in `pyproject.toml`. The API extra installs FastAPI, Starlette, and Uvicorn.

### Environment variables

The API reads the following core variables. The first two are required to start the API.

```text
OPENAI_API_KEY
BUGAGENT_SANDBOX_IMAGE
BUGAGENT_MODEL
BUGAGENT_MAX_ATTEMPTS
BUGAGENT_SANDBOX_TIMEOUT_SECONDS
BUGAGENT_RUNS_ROOT
```

The defaults are shown below.

```text
BUGAGENT_MODEL=gpt-5.6-terra
BUGAGENT_MAX_ATTEMPTS=3
BUGAGENT_SANDBOX_TIMEOUT_SECONDS=30
BUGAGENT_RUNS_ROOT=.bugagent/runs
```

GitHub-backed sources and draft PR publication use these variables.

```text
BUGAGENT_GITHUB_ALLOWED_REPOSITORIES
BUGAGENT_GITHUB_TOKEN
BUGAGENT_GITHUB_PR_PUBLISH_ENABLED
```

Jira intake uses these variables when enabled.

```text
BUGAGENT_JIRA_BASE_URL
BUGAGENT_JIRA_EMAIL
BUGAGENT_JIRA_API_TOKEN
BUGAGENT_JIRA_WEBHOOK_SECRET
BUGAGENT_JIRA_PROJECT_SOURCES
```

The narrow Jira gateway has optional settings.

```text
BUGAGENT_JIRA_WEBHOOK_UPSTREAM
BUGAGENT_JIRA_WEBHOOK_GATEWAY_PORT
```

## Setup and run

The commands below use PowerShell from the repository root.

1. Create a virtual environment and install the API extra.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[api]"
```

2. Build the local pytest image and capture its immutable image ID. The sandbox does not pull images at run time.

```powershell
docker build -f containers/python-pytest.Dockerfile -t bugagent-python-pytest:dev .
$image = docker image inspect bugagent-python-pytest:dev --format '{{.Id}}'
```

3. Set the required API configuration.

```powershell
$env:OPENAI_API_KEY = "<YOUR_OPENAI_API_KEY>"
$env:BUGAGENT_SANDBOX_IMAGE = $image
$env:BUGAGENT_MODEL = "gpt-5.6-terra"
$env:BUGAGENT_MAX_ATTEMPTS = "3"
$env:BUGAGENT_SANDBOX_TIMEOUT_SECONDS = "30"
$env:BUGAGENT_RUNS_ROOT = ".bugagent/runs"
```

4. Start the API and open the UI.

```powershell
python -m bugagent.api
```

Open `http://127.0.0.1:8001/app/` in a browser. API startup fails if the API key is missing, Docker is unreachable, or the configured immutable image is unavailable.

## Trying it

### Run a live CLI investigation

The CLI investigation command uses the real Responses client and the Docker sandbox. This example uses the included local fixture.

```powershell
python -m bugagent investigate `
  --ticket-id BANK-1 `
  --title "Close fails for a newly created account" `
  --body "A customer reports that closing a new account sometimes fails." `
  --repo-ref "sandbox-live@fixture" `
  --repo fixtures/sandbox_live `
  --commit "fixture" `
  --image $image
```

The command writes an immutable bundle under the configured output directory. It prints the run ID, verdict, score, candidate test, rationale, and artifact path.

The CLI also has a deterministic demo command. It writes a prepared `REPRODUCED` bundle and does not make a model request.

```powershell
python -m bugagent demo --output .bugagent/runs
```

### Submit through the HTTP API

This example starts an investigation against the included fixture through the API.

```powershell
$fixture = (Resolve-Path "fixtures/sandbox_live").Path
$payload = @{
  ticket = @{
    id = "BANK-2"
    title = "Close fails for a newly created account"
    body = "A customer reports that closing a new account sometimes fails."
    repo_ref = "sandbox-live@fixture"
  }
  repository = @{
    kind = "local_path"
    path = $fixture
    commit = "fixture"
  }
} | ConvertTo-Json -Depth 6

$job = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8001/investigations" `
  -Method Post `
  -ContentType "application/json" `
  -Body $payload

$job
Invoke-RestMethod -Uri "http://127.0.0.1:8001$($job.status_url)"
```

The `POST` response is `202 Accepted`. Poll the returned status URL until the job is `done` or `failed`. Completed runs are available through the returned run URL and the `/runs` endpoint.

### Submit through the UI

Open `http://127.0.0.1:8001/app/`. In New Investigation, provide the ticket fields and either a local repository path with a commit label or an allow-listed GitHub repository and ref. The Activity view shows background progress. The Evidence view shows the generated test, sandbox result, replays, verdict, and score. A reproduced GitHub-backed run can enter local fix validation from the same UI.

## Jira integration

Jira sends `jira:issue_created` events to the webhook gateway. The gateway forwards only the request body and Jira signature to the loopback API. The API verifies the `X-Hub-Signature` HMAC before it accepts the ticket.

The included demo helper registers one Jira webhook. Its default filter is shown below.

```text
project = SCRUM AND labels = devsleuth-demo
```

This label gate makes demo intake opt-in. When the helper is used, only newly created Jira issues that match the project and label filter reach the agent.

Set Jira configuration without committing secrets.

```powershell
$env:BUGAGENT_JIRA_BASE_URL = "https://<YOUR-SITE>.atlassian.net"
$env:BUGAGENT_JIRA_EMAIL = "<YOUR-JIRA-EMAIL>"
$env:BUGAGENT_JIRA_API_TOKEN = "<YOUR-JIRA-API-TOKEN>"
$env:BUGAGENT_JIRA_WEBHOOK_SECRET = "<YOUR-WEBHOOK-SECRET>"
$env:BUGAGENT_JIRA_PROJECT_SOURCES = '{"SCRUM":{"kind":"github","repo_ref":"<OWNER>/<REPOSITORY>@<BRANCH>","repository":"<OWNER>/<REPOSITORY>","ref":"<BRANCH>"}}'
$env:BUGAGENT_GITHUB_ALLOWED_REPOSITORIES = "<OWNER>/<REPOSITORY>"
```

Start or update the labelled demo webhook from an elevated PowerShell session.

```powershell
.\scripts\start_jira_demo.ps1
```

The helper starts or reuses the loopback API and webhook gateway, starts a Cloudflare Quick Tunnel in Docker, and creates or updates one Jira webhook. The Quick Tunnel URL changes when the tunnel restarts. Run the helper again before the next demo session.

Stop the public demo tunnel when the session ends.

```powershell
.\scripts\stop_jira_demo.ps1
```

## Testing

Run the test suite from the repository root.

```powershell
python -m unittest discover -s tests
```

The suite contains 69 tests at the time this README was written. It covers the crash reproduction path, the contract-backed silent-output path, candidate validation, the Docker sandbox policy, artifacts and replay, the API, Jira integration, GitHub controls, fix validation, and publication job state.

## Repository access for judges

```text
Project source repository: https://github.com/the-fat-panda/dev-sleuth-agent
Demo target repository: https://github.com/the-fat-panda/e-commerce
Judge access status: <CONFIRM THAT testing@devpost.com AND build-week-event@openai.com HAVE BEEN GRANTED ACCESS>
```

For a private submission, share repository access with both required judging accounts before submitting. Replace the status placeholder only after access is confirmed.

## Limitations

- The target repository must execute in the configured pytest image. Dependencies that are unavailable in that image can prevent investigation.
- Silent wrong-output bugs need a supported repository-owned contract and a valid public API probe. Otherwise the agent returns an abstaining result instead of treating a generated assertion as proof.
- Active investigation, fix, and publication job state is in process. Restarting the API loses active job state and live events. Completed bundles and persisted PR records remain on disk.
- The main API has no general API authentication and is intended to remain on loopback. The public demo path is limited to the Jira webhook gateway.

## Fact check

The README facts above were checked against the following repository sources:

- Python version, optional API dependencies, and console scripts: `pyproject.toml`.
- CLI commands and flags: `bugagent/cli.py`.
- Default model, API configuration, API port, and endpoints: `bugagent/agent/client.py` and `bugagent/api.py`.
- Sandbox image build command and restrictions: `containers/python-pytest.Dockerfile`, `bugagent/sandbox/policy.py`, and `bugagent/sandbox/docker.py`.
- Jira variables, signature handling, gateway port, and labelled demo setup: `bugagent/jira.py`, `bugagent/jira_api.py`, `bugagent/jira_gateway.py`, and `scripts/start_jira_demo.ps1`.
- GitHub controls and fix validation: `bugagent/github.py` and `bugagent/fix.py`.
- Test count: `python -m unittest discover -s tests` returned 69 passing tests.
