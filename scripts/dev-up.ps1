param(
  [switch]$Rebuild
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

# Ensure the regular worker is not running when using worker-dev replicas.
$workerContainerIdRaw = docker compose ps -q worker
$workerContainerId = if ($null -eq $workerContainerIdRaw) { "" } else { "$workerContainerIdRaw".Trim() }
if ($workerContainerId -ne "") {
  docker compose stop worker
  docker compose rm -f worker
}

if ($Rebuild) {
  # Rebuild only when dependencies/image inputs change (requirements/Dockerfile/base image).
  docker compose --profile dev build worker-dev
}

docker compose --profile dev up -d --scale worker-dev=3 `
  redis postgres api producer prometheus worker-dev
