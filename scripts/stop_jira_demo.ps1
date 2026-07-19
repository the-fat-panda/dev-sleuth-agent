[CmdletBinding()]
param(
    [string]$TunnelContainerName = "devsleuth-jira-demo-tunnel"
)

$ErrorActionPreference = "Stop"
$containerOutput = & docker ps --filter "name=^/$TunnelContainerName$" --format "{{.ID}}" | Select-Object -First 1
$containerId = if ($null -eq $containerOutput) { "" } else { ([string]$containerOutput).Trim() }
if ($containerId) {
    & docker stop $containerId | Out-Null
    Write-Host "Stopped the public Cloudflare demo tunnel."
} else {
    Write-Host "No running DevSleuthAgent demo tunnel was found."
}

Write-Host "The Jira webhook remains registered but points at the now-closed temporary URL. Run scripts\\start_jira_demo.ps1 before the next demo session to update it."
