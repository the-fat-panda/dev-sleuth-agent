[CmdletBinding()]
param(
    [string]$ProjectKey = "SCRUM",
    [string]$DemoLabel = "devsleuth-demo",
    [int]$ApiPort = 8001,
    [int]$GatewayPort = 8002,
    [string]$TunnelContainerName = "devsleuth-jira-demo-tunnel"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $repoRoot ".tools"
$tunnelImage = "cloudflare/cloudflared@sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d"
$webhookName = "DevSleuthAgent labelled demo intake ($ProjectKey)"

function Require-EnvironmentVariable {
    param([string]$Name)

    $item = Get-Item -Path "Env:$Name" -ErrorAction SilentlyContinue
    if ($null -eq $item -or [string]::IsNullOrWhiteSpace($item.Value)) {
        throw "Set $Name before starting the Jira demo."
    }
}

function Get-HttpStatusCode {
    param([string]$Url, [string]$Method = "GET")

    $arguments = @("--connect-timeout", "2", "--max-time", "4", "--silent", "--output", "NUL", "--write-out", "%{http_code}", "--request", $Method, $Url)
    if ($Method -eq "POST") {
        $arguments = @("--connect-timeout", "2", "--max-time", "4", "--silent", "--output", "NUL", "--write-out", "%{http_code}", "--request", "POST", "--header", "Content-Type: application/json", "--data", "{}", $Url)
    }
    return (& curl.exe @arguments).Trim()
}

function Wait-ForApi {
    param([string]$Url)

    for ($attempt = 0; $attempt -lt 20; $attempt += 1) {
        try {
            return Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 2
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "The DevSleuthAgent API did not become ready at $Url. Inspect .tools\\devsleuth-api-error.log."
}

function Start-LoopbackProcess {
    param([string]$Module, [string]$OutputName)

    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    $stdout = Join-Path $runtimeRoot "$OutputName-output.log"
    $stderr = Join-Path $runtimeRoot "$OutputName-error.log"
    return Start-Process -FilePath python -ArgumentList @("-m", $Module) -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
}

function Get-DockerLogsText {
    param([string]$ContainerId)

    if ($ContainerId -notmatch "^[a-f0-9]+$") {
        throw "Docker returned an invalid tunnel container ID."
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "docker"
    $startInfo.Arguments = "logs $ContainerId"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    if ($process.ExitCode -ne 0) {
        throw "docker logs failed for the demo tunnel: $stderr"
    }
    return "$stdout`n$stderr"
}

function Get-Webhooks {
    param([string]$BaseUrl, [hashtable]$Headers)

    $response = Invoke-RestMethod -Uri "$BaseUrl/rest/webhooks/1.0/webhook" -Method Get -Headers $Headers -TimeoutSec 15
    if ($response -is [System.Array]) {
        return @($response)
    }
    if ($null -ne $response.values) {
        return @($response.values)
    }
    return @($response)
}

function Get-WebhookId {
    param($Webhook)

    if ($null -ne $Webhook.id) {
        return [string]$Webhook.id
    }
    if ($Webhook.self -match "/(\d+)$") {
        return $Matches[1]
    }
    throw "Jira returned a matching webhook without an ID."
}

if ($ProjectKey -notmatch "^[A-Z][A-Z0-9_]{1,9}$") {
    throw "ProjectKey must be a Jira-style uppercase project key."
}
if ($DemoLabel -notmatch "^[A-Za-z0-9_.-]+$") {
    throw "DemoLabel may contain only letters, numbers, dot, underscore, or hyphen."
}

foreach ($name in @(
        "OPENAI_API_KEY",
        "BUGAGENT_SANDBOX_IMAGE",
        "BUGAGENT_JIRA_BASE_URL",
        "BUGAGENT_JIRA_EMAIL",
        "BUGAGENT_JIRA_API_TOKEN",
        "BUGAGENT_JIRA_WEBHOOK_SECRET",
        "BUGAGENT_JIRA_PROJECT_SOURCES",
        "BUGAGENT_GITHUB_ALLOWED_REPOSITORIES"
    )) {
    Require-EnvironmentVariable $name
}

$apiStatusUrl = "http://127.0.0.1:$ApiPort/integrations/jira/status"
try {
    $jiraStatus = Invoke-RestMethod -Uri $apiStatusUrl -Method Get -TimeoutSec 2
} catch {
    $apiProcess = Start-LoopbackProcess "bugagent.api" "devsleuth-api"
    Write-Host "Started DevSleuthAgent API process $($apiProcess.Id)."
    $jiraStatus = Wait-ForApi $apiStatusUrl
}
if (-not $jiraStatus.configured -or $jiraStatus.project_keys -notcontains $ProjectKey) {
    throw "The API is not configured for Jira project $ProjectKey. Check BUGAGENT_JIRA_PROJECT_SOURCES."
}

$gatewayRootUrl = "http://127.0.0.1:$GatewayPort/"
if ((Get-HttpStatusCode $gatewayRootUrl) -eq "000") {
    $gatewayProcess = Start-LoopbackProcess "bugagent.jira_gateway" "devsleuth-jira-gateway"
    Write-Host "Started webhook-only gateway process $($gatewayProcess.Id)."
    for ($attempt = 0; $attempt -lt 15 -and (Get-HttpStatusCode $gatewayRootUrl) -eq "000"; $attempt += 1) {
        Start-Sleep -Seconds 1
    }
}
if ((Get-HttpStatusCode $gatewayRootUrl) -ne "404") {
    throw "The webhook-only gateway is not healthy on port $GatewayPort."
}
if ((Get-HttpStatusCode "http://127.0.0.1:$GatewayPort/integrations/jira/webhook" "POST") -ne "401") {
    throw "The gateway did not reject an unsigned webhook. Refusing to expose it publicly."
}

$containerOutput = & docker ps --filter "name=^/$TunnelContainerName$" --format "{{.ID}}" | Select-Object -First 1
$containerId = if ($null -eq $containerOutput) { "" } else { ([string]$containerOutput).Trim() }
if (-not $containerId) {
    $containerOutput = & docker run -d --rm --name $TunnelContainerName $tunnelImage tunnel --url "http://host.docker.internal:$GatewayPort"
    $containerId = if ($null -eq $containerOutput) { "" } else { ([string]$containerOutput).Trim() }
    if (-not $containerId) {
        throw "Docker did not start the Cloudflare demo tunnel."
    }
}

$tunnelUrl = $null
for ($attempt = 0; $attempt -lt 45 -and -not $tunnelUrl; $attempt += 1) {
    # cloudflared writes normal startup logs to stderr, including the URL.
    $logs = Get-DockerLogsText $containerId
    $match = [regex]::Match($logs, "https://[a-z0-9-]+\.trycloudflare\.com", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($match.Success) {
        $tunnelUrl = $match.Value.ToLowerInvariant()
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $tunnelUrl) {
    throw "Cloudflare did not provide a Quick Tunnel URL. Inspect: docker logs $containerId"
}

$baseUrl = $env:BUGAGENT_JIRA_BASE_URL.TrimEnd("/")
$credentialBytes = [Text.Encoding]::UTF8.GetBytes("$($env:BUGAGENT_JIRA_EMAIL):$($env:BUGAGENT_JIRA_API_TOKEN)")
$headers = @{
    Authorization = "Basic $([Convert]::ToBase64String($credentialBytes))"
    Accept = "application/json"
}
$webhookPayload = @{
    name = $webhookName
    description = "Temporary signed intake for labelled DevSleuthAgent demo tickets."
    url = "$tunnelUrl/integrations/jira/webhook"
    events = @("jira:issue_created")
    filters = @{ "issue-related-events-section" = "project = $ProjectKey AND labels = $DemoLabel" }
    excludeBody = $false
    secret = $env:BUGAGENT_JIRA_WEBHOOK_SECRET
}
$body = $webhookPayload | ConvertTo-Json -Depth 5 -Compress
$matches = @(Get-Webhooks $baseUrl $headers | Where-Object { $_.name -eq $webhookName })
if ($matches.Count -gt 1) {
    throw "Found multiple Jira webhooks named '$webhookName'. Remove duplicates before continuing."
}
if ($matches.Count -eq 1) {
    $webhookId = Get-WebhookId $matches[0]
    $registered = Invoke-RestMethod -Uri "$baseUrl/rest/webhooks/1.0/webhook/$webhookId" -Method Put -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 20
    $registrationAction = "updated"
} else {
    $registered = Invoke-RestMethod -Uri "$baseUrl/rest/webhooks/1.0/webhook" -Method Post -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 20
    $webhookId = Get-WebhookId $registered
    $registrationAction = "created"
}

$state = @{
    tunnel_container = $TunnelContainerName
    tunnel_url = $tunnelUrl
    webhook_id = $webhookId
    webhook_name = $webhookName
    project_key = $ProjectKey
    demo_label = $DemoLabel
    registered_at = (Get-Date).ToUniversalTime().ToString("o")
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$state | ConvertTo-Json | Set-Content -Path (Join-Path $runtimeRoot "jira-demo-state.json") -Encoding utf8

Write-Host "Jira webhook ${registrationAction}: $webhookId"
Write-Host "Demo filter: project = $ProjectKey AND labels = $DemoLabel"
Write-Host "Webhook URL: $($webhookPayload.url)"
Write-Host "Create a new $ProjectKey issue with label '$DemoLabel' to start one live investigation."
