param(
    [string]$LiveContainer = "codex-lb",
    [string]$ImageTag = "codex-lb:patched-first-event-20260824",
    [string]$RollbackContainer = "codex-lb-pre-first-event-20260824"
)

$ErrorActionPreference = "Stop"
$CandidateContainer = "codex-lb-first-event-test-20260824"
$CandidateVolume = "codex-lb-first-event-test-20260824"
$LiveWasStopped = $false

function Remove-Candidate {
    docker rm -f $CandidateContainer 2>$null | Out-Null
    docker volume rm $CandidateVolume 2>$null | Out-Null
}

function Wait-Healthy([int]$Port) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($response.status -eq "ok") {
                return
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "CodexLB did not become healthy on port $Port"
}

if (-not (Test-Path "Dockerfile")) {
    throw "Run this script from the codex-lb-patched repository root."
}

$Revision = (git rev-parse HEAD).Trim()
$LiveVolume = docker inspect $LiveContainer --format '{{range .Mounts}}{{if eq .Destination "/var/lib/codex-lb"}}{{.Name}}{{end}}{{end}}'
if (-not $LiveVolume) {
    throw "The live container does not have a named volume mounted at /var/lib/codex-lb."
}
if (docker container inspect $RollbackContainer 2>$null) {
    throw "Rollback container $RollbackContainer already exists. Rename or remove it before deploying."
}

Write-Host "Building $ImageTag from $Revision"
docker build `
    --label "org.opencontainers.image.revision=$Revision" `
    --label "org.opencontainers.image.source=https://github.com/samochreno/codex-lb-patched" `
    -t $ImageTag .

Remove-Candidate
docker volume create $CandidateVolume | Out-Null
try {
    # The source is mounted read-only. REINDEX repairs the copied SQLite index
    # snapshot if the live WAL changes while Docker copies the files.
    docker run --rm `
        -v "${LiveVolume}:/from:ro" `
        -v "${CandidateVolume}:/to" `
        alpine:3.22 sh -c `
        'apk add --no-cache sqlite >/dev/null && cp -a /from/. /to/ && sqlite3 /to/store.db "REINDEX; PRAGMA quick_check;"' | Out-Host

    docker run -d `
        --name $CandidateContainer `
        --restart no `
        -p 4455:1455 `
        -p 3455:2455 `
        -v "${CandidateVolume}:/var/lib/codex-lb" `
        $ImageTag | Out-Null
    Wait-Healthy 3455
    Write-Host "Isolated candidate is healthy. Removing duplicated credentials."
} finally {
    Remove-Candidate
}

try {
    # Final cutover: this is the first point where the live service stops.
    docker stop $LiveContainer | Out-Null
    $LiveWasStopped = $true
    docker rename $LiveContainer $RollbackContainer
    docker run -d `
        --name $LiveContainer `
        --restart unless-stopped `
        -p 1455:1455 `
        -p 2455:2455 `
        -v "${LiveVolume}:/var/lib/codex-lb" `
        $ImageTag | Out-Null
    Wait-Healthy 2455
    Write-Host "Promoted $ImageTag. Dashboard: http://127.0.0.1:2455"
    Write-Host "Rollback container retained as $RollbackContainer"
} catch {
    if ($LiveWasStopped) {
        docker rm -f $LiveContainer 2>$null | Out-Null
        if (docker container inspect $RollbackContainer 2>$null) {
            docker rename $RollbackContainer $LiveContainer
            docker update --restart unless-stopped $LiveContainer | Out-Null
            docker start $LiveContainer | Out-Null
        }
    }
    throw
}
