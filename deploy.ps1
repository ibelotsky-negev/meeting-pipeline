param(
    [string]$RepoDir = "$env:USERPROFILE\Downloads\pipeline",
    [string]$BaseUrl = "https://meeting-pipeline-production.up.railway.app"
)

$ErrorActionPreference = "Continue"
Write-Host ""
Write-Host "=== RAILWAY DEPLOY ===" -ForegroundColor Cyan
Write-Host ("Repo:    " + $RepoDir)
Write-Host ("Service: " + $BaseUrl)
Write-Host ""

# ─── STEP 1: Find newest app*.py in Downloads ─────────────────────────────────
Write-Host "[1/6] Finding newest app*.py in Downloads..." -ForegroundColor Yellow
$downloads = "$env:USERPROFILE\Downloads"
$candidates = Get-ChildItem -Path $downloads -Filter "app*.py" | Sort-Object LastWriteTime -Descending
if ($candidates.Count -eq 0) {
    Write-Host "  ERROR: No app*.py found in Downloads folder" -ForegroundColor Red
    exit 1
}
$sourceFile = $candidates[0].FullName
Write-Host ("  Using: " + $candidates[0].Name + " (modified " + $candidates[0].LastWriteTime + ")") -ForegroundColor Green

# ─── STEP 2: Extract version dynamically from source file ─────────────────────
Write-Host "[2/6] Extracting version from source file..." -ForegroundColor Yellow
$content = Get-Content $sourceFile -Raw
$match = [regex]::Match($content, '"version":\s*"([^"]+)"')
if (-not $match.Success) {
    Write-Host "  ERROR: No version string found in app.py" -ForegroundColor Red
    Write-Host "  Add:  return jsonify({'version': 'X.Y.Z-description'})" -ForegroundColor Red
    exit 1
}
$expectedVersion = $match.Groups[1].Value
Write-Host ("  Detected version: " + $expectedVersion) -ForegroundColor Green

# ─── STEP 3: Copy to repo + write CACHEBUST ───────────────────────────────────
Write-Host "[3/6] Copying to repo and writing CACHEBUST..." -ForegroundColor Yellow
Copy-Item $sourceFile -Destination "$RepoDir\app.py" -Force
$timestamp = Get-Date -Format "yyyyMMddHHmmss"
Set-Content -Path "$RepoDir\CACHEBUST" -Value $timestamp -NoNewline
Write-Host ("  Cache-bust: " + $timestamp) -ForegroundColor Green

# ─── STEP 4: Git commit and push ──────────────────────────────────────────────
Write-Host "[4/6] Git commit and push..." -ForegroundColor Yellow
Set-Location $RepoDir
git add app.py CACHEBUST
git commit -m ("deploy: " + $expectedVersion + " [" + $timestamp + "]")
git push 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Normal push failed, trying force push..." -ForegroundColor Yellow
    git push --force 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Git push failed. Check git credentials." -ForegroundColor Red
        exit 1
    }
}
Write-Host "  Pushed to GitHub" -ForegroundColor Green

# ─── STEP 5: Poll /version until new version is live (up to 4 minutes) ────────
Write-Host "[5/6] Polling /version until Railway deploys new version..." -ForegroundColor Yellow
Write-Host ("  Waiting for: " + $expectedVersion) -ForegroundColor Gray
$confirmed = $false
for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 20
    try {
        $resp = Invoke-RestMethod -Uri ($BaseUrl + "/version") -TimeoutSec 10
        $liveVersion = $resp.version
        Write-Host ("  [" + ($i * 20) + "s] Live: " + $liveVersion) -ForegroundColor Cyan
        if ($liveVersion -eq $expectedVersion) {
            Write-Host ("  VERSION CONFIRMED: " + $expectedVersion) -ForegroundColor Green
            $confirmed = $true
            break
        }
    } catch {
        Write-Host ("  [" + ($i * 20) + "s] Not responding yet...") -ForegroundColor Gray
    }
}

if (-not $confirmed) {
    Write-Host ""
    Write-Host "  TIMEOUT: New version not live after 4 minutes." -ForegroundColor Red
    Write-Host "  Diagnose:" -ForegroundColor Yellow
    Write-Host "    1. Check Railway dashboard for build errors" -ForegroundColor Yellow
    Write-Host "    2. Run: railway logs --tail 50" -ForegroundColor Yellow
    Write-Host "    3. Try: railway redeploy" -ForegroundColor Yellow
    exit 1
}

# ─── STEP 6: Run /test endpoint ───────────────────────────────────────────────
Write-Host "[6/6] Running /test (dry-run)..." -ForegroundColor Yellow
$testPassed = $false
for ($i = 1; $i -le 3; $i++) {
    try {
        $testResp = Invoke-RestMethod -Uri ($BaseUrl + "/test") -TimeoutSec 60
        $status = $testResp.status
        Write-Host ("  Test status: " + $status) -ForegroundColor Cyan
        if ($status -eq "pass") {
            Write-Host "  ALL TESTS PASSED" -ForegroundColor Green
            $testPassed = $true
            break
        } else {
            Write-Host ("  FAILED: " + ($testResp | ConvertTo-Json -Depth 3)) -ForegroundColor Red
            break
        }
    } catch {
        Write-Host ("  Attempt " + $i + " failed: " + $_.Exception.Message) -ForegroundColor Yellow
        if ($i -lt 3) { Start-Sleep -Seconds 15 }
    }
}

if (-not $testPassed) {
    Write-Host "  WARNING: /test did not pass. Check Railway logs." -ForegroundColor Red
}

# ─── SUMMARY ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== DEPLOY COMPLETE ===" -ForegroundColor Cyan
Write-Host ("Version: " + $BaseUrl + "/version")
Write-Host ("Config:  " + $BaseUrl + "/config")
Write-Host ("Test:    " + $BaseUrl + "/test")
Write-Host ""