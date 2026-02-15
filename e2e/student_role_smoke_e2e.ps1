param(
  [string]$FrontendBaseUrl = "http://localhost:3000",
  [string]$BackendBaseUrl = "http://localhost:8888",
  [string]$SessionName = "student-role-smoke-e2e"
)

$ErrorActionPreference = "Stop"

function Invoke-PlaywrightCli {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments
  )

  Write-Host ">> playwright-cli $($Arguments -join ' ')" -ForegroundColor Cyan
  $rawOutput = & npx --yes @playwright/cli@latest "-s=$SessionName" @Arguments 2>&1
  $exitCode = $LASTEXITCODE
  $lines = @($rawOutput | ForEach-Object { $_.ToString() })
  if ($lines.Count -gt 0) {
    $lines | ForEach-Object { Write-Host $_ }
  }
  $joined = $lines -join "`n"
  if ($exitCode -ne 0 -or $joined -match "(?m)^### Error\b" -or $joined -match "(?m)^error:\s") {
    throw "Playwright CLI failed: $($Arguments -join ' ')"
  }
}

function Invoke-PlaywrightCode {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Code
  )

  Invoke-PlaywrightCli -Arguments @("run-code", $Code)
}

$timestamp = Get-Date -Format "yyyyMMddHHmmss"
$email = "student_smoke_$timestamp@example.com"
$username = "student_smoke_$timestamp"
$password = "StudentSmoke!$timestamp"

Write-Host "Creating student test user: $email" -ForegroundColor Yellow
$registerPayload = @{
  email = $email
  username = $username
  password = $password
  full_name = "Student Smoke User"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "$BackendBaseUrl/api/v1/auth/register" `
  -ContentType "application/json" `
  -Body $registerPayload | Out-Null

New-Item -ItemType Directory -Force "output/playwright" | Out-Null

try {
  Invoke-PlaywrightCli -Arguments @("open", "$FrontendBaseUrl/login")

  Invoke-PlaywrightCode -Code "(async (page) => { await page.locator('input').first().fill('$email'); await page.locator('input[type=password]').first().fill('$password'); await Promise.all([page.waitForURL('**/dashboard', { timeout: 20000 }), page.click('button[type=submit]')]); })"

  $mustAllow = @(
    "/dashboard",
    "/chat",
    "/knowledge",
    "/literature",
    "/code",
    "/settings",
    "/profile",
    "/student/mentor",
    "/student/announcements",
    "/student/shared"
  )

  foreach ($route in $mustAllow) {
    Invoke-PlaywrightCode -Code "(async (page) => { await page.goto('$FrontendBaseUrl$route'); await page.waitForLoadState('domcontentloaded'); await page.waitForTimeout(600); const current = '/' + page.url().split('/').slice(3).join('/').split('?')[0]; if (current !== '$route') { throw new Error('Expected stay on $route, got ' + current); } })"
  }

  $blocked = @("/mentor/students", "/mentor/groups", "/admin/users")
  foreach ($route in $blocked) {
    Invoke-PlaywrightCode -Code "(async (page) => { await page.goto('$FrontendBaseUrl$route'); await page.waitForLoadState('domcontentloaded'); await page.waitForTimeout(600); const current = '/' + page.url().split('/').slice(3).join('/').split('?')[0]; if (current === '$route') { throw new Error('Expected role guard to block $route, but stayed on it'); } if (current !== '/dashboard') { throw new Error('Expected redirect to /dashboard for blocked route $route, got ' + current); } })"
  }

  Invoke-PlaywrightCli -Arguments @("screenshot", "--filename", "output/playwright/student-role-smoke-e2e.png", "--full-page")
  Invoke-PlaywrightCli -Arguments @("snapshot")

  Write-Host "Student role smoke E2E completed successfully." -ForegroundColor Green
  Write-Host "Screenshot: output/playwright/student-role-smoke-e2e.png" -ForegroundColor Green
}
finally {
  try {
    Invoke-PlaywrightCli -Arguments @("close")
  }
  catch {
    Write-Host "Browser already closed or close failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
  }
}
