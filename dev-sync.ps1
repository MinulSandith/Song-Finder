# Runs Song Finder with auto-reload and pulls new commits from GitHub every few seconds.
# Usage:  powershell -ExecutionPolicy Bypass -File dev-sync.ps1 [-Branch <name>] [-Interval 10] [-Port 8000]
param(
    [string]$Branch = "",
    [int]$Interval = 10,
    [int]$Port = 8000
)

Set-Location $PSScriptRoot

if ($Branch) {
    git fetch --quiet origin $Branch
    git switch $Branch
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not switch to '$Branch'. Run 'git status'."; exit 1 }
}
$Branch = git rev-parse --abbrev-ref HEAD

if (-not (Test-Path ".env")) {
    Write-Warning ".env not found - copy .env.example to .env and put your GEMINI_API_KEY in it."
}

$server = Start-Process python -ArgumentList "-m", "uvicorn", "backend.main:app", "--port", $Port, "--reload", "--reload-dir", "backend" -PassThru
Write-Host "App: http://127.0.0.1:$Port"
Write-Host "Following '$Branch', checking every $Interval s. Ctrl+C stops both."

try {
    while ($true) {
        git fetch --quiet origin $Branch
        $local = git rev-parse HEAD
        $remote = git rev-parse "origin/$Branch"
        if ($local -ne $remote) {
            git pull --ff-only --quiet origin $Branch
            if ($LASTEXITCODE -eq 0) {
                Write-Host "[$(Get-Date -Format HH:mm:ss)] Updated - refresh the browser:" -ForegroundColor Green
                git log --oneline "$local..HEAD"
                git diff --quiet $local HEAD -- requirements.txt
                if ($LASTEXITCODE -ne 0) { python -m pip install -r requirements.txt }
            } else {
                Write-Warning "Pull failed (local edits, or the branch was rewritten). Run 'git status'."
            }
        }
        Start-Sleep $Interval
    }
} finally {
    # /T also stops the reloader's child process, which holds the port.
    taskkill /PID $server.Id /T /F | Out-Null
}
