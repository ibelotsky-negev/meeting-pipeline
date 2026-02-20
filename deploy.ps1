param(
    [string]$RepoDir = "$env:USERPROFILE\Downloads\pipeline",
    [string]$BaseUrl = "https://meeting-pipeline-production.up.railway.app"
)

Write-Host ''
Write-Host '=== RAILWAY DEPLOY PIPELINE ===' -ForegroundColor Cyan

# Step 1: Find newest app*.py in Downloads
Write-Host ''
Write-Host '[1/6] Finding newest app*.py download...' -ForegroundColor Yellow
$downloads = "$env:USERPROFILE\Downloads"
$candidates = Get-ChildItem -Path $downloads -Filter 'app*.py' | Sort-Object LastWriteTime -Descending
if ($candidates.Count -eq 0) {
    Write-Host 'ERROR: No app*.py files found in Downloads' -ForegroundColor Red
    exit 1
}
$newest = $candidates[0]
Write-Host ('  Found: ' + $newest.Name + ' (modified ' + $newest.LastWriteTime + ')') -ForegroundColor Green

# Step 2: Copy to git repo
Write-Host ''
Write-Host '[2/6] Copying to repo...' -ForegroundColor Yellow
Copy-Item $newest.FullName -Destination "$RepoDir\app.py" -Force
$verCheck = Select-String '2.4.0-task-routing-fix' "$RepoDir\app.py"
if ($verCheck) {
    Write-Host '  Verified: version string found in repo file' -ForegroundColor Green
} else {
    Write-Host 'ERROR: Version string NOT found. Wrong download?' -ForegroundColor Red
    exit 1
}

# Step 3: Git push
Write-Host ''
Write-Host '[3/6] Git commit and push...' -ForegroundColor Yellow
Set-Location $RepoDir
git add app.py
git commit -m 'feat: task routing (hubspot/asana), owner fix, notify all internal v2.4.0'
git push 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  Normal push failed, trying force push...' -ForegroundColor Yellow
    git push --force
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: Force push also failed' -ForegroundColor Red
        exit 1
    }
}
Write-Host '  Pushed successfully' -ForegroundColor Green

# Step 4: Wait for Railway build
Write-Host ''
Write-Host '[4/6] Waiting 45s for Railway build+deploy...' -ForegroundColor Yellow
Start-Sleep -Seconds 45

# Step 5: Verify /version
Write-Host ''
Write-Host '[5/6] Checking /version endpoint...' -ForegroundColor Yellow
$versionOk = $false
for ($i = 1; $i -le 3; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri ($BaseUrl + '/version') -TimeoutSec 10
        Write-Host ('  Attempt ' + $i + ': version=' + $resp.version) -ForegroundColor Cyan
        if ($resp.version -eq '2.4.0-task-routing-fix') {
            Write-Host '  VERSION VERIFIED' -ForegroundColor Green
            $versionOk = $true
            break
        } else {
            Write-Host '  Stale version, waiting 15s...' -ForegroundColor Yellow
        }
    } catch {
        Write-Host ('  Attempt ' + $i + ' failed, waiting 15s...') -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 15
}
if (-not $versionOk) {
    Write-Host 'WARNING: Could not verify new version. Check Railway dashboard.' -ForegroundColor Red
}

# Step 6: Run /test
Write-Host ''
Write-Host '[6/6] Running /test endpoint...' -ForegroundColor Yellow
for ($i = 1; $i -le 3; $i++) {
    try {
        $testResp = Invoke-RestMethod -Uri ($BaseUrl + '/test') -TimeoutSec 60
        Write-Host ('  Test status: ' + $testResp.status) -ForegroundColor Cyan
        if ($testResp.status -eq 'pass') {
            Write-Host '  ALL TESTS PASSED' -ForegroundColor Green
            break
        }
    } catch {
        Write-Host ('  Attempt ' + $i + ' failed, waiting 15s...') -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 15
}

Write-Host ''
Write-Host '=== DEPLOY COMPLETE ===' -ForegroundColor Cyan
Write-Host ('Version: ' + $BaseUrl + '/version')
Write-Host ('Config:  ' + $BaseUrl + '/config')
Write-Host ('Test:    ' + $BaseUrl + '/test')
Write-Host ''
