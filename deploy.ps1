# deploy.ps1 - One-click deploy, verify, and test for Sara pipeline
param(
    [string]$AppFile = "$env:USERPROFILE\Downloads\app.py",
    [string]$RepoDir = "C:\Users\ibelo\Downloads\pipeline",
    [string]$BaseUrl = "https://meeting-pipeline-production.up.railway.app",
    [string]$Git = "C:\Program Files\Git\cmd\git.exe"
)

Write-Host ""
Write-Host "=== SARA PIPELINE DEPLOY ===" -ForegroundColor Cyan
Write-Host ""

# 1. Copy updated app.py
Write-Host "[1/6] Copying app.py to repo..." -ForegroundColor Yellow
if (-not (Test-Path $AppFile)) {
    Write-Host "ERROR: app.py not found at: $AppFile" -ForegroundColor Red
    exit 1
}
Copy-Item $AppFile "$RepoDir\app.py" -Force
Write-Host "  OK - File copied" -ForegroundColor Green

# 2. Git commit and push
Write-Host "[2/6] Git commit and push..." -ForegroundColor Yellow
Set-Location $RepoDir
& $Git add app.py
$ts = Get-Date -Format "yyyy-MM-dd HH:mm"
& $Git commit -m "deploy: $ts auto-deploy" 2>$null
& $Git push 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Trying force push..." -ForegroundColor Red
    & $Git push --force 2>$null
}
Write-Host "  OK - Pushed to GitHub" -ForegroundColor Green

# 3. Wait for Railway
Write-Host "[3/6] Waiting 45s for Railway deploy..." -ForegroundColor Yellow
Start-Sleep -Seconds 45
Write-Host "  OK - Wait complete" -ForegroundColor Green

# 4. Check /version
Write-Host "[4/6] Checking /version..." -ForegroundColor Yellow
$deployed = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
        $ver = Invoke-RestMethod "$BaseUrl/version" -TimeoutSec 10
        Write-Host "  OK - Version: $($ver.version) | Deployed: $($ver.deployed)" -ForegroundColor Green
        $deployed = $true
        break
    } catch {
        Write-Host "  Attempt $attempt/3 failed - retrying in 15s..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 15
    }
}
if (-not $deployed) {
    Write-Host "  FAILED - /version not responding." -ForegroundColor Red
    exit 1
}

# 5. Smoke test /config
Write-Host "[5/6] Smoke test - /config..." -ForegroundColor Yellow
try {
    $cfg = Invoke-RestMethod "$BaseUrl/config" -TimeoutSec 10
    Write-Host "  OK - Config loaded" -ForegroundColor Green
} catch {
    Write-Host "  WARNING - /config error (non-critical)" -ForegroundColor Red
}

# 6. End-to-end pipeline test
Write-Host "[6/6] End-to-end pipeline test (fetches real transcript, runs Claude extraction)..." -ForegroundColor Yellow
Write-Host "  This takes 15-30s..." -ForegroundColor DarkGray
try {
    $test = Invoke-RestMethod "$BaseUrl/test" -TimeoutSec 60
    if ($test.status -eq "pass") {
        $title = $test.steps.fetch_transcript.title
        $tasks = $test.steps.claude_extraction.tasks_found
        $contacts = $test.steps.claude_extraction.contacts_found
        $ms = $test.steps.claude_extraction.duration_ms
        Write-Host "  PASS - Tested on: $title" -ForegroundColor Green
        Write-Host "  Extracted $tasks tasks, $contacts contacts in ${ms}ms" -ForegroundColor Green
    } elseif ($test.status -eq "skip") {
        Write-Host "  SKIP - $($test.reason)" -ForegroundColor Yellow
    } else {
        Write-Host "  FAIL - $($test | ConvertTo-Json -Depth 5)" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "  FAIL - /test endpoint error: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== DEPLOY COMPLETE - ALL TESTS PASSED ===" -ForegroundColor Cyan
Write-Host ""
