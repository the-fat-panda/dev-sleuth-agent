# DevSleuthAgent setup and judge guide

This repository contains DevSleuthAgent, the Jira to evidence to validated draft-PR service. The separate [e-commerce demo target](https://github.com/the-fat-panda/e-commerce) supplies intentionally planted bugs for the recorded workflow.

No OpenAI, Jira, GitHub, or webhook credentials are included in this repository. Do not commit credentials to a fork or clone.

## Supported platform

The demonstrated configuration is Windows 10 or 11 with PowerShell, Python 3.11 or newer, Git, and Docker Desktop. The service is Python and Docker based, so it can also be adapted to macOS or Linux with equivalent tooling, but those environments were not part of the recorded demo verification.

## Quick verification without credentials

Clone the repository, create a virtual environment, and install the API dependencies.

```powershell
git clone https://github.com/the-fat-panda/dev-sleuth-agent.git
Set-Location dev-sleuth-agent

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[api]"
```

Run the deterministic test suite.

```powershell
python -m unittest discover -s tests
```

The suite contains 69 tests and does not make a live model request. It covers evidence scoring, replay, sandbox policy, API behavior, Jira signature verification, GitHub controls, fix validation, and publication job state.

You can also write a deterministic example evidence bundle without a model call.

```powershell
python -m bugagent demo --output .bugagent/runs
```

## Live configuration

The live API starts only after it can find an OpenAI API key, reach Docker, and find the configured immutable pytest image. All commands below set values only for the current PowerShell session. Use a password manager or your organization's secret manager for persistent credentials, never a committed `.env` file.

### 1. Build the sandbox image

Docker Desktop must be running. Build the local pytest image and capture its immutable image ID.

```powershell
docker build -f containers/python-pytest.Dockerfile -t bugagent-python-pytest:dev .
$image = docker image inspect bugagent-python-pytest:dev --format '{{.Id}}'
```

Set `BUGAGENT_SANDBOX_IMAGE` to `$image`, not to the mutable image tag. The API refuses to start if Docker is unavailable or this image ID cannot be found.

### 2. Create an OpenAI API key

1. Go to the [OpenAI API key page](https://platform.openai.com/api-keys) and select the API project that should own the usage.
2. Select **Create new secret key**, choose the narrowest permissions that allow Responses API usage, and copy the key immediately.
3. Keep the key in a password manager or secret manager. It cannot be recovered from this repository.

Set the core variables. The values below are safe dummy values except for `$image`, which was created locally in the previous step.

```powershell
$env:OPENAI_API_KEY = "sk-proj-REPLACE_WITH_YOUR_KEY"
$env:BUGAGENT_SANDBOX_IMAGE = $image
$env:BUGAGENT_MODEL = "gpt-5.6-terra"
$env:BUGAGENT_MAX_ATTEMPTS = "3"
$env:BUGAGENT_SANDBOX_TIMEOUT_SECONDS = "30"
$env:BUGAGENT_RUNS_ROOT = ".bugagent/runs"
```

`OPENAI_API_KEY` and `BUGAGENT_SANDBOX_IMAGE` are required for a live run. `BUGAGENT_MODEL` defaults to `gpt-5.6-terra` if omitted. The client sends Responses API requests with `store: false`.

### 3. Configure one GitHub repository

GitHub configuration is optional for the local banklib fixture. It is required to investigate a GitHub repository, and a GitHub token plus explicit publishing opt-in are required to create draft pull requests.

1. In GitHub, open **Profile picture > Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**.
2. Select the account or organization that owns the target repository. Under **Repository access**, select **Only select repositories** and choose the one target repository.
3. Under repository permissions, grant **Contents: Read and write** and **Pull requests: Read and write**. Metadata read access is included by GitHub. Do not grant organization-wide or unrelated repository access.
4. Generate the token, copy it once, and store it securely. If the organization requires approval, wait for approval before testing.

Use these dummy values, replacing `YOUR_OWNER`, `YOUR_REPOSITORY`, and the token.

```powershell
$env:BUGAGENT_GITHUB_ALLOWED_REPOSITORIES = "YOUR_OWNER/YOUR_REPOSITORY"
$env:BUGAGENT_GITHUB_TOKEN = "github_pat_REPLACE_WITH_YOUR_TOKEN"
$env:BUGAGENT_GITHUB_PR_PUBLISH_ENABLED = "false"
```

For example, a single permitted repository is `the-fat-panda/e-commerce`. The allow-list accepts comma-separated `owner/repository` values. Keep publishing set to `false` until a validated local plan is ready. Set it to `true` only when deliberately allowing draft PR creation:

```powershell
$env:BUGAGENT_GITHUB_PR_PUBLISH_ENABLED = "true"
```

### 4. Configure Jira Cloud

Jira is optional for direct API or CLI use. Configure all five Jira variables together to use signed issue-created webhooks and post result comments.

1. Set `BUGAGENT_JIRA_BASE_URL` to the browser URL of the Jira Cloud site, for example `https://your-company.atlassian.net`. Do not add a trailing slash.
2. Set `BUGAGENT_JIRA_EMAIL` to the Atlassian-account email that can view the target project and post issue comments. A Jira administrator may be needed to register webhooks.
3. Create `BUGAGENT_JIRA_API_TOKEN` at [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens). Name it `DevSleuthAgent`, choose a sensible expiry, copy it once, and store it securely.
4. Generate `BUGAGENT_JIRA_WEBHOOK_SECRET` locally. This secret is sent to Jira when the webhook is registered and is used to verify the `X-Hub-Signature` HMAC on every delivery.
5. Set `BUGAGENT_JIRA_PROJECT_SOURCES` to the JSON mapping from a Jira project key to its approved repository.

Generate a webhook secret.

```powershell
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$env:BUGAGENT_JIRA_WEBHOOK_SECRET = [Convert]::ToBase64String($bytes)
$rng.Dispose()
```

Set the remaining Jira values. This example maps the `SCRUM` Jira project to the `main` branch of one GitHub repository.

```powershell
$env:BUGAGENT_JIRA_BASE_URL = "https://your-company.atlassian.net"
$env:BUGAGENT_JIRA_EMAIL = "you@example.com"
$env:BUGAGENT_JIRA_API_TOKEN = "ATATT3xFfREPLACE_WITH_YOUR_TOKEN"
$env:BUGAGENT_JIRA_PROJECT_SOURCES = '{"SCRUM":{"kind":"github","repo_ref":"YOUR_OWNER/YOUR_REPOSITORY@main","repository":"YOUR_OWNER/YOUR_REPOSITORY","ref":"main"}}'
```

`repo_ref` is the human-readable source label recorded in evidence. `repository` must also appear in `BUGAGENT_GITHUB_ALLOWED_REPOSITORIES`. Change `SCRUM` and `main` to your Jira project key and target branch. The mapping can contain more than one project.

### 5. Start the service

Start the service and open the local UI.

```powershell
python -m bugagent.api
```

Open `http://127.0.0.1:8001/app/`. Startup checks validate the OpenAI key configuration, Docker, the immutable sandbox image, and configured integration syntax before accepting work.

## Run one live local investigation

With the variables above set, this uses GPT-5.6 and the Docker sandbox against the included banklib fixture. It makes a real OpenAI API request using your own key.

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

The command writes an immutable evidence bundle under `.bugagent/runs`. A successful live run should identify the fixture's `ZeroDivisionError` and produce matching clean replays.

## Optional Jira and GitHub integration demo

After completing the GitHub and Jira configuration above, the labelled demo helper registers a signed Jira webhook through a temporary Cloudflare Quick Tunnel.

```powershell
.\scripts\start_jira_demo.ps1
```

Only newly created issues matching `project = SCRUM AND labels = devsleuth-demo` are accepted by the demo configuration. Use a disposable repository and test Jira project. The service validates a patch locally before it can create a draft pull request. It never merges or deploys code.

## Configuration reference

| Variable | Required when | Safe dummy value or format | How to obtain it |
|---|---|---|---|
| `OPENAI_API_KEY` | Any live model investigation or fix | `sk-proj-REPLACE_WITH_YOUR_KEY` | Create a project API key in the OpenAI API platform. |
| `BUGAGENT_MODEL` | Optional | `gpt-5.6-terra` | Use the model available to your OpenAI project. |
| `BUGAGENT_SANDBOX_IMAGE` | Any live API run | `sha256:<local-image-id>` | Build the included Dockerfile, then inspect the local image ID. |
| `BUGAGENT_GITHUB_ALLOWED_REPOSITORIES` | GitHub source or PR workflow | `YOUR_OWNER/YOUR_REPOSITORY` | Choose the exact GitHub repository or comma-separated allow-list. |
| `BUGAGENT_GITHUB_TOKEN` | Private GitHub checkout or draft PR creation | `github_pat_REPLACE_WITH_YOUR_TOKEN` | Create a fine-grained GitHub PAT limited to the selected repository. |
| `BUGAGENT_GITHUB_PR_PUBLISH_ENABLED` | Draft PR creation | `false` or `true` | Set `true` only after intentionally enabling draft PR publication. |
| `BUGAGENT_JIRA_BASE_URL` | Jira integration | `https://your-company.atlassian.net` | Copy the Jira Cloud site URL from your browser. |
| `BUGAGENT_JIRA_EMAIL` | Jira integration | `you@example.com` | Use the email on the Atlassian account that owns the token. |
| `BUGAGENT_JIRA_API_TOKEN` | Jira integration | `ATATT3xFfREPLACE_WITH_YOUR_TOKEN` | Create an Atlassian API token. |
| `BUGAGENT_JIRA_PROJECT_SOURCES` | Jira integration | JSON project-to-repository mapping | Write the mapping shown in step 4. |
| `BUGAGENT_JIRA_WEBHOOK_SECRET` | Jira integration | Random Base64 string | Generate locally with the PowerShell command in step 4. |

See [README.md](README.md) for the complete API, Jira, GitHub, security, and architecture reference.
