param(
  [ValidateSet("mentor", "admin")]
  [string]$Role,
  [string]$FrontendBaseUrl = "http://localhost:3000",
  [string]$BackendBaseUrl = "http://localhost:8888",
  [string]$SessionName = "role-route-smoke-e2e",
  [string]$DbContainer = "research_postgres",
  [string]$DbUser = "",
  [string]$DbName = "",
  [string]$EnvFilePath = ".env"
)

$ErrorActionPreference = "Stop"

function Get-EnvValue {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Key
  )

  if (-not (Test-Path $Path)) {
    return ""
  }

  $line = Select-String -Path $Path -Pattern "^\s*$Key=(.*)$" | Select-Object -First 1
  if (-not $line) {
    return ""
  }

  $value = $line.Matches[0].Groups[1].Value.Trim()
  return $value
}

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

$dbUserResolved = if ([string]::IsNullOrWhiteSpace($DbUser)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_USER" } else { $DbUser }
$dbNameResolved = if ([string]::IsNullOrWhiteSpace($DbName)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_DB" } else { $DbName }
if ([string]::IsNullOrWhiteSpace($dbUserResolved)) { $dbUserResolved = "research_user" }
if ([string]::IsNullOrWhiteSpace($dbNameResolved)) { $dbNameResolved = "research_assistant" }

$timestamp = Get-Date -Format "yyyyMMddHHmmss"
$email = "${Role}_smoke_$timestamp@example.com"
$username = "${Role}_smoke_$timestamp"
$password = "RoleSmoke!$timestamp"

Write-Host "Creating $Role test user: $email" -ForegroundColor Yellow
$registerPayload = @{
  email = $email
  username = $username
  password = $password
  full_name = "$Role Smoke User"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "$BackendBaseUrl/api/v1/auth/register" `
  -ContentType "application/json" `
  -Body $registerPayload | Out-Null

$setRoleSql = "UPDATE users SET role='$Role' WHERE email='$email';"
$verifySql = "SELECT role FROM users WHERE email='$email';"

Write-Host "Promoting role in DB: user=$dbUserResolved db=$dbNameResolved container=$DbContainer" -ForegroundColor Yellow
docker exec $DbContainer psql -U $dbUserResolved -d $dbNameResolved -c $setRoleSql | Out-Null
$verifyOutput = docker exec $DbContainer psql -U $dbUserResolved -d $dbNameResolved -t -A -c $verifySql
if ($verifyOutput -notmatch "^$Role$") {
  throw "Role update failed, expected '$Role', got '$verifyOutput'"
}

$mustAllow = @("/dashboard", "/chat", "/knowledge", "/literature", "/code", "/settings", "/profile")
$blocked = @()

if ($Role -eq "mentor") {
  $mustAllow += @("/mentor/students", "/mentor/groups", "/mentor/announcements", "/mentor/shares")
  $blocked = @("/admin/users", "/admin/statistics", "/student/mentor", "/student/shared", "/student/announcements")
}
elseif ($Role -eq "admin") {
  $mustAllow += @("/admin/users", "/admin/statistics")
  $blocked = @("/mentor/students", "/mentor/groups", "/mentor/announcements", "/mentor/shares", "/student/mentor", "/student/shared", "/student/announcements")
}
else {
  throw "Unsupported role: $Role"
}

New-Item -ItemType Directory -Force "output/playwright" | Out-Null

try {
  Invoke-PlaywrightCli -Arguments @("open", "$FrontendBaseUrl/login")

  Invoke-PlaywrightCode -Code "(async (page) => { await page.locator('input').first().fill('$email'); await page.locator('input[type=password]').first().fill('$password'); await Promise.all([page.waitForURL('**/dashboard', { timeout: 20000 }), page.click('button[type=submit]')]); })"

  foreach ($route in $mustAllow) {
    Invoke-PlaywrightCode -Code "(async (page) => { await page.goto('$FrontendBaseUrl$route'); await page.waitForLoadState('domcontentloaded'); await page.waitForTimeout(600); const current = '/' + page.url().split('/').slice(3).join('/').split('?')[0]; if (current !== '$route') { throw new Error('Expected stay on $route, got ' + current); } })"
  }

  foreach ($route in $blocked) {
    Invoke-PlaywrightCode -Code "(async (page) => { await page.goto('$FrontendBaseUrl$route'); await page.waitForLoadState('domcontentloaded'); await page.waitForTimeout(600); const current = '/' + page.url().split('/').slice(3).join('/').split('?')[0]; if (current === '$route') { throw new Error('Expected role guard to block $route, but stayed on it'); } if (current !== '/dashboard') { throw new Error('Expected redirect to /dashboard for blocked route $route, got ' + current); } })"
  }

  $screenshotPath = "output/playwright/$Role-role-smoke-e2e.png"
  Invoke-PlaywrightCli -Arguments @("screenshot", "--filename", $screenshotPath, "--full-page")
  Invoke-PlaywrightCli -Arguments @("snapshot")

  Write-Host "$Role role smoke E2E completed successfully." -ForegroundColor Green
  Write-Host "Screenshot: $screenshotPath" -ForegroundColor Green
}
finally {
  try {
    Invoke-PlaywrightCli -Arguments @("close")
  }
  catch {
    Write-Host "Browser already closed or close failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
  }
}
