# Run the untrusted half of a miner in a locked-down container.
#
#   ./research/run-docker.ps1                                          # AI-agent PRs (mine.py)
#   ./research/run-docker.ps1 -Study upgrades -Python 3.11             # dependency bumps (upgrades.py)
#   ./research/run-docker.ps1 -Only "BerriAI/litellm#48945"            # one PR
#
# Stage 1 (fetch) has network access to clone repos and pip-install wheels.
# Stage 2 (check) runs pipbreak - which imports those packages - with no network at all.
# Neither stage gets credentials or sees your files: only the sample (read-only), a Docker volume
# for checkouts, and the results folder.
param(
    [ValidateSet("agents", "upgrades", "known")][string]$Study = "agents",
    [string]$Python = "3.13",
    [string]$Only,
    [int]$Limit
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$image = "pipbreak-miner:py$Python"
$volume = "pipbreak-work-py$Python"
$script, $sample, $results = switch ($Study) {
    "agents"   { "/app/research/mine.py", "sample.json", "results" }
    "upgrades" { "/app/research/upgrades.py", "upgrades.json", "results-upgrades" }
    "known"    { "/app/research/known_breaks.py", "known_breaks.py", "results-known" }  # cases live in the script
}

docker build -q --build-arg "PYTHON=$Python" -t $image -f "$root\research\Dockerfile" $root | Out-Null
docker volume create $volume | Out-Null
New-Item -ItemType Directory -Force "$root\research\$results" | Out-Null

$sandbox = @(
    "--rm", "--init",
    "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--read-only", "--tmpfs", "/tmp:rw,size=2g",
    "--memory=6g", "--cpus=2", "--pids-limit=1024",
    "-v", "${volume}:/work",
    "-v", "$root\research\${sample}:/data/sample.json:ro"
)
$selection = @()
if ($Study -eq "known") {
    if ($Only) { $selection += @($Only) }
} else {
    if ($Only) { $selection += @("--only", $Only) }
    if ($Limit) { $selection += @("--limit", $Limit) }
}

Write-Host "== fetch (online: clone + install wheels) =="
docker run @sandbox $image $script fetch @selection

Write-Host "== check (offline: run pipbreak) =="
docker run @sandbox --network=none -v "$root\research\${results}:/results" $image $script check @selection
