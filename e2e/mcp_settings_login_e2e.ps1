param(
  [string]$FrontendBaseUrl = "http://localhost:3000",
  [string]$BackendBaseUrl = "http://localhost:8888",
  [string]$SessionName = "mcp-login-e2e"
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
$email = "mcp_e2e_$timestamp@example.com"
$username = "mcp_e2e_$timestamp"
$password = "McpE2e!$timestamp"

Write-Host "Creating test user: $email" -ForegroundColor Yellow
$registerPayload = @{
  email = $email
  username = $username
  password = $password
  fullName = "MCP E2E User"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "$BackendBaseUrl/api/auth/register" `
  -ContentType "application/json" `
  -Body $registerPayload | Out-Null

New-Item -ItemType Directory -Force "output/playwright" | Out-Null

try {
  Invoke-PlaywrightCli -Arguments @("open", "$FrontendBaseUrl/login")

  Invoke-PlaywrightCode -Code "(async (page) => { await page.locator('input').first().fill('$email'); await page.locator('input[type=password]').first().fill('$password'); await Promise.all([page.waitForURL('**/dashboard', { timeout: 20000 }), page.click('button[type=submit]')]); })"

  Invoke-PlaywrightCode -Code "(async (page) => { await page.goto('$FrontendBaseUrl/settings'); await page.waitForURL('**/settings', { timeout: 20000 }); await page.waitForSelector('textarea.ant-input', { timeout: 20000 }); const text = await page.locator('textarea.ant-input').first().inputValue(); JSON.parse(text); })"

  Invoke-PlaywrightCode -Code "(async (page) => { await page.getByRole('button', { name: /Apply Template/i }).click(); await page.waitForTimeout(500); })"

  Invoke-PlaywrightCode -Code "(async (page) => { await Promise.all([page.waitForResponse((resp) => resp.url().includes('/api/mcp/config/validate') && resp.request().method() === 'POST' && resp.status() >= 200 && resp.status() < 300, { timeout: 20000 }), page.getByRole('button', { name: /Validate/i }).click()]); })"

  Invoke-PlaywrightCode -Code "(async (page) => { await Promise.all([page.waitForResponse((resp) => resp.url().includes('/api/mcp/config') && resp.request().method() === 'PUT' && resp.status() >= 200 && resp.status() < 300, { timeout: 20000 }), page.locator('button:has(.anticon-save)').nth(1).click()]); })"

  Invoke-PlaywrightCode -Code "(async (page) => { await Promise.all([page.waitForResponse((resp) => resp.url().includes('/api/mcp/status/refresh') && resp.request().method() === 'POST' && resp.status() >= 200 && resp.status() < 300, { timeout: 20000 }), page.getByRole('button', { name: /Refresh Status/i }).click()]); const hasTransportText = (await page.getByText('transport:', { exact: false }).count()) > 0; const hasEmptyState = (await page.getByText('No status yet', { exact: false }).count()) > 0; if (!hasTransportText && !hasEmptyState) { throw new Error('MCP status section did not render expected content'); } })"

  Invoke-PlaywrightCli -Arguments @("screenshot", "--filename", "output/playwright/mcp-settings-login-e2e.png", "--full-page")
  Invoke-PlaywrightCli -Arguments @("snapshot")

  Write-Host "E2E completed successfully." -ForegroundColor Green
  Write-Host "Screenshot: output/playwright/mcp-settings-login-e2e.png" -ForegroundColor Green
}
finally {
  try {
    Invoke-PlaywrightCli -Arguments @("close")
  }
  catch {
    Write-Host "Browser already closed or close failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
  }
}
